from __future__ import annotations

import logging
import secrets
from typing import Any

from flask import Blueprint, current_app, jsonify, make_response, redirect, render_template, request, session, url_for, send_file

from app.event_service import EventService
from app.event_control_service import EventControlError, EventControlService
from app.storage_safety_service import StorageSafetyService
from app.live_service import LiveEventService
from app.timeline_service import TimelineError, TimelineService
from app.photo_service import (
    PhotoCapacityError,
    PhotoIntegrityError,
    PhotoLimitReachedError,
    PhotoNotFoundError,
    PhotoService,
    PhotoStorageError,
    PhotoValidationError,
)

LOGGER = logging.getLogger("arcobaleno.camera.routes")
camera_blueprint = Blueprint("camera", __name__)


def _event_service() -> EventService:
    return current_app.extensions["arcobaleno_event_service"]


def _photo_service() -> PhotoService:
    return current_app.extensions["arcobaleno_photo_service"]


def _event_control_service() -> EventControlService:
    return current_app.extensions["arcobaleno_event_control_service"]


def _storage_safety_service() -> StorageSafetyService:
    return current_app.extensions["arcobaleno_storage_safety_service"]


def _live_service() -> LiveEventService:
    return current_app.extensions["arcobaleno_live_event_service"]


def _timeline_service() -> TimelineService:
    return current_app.extensions["arcobaleno_timeline_service"]


def _checkpoint(event: dict[str, Any], reason: str) -> None:
    try:
        current_app.extensions["arcobaleno_recovery_service"].create_automatic_checkpoint(event, reason)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not create automatic recovery checkpoint (%s)", reason, exc_info=True)


def _device_id() -> str:
    # Keep the guest identity across browser restarts and repeat QR scans.
    # The server remains authoritative for the photo count; the cookie contains
    # only a signed random device identifier, never the remaining-shot value.
    session.permanent = True
    value = session.get("arcobaleno_guest_device_id")
    if not isinstance(value, str) or len(value) < 24:
        value = secrets.token_urlsafe(32)
        session["arcobaleno_guest_device_id"] = value
    return value


def _camera_status(event: dict[str, Any]) -> dict[str, Any]:
    event_id = str(event["id"])
    status = _photo_service().status(event_id, _device_id())
    try:
        control = _event_control_service().get(event_id)
    except EventControlError:
        LOGGER.exception("Could not read guest upload controls")
        control = {
            "uploads_open": False,
            "pause_message": "Photo uploads are temporarily unavailable. Please try again shortly.",
        }
    storage = _storage_safety_service().status(_photo_service().max_upload_bytes)
    try:
        public_timeline = _timeline_service().public_entries(event_id)
    except TimelineError:
        LOGGER.warning("Could not read the guest event timeline", exc_info=True)
        public_timeline = []
    return {
        **status,
        "event": EventService.public_event(event),
        "event_mode": str(control.get("event_mode") or "setup"),
        "uploads_open": bool(control.get("uploads_open", False)),
        "moderation_mode": str(control.get("moderation_mode") or "family"),
        "moderation_delay_seconds": int(control.get("moderation_delay_seconds") or 0),
        "pause_message": str(control.get("pause_message") or ""),
        "storage_accepting_uploads": bool(storage["accepting_uploads"]),
        "storage_level": str(storage["level"]),
        "storage_message": str(storage["message"]),
        "storage_free_bytes": int(storage["free_bytes"]),
        "timeline": public_timeline,
    }


@camera_blueprint.get("/camera")
def camera_page():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))

    response = make_response(
        render_template(
            "camera.html",
            page_title=f"{event['title']} Camera",
            active_event=EventService.public_event(event),
            camera_status=_camera_status(event),
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@camera_blueprint.get("/api/camera/status")
def camera_status_api():
    event = _event_service().get_active_event()
    if event is None:
        return jsonify({"error": "No event is active."}), 409
    return jsonify(_camera_status(event))


@camera_blueprint.post("/api/photos")
def upload_photo_api():
    event = _event_service().get_active_event()
    if event is None:
        return jsonify({"error": "The event has ended or is not active."}), 409

    try:
        control = _event_control_service().get(str(event["id"]))
    except EventControlError:
        LOGGER.exception("Could not read guest upload controls")
        return jsonify({"error": "Photo uploads are temporarily unavailable."}), 503
    if not bool(control.get("uploads_open", True)):
        return jsonify(
            {
                "error": str(control.get("pause_message") or "Photo uploads are temporarily paused."),
                "uploads_open": False,
            }
        ), 423

    photo = request.files.get("photo")
    if photo is None:
        return jsonify({"error": "Choose or take a photo before sending."}), 400

    storage = _storage_safety_service().status(_photo_service().max_upload_bytes)
    if not storage["accepting_uploads"]:
        return jsonify(
            {
                "error": storage["message"],
                "storage_accepting_uploads": False,
                "storage_level": storage["level"],
                "storage_message": storage["message"],
            }
        ), 507

    try:
        result = _photo_service().save_upload(
            event_id=str(event["id"]),
            stream=photo.stream,
            device_id=_device_id(),
            guest_name=request.form.get("guest_name", ""),
            client_upload_id=request.form.get("upload_id", ""),
            original_filename=photo.filename or "camera-photo",
            moderation_mode=str(control.get("moderation_mode") or "family"),
            moderation_delay_seconds=int(control.get("moderation_delay_seconds") or 0),
        )
    except PhotoIntegrityError as error:
        LOGGER.warning("Rejected unreadable guest photo for event %s: %s", event["id"], error)
        return jsonify({"error": str(error), "integrity_error": True}), 422
    except PhotoValidationError as error:
        return jsonify({"error": str(error)}), 400
    except PhotoLimitReachedError as error:
        return jsonify({"error": str(error), "remaining": 0, "allowed": False}), 429
    except PhotoCapacityError as error:
        storage = _storage_safety_service().status(_photo_service().max_upload_bytes)
        LOGGER.warning("Guest upload stopped by storage protection: %s", error)
        return jsonify(
            {
                "error": str(error),
                "storage_accepting_uploads": False,
                "storage_level": "critical",
                "storage_message": str(error),
                "storage_free_bytes": storage["free_bytes"],
            }
        ), 507
    except PhotoStorageError:
        LOGGER.exception("Could not store a guest photo")
        return jsonify(
            {"error": "ARCOBALENO could not save that photo. Please try again."}
        ), 500

    event_id = str(event["id"])
    if not result.get("duplicate"):
        counts = _photo_service().counts(event_id)
        _live_service().publish(
            event_id,
            "photo.uploaded",
            {
                "photo_id": result["id"],
                "guest_name": result.get("guest_name"),
                "created_at": result.get("created_at"),
                "moderation_status": result.get("moderation_status"),
                "counts": counts,
            },
        )
        milestone = None
        total = int(result.get("total_photos") or 0)
        if total == 1:
            milestone = ("First guest photo", "The first memory arrived in ARCOBALENO.")
        elif total in {25, 50, 100, 250, 500, 1000, 2000}:
            milestone = (f"{total} photos received", "Guests reached a new event photo milestone.")
        if milestone:
            try:
                entry = _timeline_service().append(
                    event_id,
                    title=milestone[0],
                    detail=milestone[1],
                    kind="milestone",
                    icon="camera",
                    public=False,
                    photo_id=result["id"],
                )
                _live_service().publish(event_id, "timeline.updated", {"entry": entry})
            except TimelineError:
                LOGGER.warning("Could not record photo milestone", exc_info=True)
        _checkpoint(event, "photo-uploaded")

    storage_after = _storage_safety_service().status(_photo_service().max_upload_bytes)
    return jsonify(
        {
            "ok": True,
            "photo_id": result["id"],
            "duplicate": result["duplicate"],
            "remaining": result["remaining"],
            "unlimited": result["unlimited"],
            "allowed": result["allowed"],
            "total_photos": result["total_photos"],
            "moderation_status": result.get("moderation_status", "approved"),
            "moderation_mode": result.get("moderation_mode", "instant"),
            "storage_accepting_uploads": storage_after["accepting_uploads"],
            "storage_level": storage_after["level"],
            "storage_message": storage_after["message"],
        }
    )


@camera_blueprint.get("/gallery")
def guest_gallery_page():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))
    mode = "mine" if request.args.get("view") == "mine" else "all"
    event_id = str(event["id"])
    device_hash = _photo_service()._device_hash(_device_id())
    before = request.args.get("before", "")[:50]
    # Fetch all records in a stable snapshot, filter moderation and ownership
    # before pagination to avoid leaking pending/hidden pictures.
    try:
        photos = _photo_service().list_photos(event_id)
    except PhotoStorageError:
        LOGGER.exception("Could not read guest gallery")
        return "Gallery is temporarily unavailable.", 503
    photos = [p for p in photos if p.get("moderation_status") == "approved"
              and (mode != "mine" or p.get("device_hash") == device_hash)]
    if before:
        photos = [p for p in photos if str(p.get("created_at") or "") < before]
    more = len(photos) > 48
    response = make_response(render_template(
        "guest_gallery.html",
        active_event=EventService.public_event(event),
        photos=photos[:48], more=more, view=mode,
        guest_downloads_enabled=bool(_event_control_service().get(event_id).get("guest_downloads_enabled", False)),
    ))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@camera_blueprint.get("/media/gallery/download/<photo_id>")
def guest_gallery_download(photo_id: str):
    event = _event_service().get_active_event()
    if event is None:
        return "", 404
    try:
        control = _event_control_service().get(str(event["id"]))
        record, path = _photo_service().photo_file(str(event["id"]), photo_id)
    except (PhotoNotFoundError, EventControlError):
        return "", 404
    except PhotoStorageError:
        return "", 503
    if not control.get("guest_downloads_enabled", False) or record.get("moderation_status") != "approved":
        return "", 403
    response = send_file(path, mimetype=str(record["media_type"]), as_attachment=True,
                         download_name=path.name, conditional=True)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@camera_blueprint.get("/media/gallery/thumb/<photo_id>")
def guest_gallery_thumbnail(photo_id: str):
    event = _event_service().get_active_event()
    if event is None:
        return "", 404
    try:
        record, path = _photo_service().thumbnail_file(str(event["id"]), photo_id)
    except PhotoNotFoundError:
        return "", 404
    except PhotoStorageError:
        LOGGER.exception("Guest gallery thumbnail failure")
        return "", 503
    if record.get("moderation_status") != "approved":
        return "", 404
    response = send_file(path, mimetype="image/jpeg", conditional=True)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=60"
    return response
