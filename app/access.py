from __future__ import annotations

import io
import logging
from typing import Any

from flask import Blueprint, current_app, make_response, redirect, render_template, request, send_file, url_for

from app.access_service import AccessService, AccessStorageError, AccessValidationError
from app.event_service import EventService
from app.photo_service import PhotoService, PhotoStorageError
from app.security import operator_only
from app.utils import select_local_network

LOGGER = logging.getLogger("arcobaleno.access.routes")
access_blueprint = Blueprint("access", __name__)

_NOTICE_MESSAGES = {
    "saved": "Wi-Fi details saved. Your QR codes are ready.",
    "wifi-required": "Save valid Wi-Fi details before opening the print layout.",
    "network-required": "Connect the laptop to the reserved event network before opening the print layout.",
    "allowances-current": "Everyone already has their full current shot allowance.",
    "allowances-unlimited": "Guest uploads are unlimited, so there is no allowance to reset.",
}


def _event_service() -> EventService:
    return current_app.extensions["arcobaleno_event_service"]


def _access_service() -> AccessService:
    return current_app.extensions["arcobaleno_access_service"]


def _photo_service() -> PhotoService:
    return current_app.extensions["arcobaleno_photo_service"]


def _checkpoint(event: dict[str, Any], reason: str) -> None:
    try:
        current_app.extensions["arcobaleno_recovery_service"].create_automatic_checkpoint(event, reason)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not create automatic recovery checkpoint (%s)", reason, exc_info=True)


def _network_state() -> dict[str, Any]:
    startup_ip = str(current_app.config["ARCOBALENO_NETWORK_IP"])
    preferred_ip = str(current_app.config.get("ARCOBALENO_PREFERRED_IP") or "").strip()
    selection = select_local_network(preferred_ip)
    warnings: list[str] = []

    if preferred_ip:
        if not selection.preferred_available:
            alternate = selection.detected_ip if not selection.detected_ip.startswith("127.") else "none"
            warnings.append(
                f"Reserved event server {preferred_ip} is not currently assigned to this laptop. "
                f"ARCOBALENO will not substitute a VPN or virtual-adapter address in guest QR codes. "
                f"Detected alternate address: {alternate}."
            )
    elif not selection.ready:
        warnings.append(
            "No usable event-network address was detected. Connect the laptop to the event router, then restart ARCOBALENO."
        )
    elif selection.selected_ip != startup_ip:
        warnings.append(
            f"The laptop network changed after ARCOBALENO started ({startup_ip} → {selection.selected_ip}). "
            "Restart ARCOBALENO before printing QR cards."
        )

    ready = selection.ready and not warnings
    return {
        "startup_ip": startup_ip,
        "preferred_ip": preferred_ip,
        "candidates": selection.candidates,
        "detected_ip": selection.detected_ip,
        "using_reserved_ip": bool(preferred_ip),
        "warning": " ".join(warnings),
        "ready": ready,
    }


def _submitted_wifi() -> dict[str, Any]:
    return {
        "ssid": request.form.get("ssid", ""),
        "security": request.form.get("security", "WPA"),
        "password": request.form.get("password", ""),
        "hidden": request.form.get("hidden") == "true",
    }


def _render_access(
    *,
    event: dict[str, Any],
    wifi: dict[str, Any],
    errors: dict[str, str] | None = None,
    notice: str | None = None,
    status_code: int = 200,
):
    access_service = _access_service()
    network_state = _network_state()
    allowance_summary = _photo_service().allowance_summary(str(event["id"]))
    response = make_response(
        render_template(
            "access.html",
            page_title="Guests",
            active_event=EventService.public_event(event),
            wifi=wifi,
            wifi_ready=access_service.wifi_ready(wifi),
            errors=errors or {},
            notice=notice,
            network_state=network_state,
            allowance_summary=allowance_summary,
        ),
        status_code,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@access_blueprint.get("/access")
@operator_only
def manage_access():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))

    try:
        settings = _access_service().get_settings(str(event["id"]))
    except AccessStorageError:
        LOGGER.exception("Could not read event-access settings")
        raise

    notice_key = request.args.get("notice", "")
    notice = _NOTICE_MESSAGES.get(notice_key)
    if notice_key == "allowances-reset":
        try:
            devices_reset = max(0, int(request.args.get("devices", "0")))
            photo_limit = max(0, int(request.args.get("limit", str(_photo_service().photo_limit))))
        except ValueError:
            devices_reset = 0
            photo_limit = int(_photo_service().photo_limit)
        noun = "device" if devices_reset == 1 else "devices"
        follow_up = (
            f"That device now has {photo_limit} shots available again."
            if devices_reset == 1
            else f"Each device now has {photo_limit} shots available again."
        )
        notice = f"Shot allowances reset for {devices_reset} guest {noun}. {follow_up}"
    return _render_access(event=event, wifi=dict(settings["wifi"]), notice=notice)


@access_blueprint.post("/access/allowances/reset")
@operator_only
def reset_guest_allowances():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))

    try:
        result = _photo_service().reset_guest_allowances(str(event["id"]))
    except PhotoStorageError:
        LOGGER.exception("Could not reset guest shot allowances")
        raise

    if bool(result.get("unlimited")):
        return redirect(url_for("access.manage_access", notice="allowances-unlimited"))
    if not bool(result.get("changed")):
        return redirect(url_for("access.manage_access", notice="allowances-current"))

    _checkpoint(event, "guest-shot-allowances-reset")
    return redirect(
        url_for(
            "access.manage_access",
            notice="allowances-reset",
            devices=int(result.get("devices_reset") or 0),
            limit=int(result.get("photo_limit") or 0),
        )
    )


@access_blueprint.post("/access/settings")
@operator_only
def save_access_settings():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))

    submitted = _submitted_wifi()
    try:
        _access_service().save_settings(str(event["id"]), submitted)
    except AccessValidationError as error:
        return _render_access(
            event=event,
            wifi=submitted,
            errors=error.errors,
            status_code=400,
        )
    except AccessStorageError:
        LOGGER.exception("Could not save event-access settings")
        raise

    return redirect(url_for("access.manage_access", notice="saved"))


@access_blueprint.get("/access/qr/<kind>.png")
@operator_only
def qr_image(kind: str):
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))

    settings = _access_service().get_settings(str(event["id"]))
    payloads = {
        "camera": str(current_app.config["ARCOBALENO_CAMERA_URL"]),
        "slideshow": str(current_app.config["ARCOBALENO_NETWORK_SLIDESHOW_URL"]),
    }

    if kind == "wifi":
        try:
            payload = _access_service().wifi_payload(settings["wifi"])
        except AccessValidationError:
            return make_response("Wi-Fi details are incomplete.", 409)
    elif kind in payloads:
        payload = payloads[kind]
    else:
        return make_response("Unknown QR code.", 404)

    image_bytes = _access_service().qr_png(payload)
    event_slug = _access_service().filename_slug(str(event["title"]))
    filename = f"{event_slug}-{kind}-qr.png"
    response = send_file(
        io.BytesIO(image_bytes),
        mimetype="image/png",
        as_attachment=request.args.get("download") == "1",
        download_name=filename,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@access_blueprint.get("/access/print")
@operator_only
def print_access_cards():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("events.home"))

    settings = _access_service().get_settings(str(event["id"]))
    wifi = dict(settings["wifi"])
    if not _access_service().wifi_ready(wifi):
        return redirect(url_for("access.manage_access", notice="wifi-required"))

    network_state = _network_state()
    if not network_state["ready"]:
        return redirect(url_for("access.manage_access", notice="network-required"))
    response = make_response(
        render_template(
            "access_print.html",
            page_title=f"{event['title']} QR Cards",
            active_event=EventService.public_event(event),
            wifi=wifi,
            network_state=network_state,
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
