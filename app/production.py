from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)

from app.event_control_service import EventControlError, EventControlService
from app.event_service import EventService
from app.live_service import LiveEventService
from app.photo_service import (
    PhotoNotFoundError,
    PhotoService,
    PhotoStorageError,
    PhotoValidationError,
)
from app.security import operator_only
from app.settings_service import SettingsService, SettingsStorageError, SettingsValidationError
from app.timeline_service import TimelineError, TimelineService, TimelineValidationError

LOGGER = logging.getLogger("arcobaleno.production.routes")
production_blueprint = Blueprint("production", __name__)


def _event_service() -> EventService:
    return current_app.extensions["arcobaleno_event_service"]


def _photo_service() -> PhotoService:
    return current_app.extensions["arcobaleno_photo_service"]


def _control_service() -> EventControlService:
    return current_app.extensions["arcobaleno_event_control_service"]


def _timeline_service() -> TimelineService:
    return current_app.extensions["arcobaleno_timeline_service"]


def _live_service() -> LiveEventService:
    return current_app.extensions["arcobaleno_live_event_service"]


def _settings_service() -> SettingsService:
    return current_app.extensions["arcobaleno_settings_service"]


def _checkpoint(event: dict[str, Any] | None, reason: str) -> None:
    if event is None:
        return
    try:
        current_app.extensions["arcobaleno_recovery_service"].create_automatic_checkpoint(event, reason)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not create automatic recovery checkpoint (%s)", reason, exc_info=True)


def _active_event() -> dict[str, Any] | None:
    return _event_service().get_active_event()


def _operator_photo_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = PhotoService.operator_photo(record)
    photo_id = payload["id"]
    payload.update(
        {
            "thumbnail_url": url_for("production.photo_thumbnail", photo_id=photo_id),
            "original_url": url_for("slideshow.slideshow_photo", photo_id=photo_id),
            "download_url": url_for("slideshow.slideshow_photo", photo_id=photo_id, download="1"),
            "moderate_url": url_for("production.moderate_photo_api", photo_id=photo_id),
            "delete_url": url_for("production.delete_photo_api", photo_id=photo_id),
        }
    )
    return payload


def _control_payload(control: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "event_mode",
        "uploads_open",
        "pause_message",
        "moderation_mode",
        "moderation_delay_seconds",
        "slideshow_state",
        "slideshow_command",
        "slideshow_command_sequence",
        "slideshow_title",
        "slideshow_message",
        "featured_marker_id",
        "live_started_at",
        "live_stopped_at",
        "updated_at",
    )
    return {key: control.get(key) for key in allowed}


def operator_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    event_id = str(event["id"])
    live_sequence = _live_service().latest_sequence(event_id)
    photo_service = _photo_service()
    control = _control_service().get(event_id)
    counts = photo_service.counts(event_id)
    latest = photo_service.latest_photo(event_id)
    displays = _live_service().display_status(event_id)
    return {
        "event": EventService.public_event(event),
        "control": _control_payload(control),
        "counts": counts,
        "photo_count": counts["total"],
        "guest_count": photo_service.unique_guest_count(event_id),
        "latest_photo": _operator_photo_payload(latest) if latest else None,
        "displays": displays,
        "timeline": _timeline_service().list_entries(event_id)[-8:],
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "live_sequence": live_sequence,
        "photo_limit": int(photo_service.photo_limit),
    }


@production_blueprint.get("/api/operator/snapshot")
@operator_only
def operator_snapshot_api():
    event = _active_event()
    if event is None:
        return jsonify({"active": False, "error": "No event is active."}), 409
    try:
        payload = operator_snapshot(event)
    except (PhotoStorageError, EventControlError, TimelineError):
        LOGGER.exception("Could not build the live operator snapshot")
        return jsonify({"error": "ARCOBALENO could not read the live event state."}), 500
    response = jsonify({"active": True, **payload})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@production_blueprint.get("/api/live/events")
@operator_only
def live_events_api():
    event = _active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    event_id = str(event["id"])
    raw_since = request.args.get("since")
    if raw_since is None:
        raw_since = request.headers.get("Last-Event-ID")
    if raw_since is None:
        since = _live_service().latest_sequence(event_id)
    else:
        try:
            since = max(0, int(raw_since))
        except (TypeError, ValueError):
            since = _live_service().latest_sequence(event_id)
    response = Response(
        stream_with_context(_live_service().stream(event_id, since)),
        mimetype="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@production_blueprint.get("/api/operator/photos")
@operator_only
def operator_photos_api():
    event = _active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    try:
        limit = max(1, min(120, int(request.args.get("limit", "60"))))
    except ValueError:
        limit = 60
    before = request.args.get("before", "")
    around = request.args.get("around", "")
    event_id = str(event["id"])
    live_sequence = _live_service().latest_sequence(event_id)
    try:
        photos = _photo_service().list_photos(
            event_id,
            limit=limit + 1,
            before=before,
            around=around,
        )
        has_more = len(photos) > limit
        photos = photos[:limit]
        payload = [_operator_photo_payload(item) for item in photos]
        counts = _photo_service().counts(event_id)
    except PhotoStorageError:
        LOGGER.exception("Could not read operator photos")
        return jsonify({"error": "ARCOBALENO could not read the photo library."}), 500
    return jsonify(
        {
            "photos": payload,
            "counts": counts,
            "has_more": has_more,
            "next_before": payload[-1]["created_at"] if has_more and payload else "",
            "live_sequence": live_sequence,
            "photo_limit": int(_photo_service().photo_limit),
        }
    )


@production_blueprint.get("/api/operator/photos/<photo_id>")
@operator_only
def operator_photo_api(photo_id: str):
    event = _active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    try:
        record, _path = _photo_service().photo_file(str(event["id"]), photo_id)
        payload = _operator_photo_payload(record)
    except PhotoNotFoundError:
        return jsonify({"error": "That photo was not found."}), 404
    except PhotoStorageError:
        LOGGER.exception("Could not read photo %s", photo_id)
        return jsonify({"error": "ARCOBALENO could not read that photo."}), 500
    return jsonify({"photo": payload})


@production_blueprint.get("/media/thumbnail/<photo_id>")
@operator_only
def photo_thumbnail(photo_id: str):
    event = _active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    try:
        record, thumbnail = _photo_service().thumbnail_file(str(event["id"]), photo_id)
    except PhotoNotFoundError:
        return jsonify({"error": "That thumbnail was not found."}), 404
    except PhotoStorageError:
        LOGGER.exception("Could not create or serve thumbnail %s", photo_id)
        return jsonify({"error": "ARCOBALENO could not prepare that thumbnail."}), 500
    response = send_file(
        thumbnail,
        mimetype="image/jpeg",
        conditional=True,
        etag=f"thumb-{record.get('sha256', '')}",
        last_modified=thumbnail.stat().st_mtime,
        max_age=86400,
    )
    response.headers["Cache-Control"] = "private, max-age=86400, immutable"
    return response


@production_blueprint.post("/api/operator/photos/<photo_id>/moderate")
@operator_only
def moderate_photo_api(photo_id: str):
    event = _active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    action = request.form.get("action", "")
    event_id = str(event["id"])
    try:
        photo = _photo_service().moderate_photo(event_id, photo_id, action)
        counts = _photo_service().counts(event_id)
    except PhotoNotFoundError:
        return jsonify({"error": "That photo was not found."}), 404
    except PhotoValidationError as error:
        return jsonify({"error": str(error)}), 400
    except PhotoStorageError:
        LOGGER.exception("Could not moderate photo %s", photo_id)
        return jsonify({"error": "ARCOBALENO could not update that photo."}), 500

    if action == "hide":
        try:
            _timeline_service().append(
                event_id,
                title="Photo hidden by operator",
                detail=f"An upload from {photo.get('guest_name') or 'a guest'} was removed from displays.",
                kind="moderation",
                icon="camera",
                public=False,
                photo_id=photo_id,
            )
        except TimelineError:
            LOGGER.warning("Could not record moderation timeline entry", exc_info=True)

    full_photo = _operator_photo_payload(photo)
    _live_service().publish(
        event_id,
        "photo.updated",
        {"photo": full_photo, "counts": counts, "action": action},
    )
    _checkpoint(event, f"photo-{action or 'updated'}")
    return jsonify({"ok": True, "photo": full_photo, "counts": counts})


@production_blueprint.post("/api/operator/photos/<photo_id>/delete")
@operator_only
def delete_photo_api(photo_id: str):
    event = _active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    event_id = str(event["id"])
    try:
        deleted = _photo_service().delete_photo(event_id, photo_id)
        counts = _photo_service().counts(event_id)
    except PhotoNotFoundError:
        return jsonify({"error": "That photo was not found."}), 404
    except PhotoStorageError:
        LOGGER.exception("Could not delete photo %s", photo_id)
        return jsonify({"error": "ARCOBALENO could not delete that photo."}), 500
    _live_service().publish(
        event_id,
        "photo.deleted",
        {"photo_id": photo_id, "guest_name": deleted.get("guest_name"), "counts": counts},
    )
    _checkpoint(event, "photo-deleted")
    return jsonify({"ok": True, "photo_id": photo_id, "counts": counts})


@production_blueprint.post("/event/live-mode")
@operator_only
def update_event_mode():
    event = _active_event()
    if event is None:
        return redirect(url_for("events.home"))
    event_id = str(event["id"])
    mode = request.form.get("mode", "setup")
    try:
        control = _control_service().set_event_mode(event_id, mode)
        _timeline_service().append(
            event_id,
            title="Event started" if mode == "live" else "Live event stopped",
            detail=(
                "Guest uploads and production displays are now live."
                if mode == "live"
                else "ARCOBALENO returned to setup mode. Event files remain safely available."
            ),
            kind="system",
            icon="spark",
            public=False,
        )
    except (EventControlError, TimelineError):
        LOGGER.exception("Could not change live event mode")
        raise
    _live_service().publish(event_id, "control.updated", {"control": _control_payload(control)})
    _checkpoint(event, f"event-mode-{mode}")
    notice = "live-started" if mode == "live" else "setup-restored"
    if request.headers.get("Accept", "").startswith("application/json"):
        return jsonify({"ok": True, "control": _control_payload(control)})
    return redirect(url_for("events.home", notice=notice))


@production_blueprint.post("/event/moderation")
@operator_only
def update_moderation_settings():
    event = _active_event()
    if event is None:
        return redirect(url_for("events.home"))
    event_id = str(event["id"])
    try:
        control = _control_service().update(
            event_id,
            moderation_mode=request.form.get("moderation_mode", "family"),
            moderation_delay_seconds=request.form.get("moderation_delay_seconds", "10"),
        )
    except EventControlError:
        LOGGER.exception("Could not update moderation settings")
        raise
    _live_service().publish(event_id, "control.updated", {"control": _control_payload(control)})
    _checkpoint(event, "moderation-updated")
    return redirect(url_for("events.home", notice="moderation-saved"))


@production_blueprint.post("/api/operator/photo-limit")
@operator_only
def photo_limit_api():
    event = _active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    try:
        limit = _settings_service().save_photo_limit(request.form.get("photo_limit", ""))
    except SettingsValidationError as error:
        message = error.errors.get("photo_limit_per_device") or "Choose a limit from 0 to 1000."
        return jsonify({"error": message}), 400
    except SettingsStorageError:
        LOGGER.exception("Could not save the live guest photo limit")
        return jsonify({"error": "ARCOBALENO could not save the guest photo limit."}), 500

    _photo_service().photo_limit = limit
    event_id = str(event["id"])
    _live_service().publish(event_id, "settings.updated", {"photo_limit": limit})
    _checkpoint(event, "photo-limit-updated")
    return jsonify({"ok": True, "photo_limit": limit})


@production_blueprint.post("/api/operator/slideshow-control")
@operator_only
def slideshow_control_api():
    event = _active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    event_id = str(event["id"])
    state = request.form.get("state", "")
    command = request.form.get("command", "")
    title = request.form.get("title")
    message = request.form.get("message")
    marker_id = request.form.get("marker_id")
    try:
        if command:
            control = _control_service().send_slideshow_command(event_id, command)
        else:
            changes: dict[str, Any] = {}
            if state:
                changes["slideshow_state"] = state
            if title is not None:
                changes["slideshow_title"] = title
            if message is not None:
                changes["slideshow_message"] = message
            if marker_id is not None:
                changes["featured_marker_id"] = marker_id
            control = _control_service().update(event_id, **changes)
    except EventControlError as error:
        return jsonify({"error": str(error)}), 400
    payload = _control_payload(control)
    _live_service().publish(event_id, "control.updated", {"control": payload})
    return jsonify({"ok": True, "control": payload})


@production_blueprint.get("/api/display/control")
def display_control_api():
    event = _active_event()
    if event is None:
        return jsonify({"active": False, "error": "No event is active."}), 409
    event_id = str(event["id"])
    try:
        control = _control_service().get(event_id)
        timeline = _timeline_service().public_entries(event_id)
        featured = _timeline_service().get(event_id, str(control.get("featured_marker_id") or ""))
    except (EventControlError, TimelineError):
        LOGGER.exception("Could not read display controls")
        return jsonify({"error": "ARCOBALENO could not read display controls."}), 500
    response = jsonify(
        {
            "active": True,
            "control": _control_payload(control),
            "timeline": timeline,
            "featured_marker": featured if featured and bool(featured.get("public", True)) else None,
        }
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@production_blueprint.post("/api/display/heartbeat")
def display_heartbeat_api():
    event = _active_event()
    if event is None:
        return jsonify({"active": False}), 409
    event_id = str(event["id"])
    changed = _live_service().register_display(
        event_id=event_id,
        display_id=request.form.get("display_id", ""),
        mode=request.form.get("mode", "network"),
        current_photo_id=request.form.get("current_photo_id", ""),
        state=request.form.get("state", ""),
        label=request.form.get("label", ""),
    )
    if changed:
        status = _live_service().display_status(event_id)
        _live_service().publish(event_id, "display.updated", {"displays": status})
    return jsonify({"ok": True})


@production_blueprint.get("/events/current/timeline")
@operator_only
def timeline_page():
    event = _active_event()
    if event is None:
        return redirect(url_for("events.home"))
    event_id = str(event["id"])
    try:
        entries = _timeline_service().list_entries(event_id)
        control = _control_service().get(event_id)
        event_date = str(event.get("event_date") or "")
        for entry in entries:
            scheduled_time = str(entry.get("scheduled_time") or "")
            if event_date and scheduled_time:
                try:
                    scheduled = datetime.fromisoformat(f"{event_date}T{scheduled_time}").astimezone()
                    entry["photo_window_at"] = scheduled.isoformat(timespec="seconds")
                except ValueError:
                    entry["photo_window_at"] = str(entry.get("created_at") or "")
            else:
                entry["photo_window_at"] = str(entry.get("created_at") or "")
    except (TimelineError, EventControlError):
        LOGGER.exception("Could not read event timeline")
        raise
    return render_template(
        "timeline.html",
        active_event=event,
        entries=entries,
        event_control=control,
        notice=request.args.get("notice", ""),
        page_title="Event timeline",
    )


@production_blueprint.post("/events/current/timeline")
@operator_only
def add_timeline_marker():
    event = _active_event()
    if event is None:
        return redirect(url_for("events.home"))
    event_id = str(event["id"])
    try:
        marker = _timeline_service().append(
            event_id,
            title=request.form.get("title", ""),
            detail=request.form.get("detail", ""),
            kind="marker",
            icon=request.form.get("icon", "marker"),
            scheduled_time=request.form.get("scheduled_time", ""),
            public=request.form.get("public") == "true",
        )
    except TimelineValidationError as error:
        return redirect(url_for("production.timeline_page", notice=str(error)))
    except TimelineError:
        LOGGER.exception("Could not add timeline marker")
        raise
    _live_service().publish(event_id, "timeline.updated", {"entry": marker})
    return redirect(url_for("production.timeline_page", notice="marker-added"))


@production_blueprint.post("/events/current/timeline/<entry_id>/delete")
@operator_only
def delete_timeline_marker(entry_id: str):
    event = _active_event()
    if event is None:
        return redirect(url_for("events.home"))
    event_id = str(event["id"])
    try:
        _timeline_service().delete(event_id, entry_id)
    except TimelineError:
        LOGGER.exception("Could not delete timeline marker")
        return redirect(url_for("production.timeline_page"))
    _live_service().publish(event_id, "timeline.updated", {"deleted_id": entry_id})
    return redirect(url_for("production.timeline_page", notice="marker-deleted"))


@production_blueprint.post("/events/current/timeline/display")
@operator_only
def display_timeline():
    event = _active_event()
    if event is None:
        return redirect(url_for("events.home"))
    event_id = str(event["id"])
    marker_id = request.form.get("marker_id", "")
    state = "marker" if marker_id else "timeline"
    try:
        control = _control_service().update(
            event_id,
            slideshow_state=state,
            featured_marker_id=marker_id,
        )
    except EventControlError:
        LOGGER.exception("Could not show timeline on displays")
        raise
    _live_service().publish(event_id, "control.updated", {"control": _control_payload(control)})
    return redirect(url_for("production.timeline_page", notice="display-updated"))
