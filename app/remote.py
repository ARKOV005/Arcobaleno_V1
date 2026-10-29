from __future__ import annotations

import io
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

import qrcode
from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.event_control_service import EventControlError
from app.event_service import EventService, EventStorageError, NoActiveEventError
from app.photo_service import PhotoNotFoundError, PhotoStorageError, PhotoValidationError
from app.remote_access_service import RemoteAccessError, RemoteAccessService
from app.security import (
    clear_remote_operator_authentication,
    is_local_network_request,
    is_remote_operator_authenticated,
    mark_remote_operator_authenticated,
    operator_only,
    remote_operator_only,
)
from app.timeline_service import TimelineError

LOGGER = logging.getLogger("arcobaleno.remote.routes")
remote_blueprint = Blueprint("remote", __name__)
_LOGIN_LOCK = threading.RLock()
_LOGIN_FAILURES: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=12))
_LOGIN_WINDOW_SECONDS = 600
_LOGIN_MAX_FAILURES = 5


def _remote_service() -> RemoteAccessService:
    return current_app.extensions["arcobaleno_remote_access_service"]


def _event_service():
    return current_app.extensions["arcobaleno_event_service"]


def _photo_service():
    return current_app.extensions["arcobaleno_photo_service"]


def _control_service():
    return current_app.extensions["arcobaleno_event_control_service"]


def _live_service():
    return current_app.extensions["arcobaleno_live_event_service"]


def _timeline_service():
    return current_app.extensions["arcobaleno_timeline_service"]


def _storage_service():
    return current_app.extensions["arcobaleno_storage_safety_service"]


def _server_state_service():
    return current_app.extensions["arcobaleno_server_state_service"]


def _remote_config() -> dict[str, Any]:
    return current_app.config["ARCOBALENO_CONFIG"]["remote"]


def _remote_url() -> str:
    return f"{current_app.config['ARCOBALENO_NETWORK_URL']}/remote"


def _require_remote_enabled() -> None:
    if not bool(_remote_service().get().get("enabled")):
        abort(403)


def _client_key() -> str:
    return str(request.remote_addr or "unknown")


def _login_locked(client: str) -> tuple[bool, int]:
    now = time.monotonic()
    with _LOGIN_LOCK:
        failures = _LOGIN_FAILURES[client]
        while failures and now - failures[0] > _LOGIN_WINDOW_SECONDS:
            failures.popleft()
        if len(failures) < _LOGIN_MAX_FAILURES:
            return False, 0
        remaining = max(1, int(_LOGIN_WINDOW_SECONDS - (now - failures[0])))
        return True, remaining


def _record_login_failure(client: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES[client].append(time.monotonic())


def _clear_login_failures(client: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(client, None)


def _control_payload(control: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "event_mode",
        "uploads_open",
        "guest_downloads_enabled",
        "pause_message",
        "moderation_mode",
        "moderation_delay_seconds",
        "slideshow_state",
        "slideshow_command_sequence",
        "live_started_at",
        "live_stopped_at",
        "updated_at",
    )
    return {key: control.get(key) for key in keys}


def _snapshot() -> dict[str, Any]:
    event = _event_service().get_active_event()
    storage = _storage_service().status(int(_photo_service().max_upload_bytes))
    base: dict[str, Any] = {
        "active": event is not None,
        "version": current_app.config.get("ARCOBALENO_VERSION", ""),
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "network_ip": current_app.config["ARCOBALENO_NETWORK_IP"],
        "network_ready": bool(current_app.config["ARCOBALENO_NETWORK_READY"]),
        "storage": storage,
        "server_state": _server_state_service().status(),
    }
    if event is None:
        return base

    event_id = str(event["id"])
    photo_service = _photo_service()
    control = _control_service().get(event_id)
    counts = photo_service.counts(event_id)
    allowance = photo_service.allowance_summary(event_id)
    displays = _live_service().display_status(event_id)
    latest = photo_service.latest_photo(event_id)
    alerts: list[dict[str, str]] = []
    if storage.get("level") != "healthy":
        alerts.append({"level": str(storage.get("level") or "warning"), "message": str(storage.get("message") or "Storage needs attention.")})
    if control.get("event_mode") == "live" and not control.get("uploads_open"):
        alerts.append({"level": "warning", "message": "The event is live, but guest uploads are paused."})
    if control.get("event_mode") == "live" and int(displays.get("total") or 0) == 0:
        alerts.append({"level": "warning", "message": "No slideshow display is currently connected."})

    latest_payload = None
    if latest:
        latest_payload = {
            "id": str(latest.get("id") or ""),
            "guest_name": str(latest.get("guest_name") or "Guest"),
            "created_at": str(latest.get("created_at") or ""),
            "moderation_status": str(latest.get("moderation_status") or "approved"),
            "favorite": bool(latest.get("favorite")),
            "thumbnail_url": url_for("remote.remote_thumbnail", photo_id=str(latest.get("id") or "")),
        }

    base.update(
        {
            "event": EventService.public_event(event),
            "control": _control_payload(control),
            "counts": counts,
            "photo_count": counts["total"],
            "guest_count": photo_service.unique_guest_count(event_id),
            "allowance": allowance,
            "displays": displays,
            "latest_photo": latest_payload,
            "alerts": alerts,
        }
    )
    return base


def _publish_control(event_id: str, control: dict[str, Any]) -> None:
    _live_service().publish(event_id, "control.updated", {"control": _control_payload(control)})


def _checkpoint(event: dict[str, Any], reason: str) -> None:
    try:
        current_app.extensions["arcobaleno_recovery_service"].create_automatic_checkpoint(event, reason)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not create remote-action checkpoint (%s)", reason, exc_info=True)


@remote_blueprint.get("/remote")
def remote_home():
    if not is_local_network_request():
        abort(403)
    settings = _remote_service().get()
    if not settings.get("enabled"):
        return render_template(
            "remote_login.html",
            remote_enabled=False,
            error="Mobile operator access is disabled on the event laptop.",
            page_title="Operator Remote",
        ), 403
    if not is_remote_operator_authenticated():
        return redirect(url_for("remote.remote_login"))
    return render_template(
        "remote_dashboard.html",
        poll_interval_seconds=float(_remote_config().get("poll_interval_seconds", 3)),
        page_title="Operator Remote",
    )


@remote_blueprint.get("/remote/login")
def remote_login():
    if not is_local_network_request():
        abort(403)
    settings = _remote_service().get()
    if is_remote_operator_authenticated() and settings.get("enabled"):
        return redirect(url_for("remote.remote_home"))
    return render_template(
        "remote_login.html",
        remote_enabled=bool(settings.get("enabled")),
        error=request.args.get("error", ""),
        page_title="Operator Remote",
    )


@remote_blueprint.post("/remote/login")
def remote_login_submit():
    if not is_local_network_request():
        abort(403)
    client = _client_key()
    locked, remaining = _login_locked(client)
    if locked:
        return render_template(
            "remote_login.html",
            remote_enabled=True,
            error=f"Too many incorrect attempts. Try again in about {max(1, remaining // 60)} minute(s).",
            page_title="Operator Remote",
        ), 429
    try:
        verified = _remote_service().verify_pin(request.form.get("pin", ""))
    except RemoteAccessError:
        LOGGER.exception("Could not verify the mobile operator PIN")
        verified = False
    if not verified:
        _record_login_failure(client)
        return render_template(
            "remote_login.html",
            remote_enabled=bool(_remote_service().get().get("enabled")),
            error="That PIN is incorrect.",
            page_title="Operator Remote",
        ), 403
    _clear_login_failures(client)
    mark_remote_operator_authenticated(float(_remote_config().get("session_hours", 12)))
    return redirect(url_for("remote.remote_home"))


@remote_blueprint.post("/remote/logout")
def remote_logout():
    clear_remote_operator_authentication()
    return redirect(url_for("remote.remote_login"))


@remote_blueprint.get("/api/remote/snapshot")
@remote_operator_only
def remote_snapshot_api():
    _require_remote_enabled()
    try:
        payload = _snapshot()
    except (EventControlError, PhotoStorageError, RemoteAccessError):
        LOGGER.exception("Could not build the mobile operator snapshot")
        return jsonify({"error": "ARCOBALENO could not read the live event state."}), 500
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@remote_blueprint.get("/remote/media/thumbnail/<photo_id>")
@remote_operator_only
def remote_thumbnail(photo_id: str):
    _require_remote_enabled()
    event = _event_service().get_active_event()
    if event is None:
        abort(404)
    try:
        record, thumbnail = _photo_service().thumbnail_file(str(event["id"]), photo_id)
    except (PhotoNotFoundError, PhotoStorageError):
        abort(404)
    response = send_file(
        thumbnail,
        mimetype="image/jpeg",
        conditional=True,
        etag=f"remote-thumb-{record.get('sha256', '')}",
        max_age=60,
    )
    response.headers["Cache-Control"] = "private, max-age=60"
    return response


@remote_blueprint.post("/api/remote/control")
@remote_operator_only
def remote_control_api():
    _require_remote_enabled()
    event = _event_service().get_active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    event_id = str(event["id"])
    action = str(request.form.get("action") or "").strip().casefold()
    control = None
    try:
        if action == "event-live":
            control = _control_service().set_event_mode(event_id, "live")
            try:
                _timeline_service().append(event_id, title="Event started from Operator Remote", detail="Guest uploads and displays are live.", kind="system", icon="spark", public=False)
            except TimelineError:
                LOGGER.warning("Could not record the remote live-mode timeline entry", exc_info=True)
        elif action == "event-setup":
            control = _control_service().set_event_mode(event_id, "setup")
            try:
                _timeline_service().append(event_id, title="Setup Mode restored from Operator Remote", detail="Uploads are paused and the display returned to the logo screen.", kind="system", icon="spark", public=False)
            except TimelineError:
                LOGGER.warning("Could not record the remote setup-mode timeline entry", exc_info=True)
        elif action == "downloads-enable":
            control = _control_service().update(event_id, guest_downloads_enabled=True)
        elif action == "downloads-disable":
            control = _control_service().update(event_id, guest_downloads_enabled=False)
        elif action == "uploads-open":
            control = _control_service().update(event_id, uploads_open=True)
        elif action == "uploads-pause":
            control = _control_service().update(event_id, uploads_open=False)
        elif action.startswith("slideshow-"):
            value = action.removeprefix("slideshow-")
            if value in {"next", "previous", "restart"}:
                control = _control_service().send_slideshow_command(event_id, value)
            elif value in {"playing", "paused", "logo", "black", "instructions"}:
                control = _control_service().update(event_id, slideshow_state=value)
            else:
                return jsonify({"error": "That display command is not supported."}), 400
        elif action in {"photo-approve", "photo-hide", "photo-favorite", "photo-unfavorite"}:
            photo_id = str(request.form.get("photo_id") or "").strip()
            moderation_action = action.removeprefix("photo-")
            photo = _photo_service().moderate_photo(event_id, photo_id, moderation_action)
            counts = _photo_service().counts(event_id)
            _live_service().publish(
                event_id,
                "photo.updated",
                {"photo_id": photo_id, "action": moderation_action, "counts": counts},
            )
            _checkpoint(event, f"remote-photo-{moderation_action}")
            return jsonify({"ok": True, "snapshot": _snapshot()})
        elif action == "reset-allowances":
            result = _photo_service().reset_guest_allowances(event_id)
            _live_service().publish(event_id, "allowances.reset", {"allowance": result})
            _checkpoint(event, "remote-allowances-reset")
            return jsonify({"ok": True, "result": result, "snapshot": _snapshot()})
        elif action == "archive-event":
            if request.form.get("confirmation") != "ARCHIVE":
                return jsonify({"error": "Type ARCHIVE to confirm."}), 400
            _checkpoint(event, "before-remote-archive")
            archived = _event_service().end_event()
            _live_service().publish(event_id, "event.archived", {"event_id": event_id})
            return jsonify({"ok": True, "archived": EventService.public_event(archived), "snapshot": _snapshot()})
        else:
            return jsonify({"error": "That remote action is not supported."}), 400
    except (EventControlError, TimelineError, PhotoStorageError, PhotoValidationError, PhotoNotFoundError, EventStorageError, NoActiveEventError) as error:
        LOGGER.exception("Mobile operator action failed: %s", action)
        return jsonify({"error": str(error) or "ARCOBALENO could not complete that action."}), 500

    if control is not None:
        _publish_control(event_id, control)
        _checkpoint(event, f"remote-{action}")
    return jsonify({"ok": True, "control": _control_payload(control or {}), "snapshot": _snapshot()})


@remote_blueprint.get("/operator-remote/qr.png")
@operator_only
def operator_remote_qr():
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=3)
    qr.add_data(_remote_url())
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    response = make_response(buffer.getvalue())
    response.headers["Content-Type"] = "image/png"
    response.headers["Cache-Control"] = "no-store"
    return response


@remote_blueprint.post("/operator-remote/reset-pin")
@operator_only
def operator_remote_reset_pin():
    try:
        _remote_service().reset_pin()
    except RemoteAccessError:
        LOGGER.exception("Could not reset the mobile operator PIN")
        raise
    return redirect(url_for("events.home", notice="remote-pin-reset"))


@remote_blueprint.post("/operator-remote/toggle")
@operator_only
def operator_remote_toggle():
    enabled = request.form.get("enabled") == "true"
    try:
        _remote_service().set_enabled(enabled)
    except RemoteAccessError:
        LOGGER.exception("Could not change mobile operator access")
        raise
    return redirect(url_for("events.home", notice="remote-enabled" if enabled else "remote-disabled"))


@remote_blueprint.get("/api/remote/photos")
@remote_operator_only
def remote_photos_api():
    _require_remote_enabled()
    event = _event_service().get_active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    before = str(request.args.get("before") or "")[:50]
    try:
        photos = _photo_service().list_photos(str(event["id"]), limit=49, before=before)
    except PhotoStorageError:
        LOGGER.exception("Could not list remote photos")
        return jsonify({"error": "Could not load photos."}), 503
    more = len(photos) > 48
    photos = photos[:48]
    return jsonify({"photos": [{
        "id": str(p["id"]),
        "guest_name": str(p.get("guest_name") or "Guest"),
        "created_at": str(p.get("created_at") or ""),
        "moderation_status": str(p.get("moderation_status") or "approved"),
        "favorite": bool(p.get("favorite")),
        "thumbnail_url": url_for("remote.remote_thumbnail", photo_id=p["id"]),
    } for p in photos], "has_more": more,
      "next_before": str(photos[-1].get("created_at") or "") if more and photos else ""})
