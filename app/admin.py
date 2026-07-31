from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, redirect, render_template, request, send_file, url_for

from app.event_service import EventService
from app.event_control_service import EventControlError, EventControlService
from app.recovery_service import RecoveryError, RecoveryService
from app.runtime_control_service import RuntimeControlError, RuntimeControlService
from app.readiness_service import ReadinessService
from app.security import operator_only
from app.live_service import LiveEventService
from app.settings_service import SettingsService, SettingsStorageError, SettingsValidationError

LOGGER = logging.getLogger("arcobaleno.admin.routes")
admin_blueprint = Blueprint("admin", __name__)


def _settings_service() -> SettingsService:
    return current_app.extensions["arcobaleno_settings_service"]


def _recovery_service() -> RecoveryService:
    return current_app.extensions["arcobaleno_recovery_service"]


def _event_service() -> EventService:
    return current_app.extensions["arcobaleno_event_service"]


def _event_control_service() -> EventControlService:
    return current_app.extensions["arcobaleno_event_control_service"]


def _readiness_service() -> ReadinessService:
    return current_app.extensions["arcobaleno_readiness_service"]


def _live_service() -> LiveEventService:
    return current_app.extensions["arcobaleno_live_event_service"]


def _runtime_control_service() -> RuntimeControlService:
    return current_app.extensions["arcobaleno_runtime_control_service"]


def _server_state_service():
    return current_app.extensions["arcobaleno_server_state_service"]


@admin_blueprint.get("/about")
@operator_only
def about_page():
    return render_template(
        "about.html",
        page_title="About ARCOBALENO",
    )


def _submitted_settings() -> dict[str, Any]:
    return {
        "photo_limit_per_device": request.form.get("photo_limit_per_device", ""),
        "max_upload_megabytes": request.form.get("max_upload_megabytes", ""),
        "seconds_per_photo": request.form.get("seconds_per_photo", ""),
        "poll_interval_seconds": request.form.get("poll_interval_seconds", ""),
        "show_guest_name": request.form.get("show_guest_name", ""),
        "show_timestamp": request.form.get("show_timestamp", ""),
        "preferred_ip": request.form.get("preferred_ip", ""),
        "open_browser": request.form.get("open_browser", ""),
    }


@admin_blueprint.get("/settings")
@operator_only
def settings_page():
    notice = request.args.get("notice", "")
    return render_template(
        "settings.html",
        form_data=_settings_service().form_values(),
        errors={},
        notice=notice,
        page_title="Application settings",
    )


@admin_blueprint.post("/settings")
@operator_only
def save_settings():
    form_data = _submitted_settings()
    try:
        cleaned, restart_required = _settings_service().save(form_data)
    except SettingsValidationError as error:
        return render_template(
            "settings.html",
            form_data=form_data,
            errors=error.errors,
            notice="",
            page_title="Application settings",
        ), 400
    except SettingsStorageError:
        LOGGER.exception("Could not save application settings")
        raise

    photo_service = current_app.extensions["arcobaleno_photo_service"]
    photo_service.photo_limit = int(cleaned["photo_limit_per_device"])
    photo_service.max_upload_bytes = int(cleaned["max_upload_megabytes"]) * 1024 * 1024
    current_app.config["MAX_CONTENT_LENGTH"] = photo_service.max_upload_bytes + (1024 * 1024)

    notice = "saved-restart" if restart_required else "saved"
    return redirect(url_for("admin.settings_page", notice=notice))


@admin_blueprint.get("/recovery")
@operator_only
def recovery_page():
    active_event = _event_service().get_active_event()
    return render_template(
        "recovery.html",
        active_event=active_event,
        diagnostics=_recovery_service().diagnostics(),
        backups=_recovery_service().list_backups(),
        notice=request.args.get("notice", ""),
        page_title="Recovery and diagnostics",
    )


@admin_blueprint.post("/recovery/backup")
@operator_only
def create_backup():
    active_event = _event_service().get_active_event()
    if active_event is None:
        return redirect(url_for("admin.recovery_page", notice="no-event"))
    try:
        _recovery_service().create_backup(active_event)
    except RecoveryError:
        LOGGER.exception("Could not create a manual recovery snapshot")
        raise
    return redirect(url_for("admin.recovery_page", notice="backup-created"))


@admin_blueprint.get("/recovery/backups/<filename>")
@operator_only
def download_backup(filename: str):
    try:
        path = _recovery_service().backup_file(filename)
    except RecoveryError:
        return redirect(url_for("admin.recovery_page"))
    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=path.name,
        conditional=True,
    )


@admin_blueprint.get("/recovery/log")
@operator_only
def download_log():
    try:
        path = _recovery_service().log_file()
    except RecoveryError:
        return redirect(url_for("admin.recovery_page"))
    return send_file(
        path,
        mimetype="text/plain",
        as_attachment=True,
        download_name="arcobaleno.log",
        conditional=True,
    )


@admin_blueprint.get("/readiness")
@operator_only
def readiness_page():
    event = _event_service().get_active_event()
    access_settings = None
    access_ready = False
    control = None
    if event is not None:
        event_id = str(event["id"])
        access_service = current_app.extensions["arcobaleno_access_service"]
        try:
            access_settings = access_service.get_settings(event_id)
            access_ready = access_service.wifi_ready(access_settings.get("wifi", {}))
        except Exception:  # noqa: BLE001
            LOGGER.exception("Could not inspect event-access readiness")
        try:
            control = _event_control_service().get(event_id)
        except EventControlError:
            LOGGER.exception("Could not inspect event controls")

    report = _readiness_service().run(
        active_event=event,
        network_ip=current_app.config["ARCOBALENO_NETWORK_IP"],
        preferred_ip=current_app.config["ARCOBALENO_PREFERRED_IP"],
        network_candidates=list(current_app.config["ARCOBALENO_NETWORK_CANDIDATES"]),
        network_ready=bool(current_app.config["ARCOBALENO_NETWORK_READY"]),
        access_settings=access_settings,
        access_ready=access_ready,
        control=control,
    )
    return render_template(
        "readiness.html",
        active_event=event,
        control=control,
        report=report,
        notice=request.args.get("notice", ""),
        page_title="Event readiness",
    )


@admin_blueprint.post("/event-control/uploads")
@operator_only
def update_upload_control():
    event = _event_service().get_active_event()
    if event is None:
        return redirect(url_for("admin.readiness_page"))

    uploads_open = request.form.get("uploads_open") == "true"
    pause_message = request.form.get("pause_message", "")
    try:
        control = _event_control_service().save(
            str(event["id"]),
            uploads_open=uploads_open,
            pause_message=pause_message,
        )
    except EventControlError:
        LOGGER.exception("Could not update guest upload controls")
        raise

    _live_service().publish(str(event["id"]), "control.updated", {"control": control})
    notice = "uploads-open" if uploads_open else "uploads-paused"
    return redirect(url_for("admin.readiness_page", notice=notice))

@admin_blueprint.post("/system/shutdown")
@operator_only
def shutdown_application():
    runtime_control = _runtime_control_service()
    if not runtime_control.available:
        return render_template(
            "error.html",
            page_title="Safe shutdown unavailable",
            error_code="503",
            error_title="Safe shutdown unavailable",
            error_message=(
                "This development runtime cannot stop itself safely. Close the black "
                "server window with Ctrl+C instead."
            ),
        ), 503

    state_service = _server_state_service()
    active_event = _event_service().get_active_event()
    if active_event is not None:
        try:
            _recovery_service().create_automatic_checkpoint(active_event, "safe-shutdown")
        except Exception:  # noqa: BLE001
            LOGGER.warning("Could not create the final automatic checkpoint before shutdown", exc_info=True)
    try:
        state_service.mark_stopping("operator-interface")
        shutdown_status = runtime_control.request_shutdown()
    except (OSError, RuntimeControlError):
        try:
            state_service.cancel_stopping()
        except OSError:
            LOGGER.exception("Could not restore the running server state after shutdown failed")
        LOGGER.exception("Could not begin safe ARCOBALENO shutdown")
        raise

    return render_template(
        "shutdown.html",
        active_event=EventService.public_event(active_event),
        shutdown_status=shutdown_status,
        page_title="Shutting down ARCOBALENO",
    ), 202

