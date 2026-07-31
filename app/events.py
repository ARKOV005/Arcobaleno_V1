from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.archive_service import ArchiveNotFoundError, ArchiveService, ArchiveStorageError
from app.event_control_service import EventControlError, EventControlService
from app.event_service import (
    ActiveEventExistsError,
    ArchivedEventNotFoundError,
    EventService,
    EventStorageError,
    EventValidationError,
    NoActiveEventError,
)
from app.photo_service import PhotoNotFoundError, PhotoService, PhotoStorageError
from app.security import is_operator_request, operator_only
from app.live_service import LiveEventService
from app.timeline_service import TimelineError, TimelineService
from app.server_state_service import ServerStateService

LOGGER = logging.getLogger("arcobaleno.events.routes")
events_blueprint = Blueprint("events", __name__)

_NOTICE_MESSAGES = {
    "created": "Event created. The workspace is ready.",
    "updated": "Event details saved.",
    "archived": "Event ended and moved safely into the archive.",
    "deleted": "Photo removed from the active event.",
    "live-started": "Live Event Mode started. Guest uploads and production displays are ready.",
    "setup-restored": "ARCOBALENO returned to Setup Mode.",
    "moderation-saved": "Photo moderation settings saved.",
    "reopened": "Archived event reopened in Setup Mode. Its photos and event history are ready.",
}

_ARCHIVE_NOTICE_MESSAGES = {
    "trashed": "Archived event moved to Trash. It can still be restored.",
    "restored": "Event restored to the archive.",
    "deleted-permanently": "Event permanently deleted from Trash.",
    "reopen-blocked": "Finish the current event before reopening an archived one.",
}


def _service() -> EventService:
    return current_app.extensions["arcobaleno_event_service"]


def _photo_service() -> PhotoService:
    return current_app.extensions["arcobaleno_photo_service"]


def _archive_service() -> ArchiveService:
    return current_app.extensions["arcobaleno_archive_service"]


def _event_control_service() -> EventControlService:
    return current_app.extensions["arcobaleno_event_control_service"]


def _live_service() -> LiveEventService:
    return current_app.extensions["arcobaleno_live_event_service"]


def _timeline_service() -> TimelineService:
    return current_app.extensions["arcobaleno_timeline_service"]


def _server_state_service() -> ServerStateService:
    return current_app.extensions["arcobaleno_server_state_service"]


def _checkpoint(event: dict[str, Any] | None, reason: str) -> None:
    if event is None:
        return
    try:
        current_app.extensions["arcobaleno_recovery_service"].create_automatic_checkpoint(event, reason)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not create automatic recovery checkpoint (%s)", reason, exc_info=True)


def _empty_form() -> dict[str, str]:
    return {
        "title": "",
        "hosts": "",
        "event_date": "",
        "venue": "",
        "notes": "",
        "theme": "default",
    }


def _submitted_form() -> dict[str, str]:
    return {
        "title": request.form.get("title", ""),
        "hosts": request.form.get("hosts", ""),
        "event_date": request.form.get("event_date", ""),
        "venue": request.form.get("venue", ""),
        "notes": request.form.get("notes", ""),
        "theme": request.form.get("theme", "default"),
    }


def _dashboard_metrics(active_event: dict[str, Any] | None) -> dict[str, Any]:
    photo_limit = int(current_app.config["ARCOBALENO_CONFIG"]["camera"]["photo_limit_per_device"])
    metrics = {
        "photo_count": 0,
        "guest_count": 0,
        "latest_photo": None,
        "photo_limit": photo_limit,
        "event_control": None,
        "photo_counts": {"total": 0, "approved": 0, "pending": 0, "hidden": 0, "favorites": 0},
        "display_status": {"total": 0, "wired": 0, "network": 0, "preview": 0, "displayed_unique": 0, "sessions": []},
        "timeline_entries": [],
        "storage_status": {
            "level": "healthy",
            "accepting_uploads": True,
            "message": "Server storage is healthy.",
            "free_bytes": 0,
        },
    }
    if active_event is None:
        return metrics

    service = _photo_service()
    event_id = str(active_event["id"])
    try:
        metrics["storage_status"] = current_app.extensions["arcobaleno_storage_safety_service"].status(
            int(service.max_upload_bytes)
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("Could not read storage status for the dashboard")
    metrics["photo_counts"] = service.counts(event_id)
    metrics["photo_count"] = metrics["photo_counts"]["total"]
    metrics["guest_count"] = service.unique_guest_count(event_id)
    metrics["display_status"] = _live_service().display_status(event_id)
    try:
        metrics["timeline_entries"] = _timeline_service().list_entries(event_id)[-6:]
    except TimelineError:
        LOGGER.exception("Could not read timeline entries for the dashboard")
    metrics["latest_photo"] = service.latest_photo(event_id)
    try:
        metrics["event_control"] = _event_control_service().get(event_id)
    except EventControlError:
        LOGGER.exception("Could not read event controls for the dashboard")
        metrics["event_control"] = {
            "event_mode": "setup",
            "uploads_open": False,
            "pause_message": "Photo uploads are temporarily unavailable.",
            "moderation_mode": "family",
            "moderation_delay_seconds": 10,
            "slideshow_state": "logo",
            "slideshow_command": "",
            "slideshow_command_sequence": 0,
            "slideshow_title": "ARCOBALENO",
            "slideshow_message": "Waiting for the celebration to begin.",
            "featured_marker_id": "",
            "live_started_at": None,
            "live_stopped_at": None,
        }
    return metrics


@events_blueprint.get("/")
def home():
    service = _service()
    active_event = service.get_active_event()

    if not is_operator_request():
        guest_control = None
        if active_event is not None:
            try:
                guest_control = _event_control_service().get(str(active_event["id"]))
            except EventControlError:
                LOGGER.exception("Could not read event controls for guest landing")
                guest_control = {
                    "uploads_open": False,
                    "pause_message": "Photo uploads are temporarily unavailable.",
                }
        return render_template(
            "guest_landing.html",
            active_event=EventService.public_event(active_event),
            event_control=guest_control,
            page_title="Event access",
        )

    notice = _NOTICE_MESSAGES.get(request.args.get("notice", ""))
    metrics = _dashboard_metrics(active_event)
    recovery_attention = _server_state_service().recovery_attention(
        str(active_event.get("id") or "") if active_event else None
    )
    return render_template(
        "index.html",
        active_event=active_event,
        archive_count=service.count_archived_events(),
        notice=notice,
        recovery_attention=recovery_attention,
        page_title="Operator dashboard",
        **metrics,
    )


@events_blueprint.get("/events/new")
@operator_only
def new_event():
    if _service().get_active_event() is not None:
        return redirect(url_for("events.home"))
    notice = None
    if request.args.get("notice") == "recovered-archived":
        notice = "The recovered event was archived safely. Create the next event when you are ready."
    return render_template(
        "event_form.html",
        form_data=_empty_form(),
        errors={},
        form_mode="create",
        notice=notice,
        page_title="Create event",
    )


@events_blueprint.post("/events")
@operator_only
def create_event():
    form_data = _submitted_form()
    try:
        created_event = _service().create_event(form_data)
        event_id = str(created_event["id"])
        try:
            _event_control_service().update(
                event_id,
                event_mode="setup",
                uploads_open=False,
                moderation_mode="family",
                moderation_delay_seconds=10,
                slideshow_state="logo",
                slideshow_title="ARCOBALENO",
                slideshow_message="Waiting for the celebration to begin.",
            )
        except EventControlError:
            LOGGER.warning("Could not initialize event controls", exc_info=True)
        try:
            _timeline_service().append(
                event_id,
                title="Event workspace created",
                detail="Setup Mode is ready for guest access, moderation, and display preparation.",
                kind="system",
                icon="spark",
                public=False,
            )
        except TimelineError:
            LOGGER.warning("Could not initialize the event timeline", exc_info=True)
    except EventValidationError as error:
        return render_template(
            "event_form.html",
            form_data=form_data,
            errors=error.errors,
            form_mode="create",
            page_title="Create event",
        ), 400
    except ActiveEventExistsError:
        return redirect(url_for("events.home"))
    except EventStorageError:
        LOGGER.exception("Could not create an event")
        raise

    _checkpoint(created_event, "event-created")
    return redirect(url_for("events.home", notice="created"))


@events_blueprint.post("/startup/recovery/continue")
@operator_only
def continue_recovered_event():
    active_event = _service().get_active_event()
    if active_event is None:
        return redirect(url_for("events.home"))
    try:
        _server_state_service().acknowledge_recovery(str(active_event.get("id") or ""))
    except OSError:
        LOGGER.exception("Could not acknowledge the recovered event")
    return redirect(url_for("events.photo_library"))


@events_blueprint.post("/startup/recovery/archive")
@operator_only
def archive_recovered_event():
    active_event = _service().get_active_event()
    if active_event is None:
        return redirect(url_for("events.new_event"))
    if request.form.get("confirmation") != "archive-recovered":
        return redirect(url_for("events.home"))

    event_id = str(active_event.get("id") or "")
    _checkpoint(active_event, "before-archive")
    try:
        _service().end_event()
    except NoActiveEventError:
        return redirect(url_for("events.new_event"))
    except EventStorageError:
        LOGGER.exception("Could not archive the recovered event")
        raise

    try:
        _server_state_service().acknowledge_recovery(event_id)
    except OSError:
        LOGGER.exception("Could not acknowledge the archived recovered event")
    return redirect(url_for("events.new_event", notice="recovered-archived"))


@events_blueprint.get("/events/current/edit")
@operator_only
def edit_event():
    active_event = _service().get_active_event()
    if active_event is None:
        return redirect(url_for("events.home"))

    return render_template(
        "event_form.html",
        form_data={**active_event, "theme": str(active_event.get("theme") or "default")},
        errors={},
        form_mode="edit",
        page_title="Edit event",
    )


@events_blueprint.post("/events/current")
@operator_only
def update_event():
    form_data = _submitted_form()
    try:
        updated_event = _service().update_event(form_data)
    except EventValidationError as error:
        return render_template(
            "event_form.html",
            form_data=form_data,
            errors=error.errors,
            form_mode="edit",
            page_title="Edit event",
        ), 400
    except NoActiveEventError:
        return redirect(url_for("events.home"))
    except EventStorageError:
        LOGGER.exception("Could not update the active event")
        raise

    _checkpoint(updated_event, "event-updated")
    return redirect(url_for("events.home", notice="updated"))


@events_blueprint.get("/events/current/photos")
@operator_only
def photo_library():
    active_event = _service().get_active_event()
    if active_event is None:
        return redirect(url_for("events.home"))

    service = _photo_service()
    event_id = str(active_event["id"])
    counts = service.counts(event_id)
    try:
        event_control = _event_control_service().get(event_id)
    except EventControlError:
        LOGGER.exception("Could not read controls for the live photo console")
        event_control = {"slideshow_state": "playing"}
    return render_template(
        "photo_library.html",
        active_event=active_event,
        photo_counts=counts,
        photo_count=counts["total"],
        guest_count=service.unique_guest_count(event_id),
        photo_limit=int(service.photo_limit),
        event_control=event_control,
        around=request.args.get("around", ""),
        timeline_label=request.args.get("label", ""),
        page_title="Live photo console",
    )


@events_blueprint.get("/events/current/export")
@operator_only
def export_event():
    active_event = _service().get_active_event()
    if active_event is None:
        return redirect(url_for("events.home"))

    try:
        zip_path = _photo_service().create_event_export(active_event)
    except PhotoStorageError:
        LOGGER.exception("Could not create event export")
        raise

    response = send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=Path(zip_path).name,
        conditional=True,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@events_blueprint.post("/events/current/photos/<photo_id>/delete")
@operator_only
def delete_photo(photo_id: str):
    active_event = _service().get_active_event()
    if active_event is None:
        return redirect(url_for("events.home"))

    event_id = str(active_event["id"])
    try:
        deleted = _photo_service().delete_photo(event_id, photo_id)
    except PhotoNotFoundError:
        return redirect(url_for("events.photo_library"))
    except PhotoStorageError:
        LOGGER.exception("Could not delete photo %s", photo_id)
        raise

    _live_service().publish(event_id, "photo.deleted", {"photo_id": photo_id, "guest_name": deleted.get("guest_name")})
    _checkpoint(active_event, "photo-deleted")
    return redirect(url_for("events.photo_library", notice="deleted"))


@events_blueprint.post("/events/current/end")
@operator_only
def end_event():
    if request.form.get("confirmation") != "archive":
        return redirect(url_for("events.home"))

    active_event = _service().get_active_event()
    _checkpoint(active_event, "before-archive")
    try:
        _service().end_event()
    except NoActiveEventError:
        return redirect(url_for("events.home"))
    except EventStorageError:
        LOGGER.exception("Could not archive the active event")
        raise

    return redirect(url_for("events.home", notice="archived"))


@events_blueprint.get("/events/archive")
@operator_only
def archive():
    event_service = _service()
    active_event = event_service.get_active_event()
    active_summary = _dashboard_metrics(active_event)
    archived_events = event_service.list_archived_events()
    archive_service = _archive_service()
    for event in archived_events:
        try:
            photos = archive_service.photos(str(event.get("archive_folder") or ""))
        except (ArchiveNotFoundError, ArchiveStorageError):
            photos = []
        event["photo_count"] = len(photos)
        event["guest_count"] = len({
            str(photo.get("guest_name") or "").strip().casefold()
            for photo in photos
            if str(photo.get("guest_name") or "").strip()
        })
    return render_template(
        "archive.html",
        active_event=active_event,
        active_summary=active_summary,
        archived_events=archived_events,
        trash_count=archive_service.count_trashed_events(),
        notice=_ARCHIVE_NOTICE_MESSAGES.get(request.args.get("notice", "")),
        page_title="Events",
    )


@events_blueprint.post("/events/archive/<folder_name>/reopen")
@operator_only
def reopen_archived_event(folder_name: str):
    if request.form.get("confirmation") != "reopen":
        return redirect(url_for("events.archive"))

    try:
        reopened_event = _service().reopen_archived_event(folder_name)
    except ActiveEventExistsError:
        return redirect(url_for("events.archive", notice="reopen-blocked"))
    except ArchivedEventNotFoundError:
        return redirect(url_for("events.archive"))
    except EventStorageError:
        LOGGER.exception("Could not reopen archived event %s", folder_name)
        raise

    event_id = str(reopened_event.get("id") or "")
    try:
        _event_control_service().update(
            event_id,
            event_mode="setup",
            uploads_open=False,
            moderation_mode="family",
            moderation_delay_seconds=10,
            slideshow_state="logo",
            slideshow_title="ARCOBALENO",
            slideshow_message="Waiting for the celebration to begin.",
            live_started_at=None,
            live_stopped_at=None,
        )
    except EventControlError:
        LOGGER.exception("Could not initialize safe controls for reopened event %s", event_id)
        raise

    try:
        _timeline_service().append(
            event_id,
            title="Archived event reopened",
            detail="The preserved workspace returned in Setup Mode with guest uploads paused.",
            kind="system",
            icon="spark",
            public=False,
        )
    except TimelineError:
        LOGGER.warning("Could not append the reopened event timeline entry", exc_info=True)

    _checkpoint(reopened_event, "event-reopened")
    return redirect(url_for("events.home", notice="reopened"))


@events_blueprint.post("/events/archive/<folder_name>/trash")
@operator_only
def trash_archived_event(folder_name: str):
    if request.form.get("confirmation") != "trash":
        return redirect(url_for("events.archive"))
    try:
        _archive_service().move_to_trash(folder_name)
    except ArchiveNotFoundError:
        return redirect(url_for("events.archive"))
    except ArchiveStorageError:
        LOGGER.exception("Could not move archived event %s to Trash", folder_name)
        raise
    return redirect(url_for("events.archive", notice="trashed"))


@events_blueprint.get("/events/archive/trash")
@operator_only
def archive_trash():
    try:
        deleted_events = _archive_service().list_trashed_events()
    except ArchiveStorageError:
        LOGGER.exception("Could not read archive Trash")
        raise
    return render_template(
        "archive_trash.html",
        deleted_events=deleted_events,
        notice=_ARCHIVE_NOTICE_MESSAGES.get(request.args.get("notice", "")),
        page_title="Archive Trash",
    )


@events_blueprint.post("/events/archive/trash/<folder_name>/restore")
@operator_only
def restore_trashed_event(folder_name: str):
    if request.form.get("confirmation") != "restore":
        return redirect(url_for("events.archive_trash"))
    try:
        _archive_service().restore_from_trash(folder_name)
    except ArchiveNotFoundError:
        return redirect(url_for("events.archive_trash"))
    except ArchiveStorageError:
        LOGGER.exception("Could not restore deleted event %s", folder_name)
        raise
    return redirect(url_for("events.archive", notice="restored"))


@events_blueprint.post("/events/archive/trash/<folder_name>/delete")
@operator_only
def permanently_delete_trashed_event(folder_name: str):
    if request.form.get("confirmation") != "DELETE":
        return redirect(url_for("events.archive_trash"))
    try:
        _archive_service().permanently_delete(folder_name)
    except ArchiveNotFoundError:
        return redirect(url_for("events.archive_trash"))
    except ArchiveStorageError:
        LOGGER.exception("Could not permanently delete event %s", folder_name)
        raise
    return redirect(url_for("events.archive_trash", notice="deleted-permanently"))


@events_blueprint.get("/events/archive/<folder_name>")
@operator_only
def archive_detail(folder_name: str):
    try:
        event = _archive_service().event(folder_name)
        photos = _archive_service().photos(folder_name)
    except ArchiveNotFoundError:
        return redirect(url_for("events.archive"))
    except ArchiveStorageError:
        LOGGER.exception("Could not read archived event %s", folder_name)
        raise

    guest_count = len({
        str(photo.get("guest_name") or "").strip().casefold()
        for photo in photos
        if str(photo.get("guest_name") or "").strip()
    })
    return render_template(
        "archive_detail.html",
        archived_event=event,
        photos=photos,
        photo_count=len(photos),
        guest_count=guest_count,
        active_event=_service().get_active_event(),
        page_title=f"{event.get('title', 'Archived event')} Archive",
    )


@events_blueprint.get("/events/archive/<folder_name>/export")
@operator_only
def export_archived_event(folder_name: str):
    try:
        path = _archive_service().create_export(folder_name)
    except ArchiveNotFoundError:
        return redirect(url_for("events.archive"))
    except ArchiveStorageError:
        LOGGER.exception("Could not export archived event %s", folder_name)
        raise
    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=path.name,
        conditional=True,
    )


@events_blueprint.get("/media/archive/<folder_name>/<photo_id>")
@operator_only
def archived_photo(folder_name: str, photo_id: str):
    try:
        record, path = _archive_service().photo_file(folder_name, photo_id)
    except ArchiveNotFoundError:
        return redirect(url_for("events.archive_detail", folder_name=folder_name))
    except ArchiveStorageError:
        LOGGER.exception("Could not serve archived photo %s/%s", folder_name, photo_id)
        raise
    response = send_file(
        path,
        mimetype=str(record.get("media_type") or "application/octet-stream"),
        conditional=True,
        etag=str(record.get("sha256") or "") or False,
        last_modified=path.stat().st_mtime,
        max_age=86400,
    )
    response.headers["Cache-Control"] = "private, max-age=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@events_blueprint.get("/api/events/current")
def current_event_api():
    event = _service().get_active_event()
    active_event = EventService.public_event(event)
    metrics = _dashboard_metrics(event)
    return jsonify({
        "active_event": active_event,
        "photo_count": metrics["photo_count"],
        "guest_count": metrics["guest_count"],
    })
