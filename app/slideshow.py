from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, jsonify, make_response, redirect, render_template, request, send_file, url_for

from app.event_service import EventService
from app.photo_service import PhotoNotFoundError, PhotoService, PhotoStorageError
from app.security import is_operator_request, operator_only
from app.event_control_service import EventControlError, EventControlService
from app.timeline_service import TimelineError, TimelineService
from app.live_service import LiveEventService

LOGGER = logging.getLogger("arcobaleno.slideshow.routes")
slideshow_blueprint = Blueprint("slideshow", __name__)


def _event_service() -> EventService:
    return current_app.extensions["arcobaleno_event_service"]


def _photo_service() -> PhotoService:
    return current_app.extensions["arcobaleno_photo_service"]


def _slideshow_config() -> dict[str, Any]:
    return current_app.config["ARCOBALENO_CONFIG"]["slideshow"]


def _control_service() -> EventControlService:
    return current_app.extensions["arcobaleno_event_control_service"]


def _timeline_service() -> TimelineService:
    return current_app.extensions["arcobaleno_timeline_service"]


def _live_service() -> LiveEventService:
    return current_app.extensions["arcobaleno_live_event_service"]


def _cursor_value() -> int:
    raw_value = request.args.get("cursor", "0")
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


@slideshow_blueprint.get("/display")
@operator_only
def display_manager_page():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))

    event_id = str(event["id"])
    try:
        control = _control_service().get(event_id)
    except EventControlError:
        LOGGER.exception("Could not read controls for the display manager")
        control = {"slideshow_state": "playing"}

    return render_template(
        "display_manager.html",
        active_event=EventService.public_event(event),
        display_status=_live_service().display_status(event_id),
        event_control=control,
        approved_photo_count=_photo_service().counts(event_id)["approved"],
        page_title="Display",
    )


@slideshow_blueprint.get("/slideshow")
def slideshow_page():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))

    settings = _slideshow_config()
    event_id = str(event["id"])
    display_mode = request.args.get("display", "network")
    if display_mode not in {"wired", "network", "preview"}:
        display_mode = "network"
    response = make_response(
        render_template(
            "slideshow.html",
            page_title=f"{event['title']} Slideshow",
            active_event=EventService.public_event(event),
            photo_count=_photo_service().counts(event_id)["approved"],
            slideshow_settings=settings,
            display_mode=display_mode,
            request_fullscreen=request.args.get("fullscreen") == "1",
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@slideshow_blueprint.get("/api/slideshow/feed")
def slideshow_feed_api():
    event = _event_service().get_active_event()
    if event is None:
        response = jsonify(
            {
                "active": False,
                "error": "The event has ended or is not active.",
                "photos": [],
                "cursor": 0,
                "total_photos": 0,
            }
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response, 409

    try:
        feed = _photo_service().slideshow_feed(
            event_id=str(event["id"]),
            cursor=_cursor_value(),
        )
    except PhotoStorageError:
        LOGGER.exception("Could not read the slideshow photo feed")
        return jsonify({"error": "ARCOBALENO could not read the slideshow feed."}), 500

    for photo in feed["photos"]:
        photo["url"] = url_for("slideshow.slideshow_photo", photo_id=photo["id"])

    try:
        control = _control_service().get(str(event["id"]))
        timeline = _timeline_service().public_entries(str(event["id"]))
        featured = _timeline_service().get(str(event["id"]), str(control.get("featured_marker_id") or ""))
    except (EventControlError, TimelineError):
        LOGGER.exception("Could not read slideshow production controls")
        control = {"slideshow_state": "playing", "slideshow_command_sequence": 0}
        timeline = []
        featured = None

    response = jsonify(
        {
            "active": True,
            "event": EventService.public_event(event),
            "settings": _slideshow_config(),
            "control": control,
            "timeline": timeline,
            "featured_marker": featured,
            **feed,
        }
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@slideshow_blueprint.get("/media/slideshow/<photo_id>")
def slideshow_photo(photo_id: str):
    event = _event_service().get_active_event()
    if event is None:
        return jsonify({"error": "The event has ended or is not active."}), 409

    try:
        record, photo_path = _photo_service().photo_file(
            event_id=str(event["id"]),
            photo_id=photo_id,
        )
    except PhotoNotFoundError:
        return jsonify({"error": "That slideshow photo was not found."}), 404
    except PhotoStorageError:
        LOGGER.exception("Could not serve slideshow photo %s", photo_id)
        return jsonify({"error": "ARCOBALENO could not read that photo."}), 500

    if str(record.get("moderation_status") or "approved") != "approved" and not is_operator_request():
        return jsonify({"error": "That slideshow photo is not available."}), 404

    response = send_file(
        photo_path,
        mimetype=str(record["media_type"]),
        conditional=True,
        etag=str(record["sha256"]),
        last_modified=photo_path.stat().st_mtime,
        max_age=86400,
    )
    response.headers["Cache-Control"] = "private, max-age=86400, immutable"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.args.get("download") == "1":
        response.headers["Content-Disposition"] = f'attachment; filename="{photo_path.name}"'
    return response
