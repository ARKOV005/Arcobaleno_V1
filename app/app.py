from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from app import __version__
from app.access import access_blueprint
from app.access_service import AccessService
from app.admin import admin_blueprint
from app.archive_service import ArchiveService
from app.camera import camera_blueprint
from app.event_service import EventService
from app.event_control_service import EventControlService
from app.events import events_blueprint
from app.photo_service import PhotoService
from app.live_service import LiveEventService
from app.timeline_service import TimelineService
from app.production import production_blueprint
from app.recovery_service import RecoveryService
from app.runtime_control_service import RuntimeControlService
from app.readiness_service import ReadinessService
from app.settings_service import SettingsService
from app.security import initialize_security
from app.slideshow import slideshow_blueprint
from app.storage_safety_service import StorageSafetyService
from app.server_state_service import ServerStateService
from app.utils import build_http_url, select_local_network


def _pretty_date(value: Any) -> str:
    if not value:
        return "Not set"
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%B %d, %Y").replace(" 0", " ")


def create_app(
    project_root: Path,
    config: dict[str, Any],
    runtime_root: Path | None = None,
) -> Flask:
    project_root = Path(project_root)
    runtime_root = Path(runtime_root or project_root)

    application = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )

    server_config = config["server"]
    camera_config = config["camera"]
    port = server_config["port"]
    preferred_ip = str(server_config.get("preferred_ip", "")).strip()
    network_selection = select_local_network(preferred_ip)
    network_ip = network_selection.selected_ip
    detected_ip = network_selection.detected_ip
    network_ready = network_selection.ready
    operator_url = build_http_url("127.0.0.1", port)
    network_url = build_http_url(network_ip, port)
    max_upload_bytes = int(camera_config["max_upload_megabytes"]) * 1024 * 1024
    safety_config = config["safety"]
    storage_safety_service = StorageSafetyService(
        runtime_root=runtime_root,
        warning_free_bytes=int(float(safety_config["warning_free_gigabytes"]) * (1024 ** 3)),
        critical_free_bytes=int(float(safety_config["critical_free_gigabytes"]) * (1024 ** 3)),
        upload_reserve_bytes=int(safety_config["upload_reserve_megabytes"]) * 1024 * 1024,
    )
    server_state_service = ServerStateService(runtime_root=runtime_root)

    camera_url = f"{network_url}/camera"
    slideshow_url = f"{operator_url}/slideshow"
    network_slideshow_url = f"{network_url}/slideshow"
    access_url = f"{operator_url}/access"

    runtime_path_text = str(runtime_root).casefold()
    if "programdata" in runtime_path_text or "\\programdata\\" in runtime_path_text:
        runtime_mode = "Installed edition"
    elif runtime_root.resolve() != project_root.resolve():
        runtime_mode = "Portable edition"
    else:
        runtime_mode = "Source edition"

    application.config.update(
        ARCOBALENO_CONFIG=config,
        ARCOBALENO_RUNTIME_ROOT=runtime_root,
        ARCOBALENO_RUNTIME_MODE=runtime_mode,
        ARCOBALENO_OPERATOR_URL=operator_url,
        ARCOBALENO_NETWORK_URL=network_url,
        ARCOBALENO_NETWORK_IP=network_ip,
        ARCOBALENO_DETECTED_IP=detected_ip,
        ARCOBALENO_NETWORK_READY=network_ready,
        ARCOBALENO_PREFERRED_IP=preferred_ip,
        ARCOBALENO_NETWORK_CANDIDATES=network_selection.candidates,
        ARCOBALENO_CAMERA_URL=camera_url,
        ARCOBALENO_SLIDESHOW_URL=slideshow_url,
        ARCOBALENO_NETWORK_SLIDESHOW_URL=network_slideshow_url,
        ARCOBALENO_ACCESS_URL=access_url,
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=max_upload_bytes + (1024 * 1024),
    )

    initialize_security(application, runtime_root)
    event_service = EventService(runtime_root)
    photo_service = PhotoService(
        runtime_root=runtime_root,
        max_upload_bytes=max_upload_bytes,
        photo_limit=int(camera_config["photo_limit_per_device"]),
    )
    removed_temporary_files = photo_service.cleanup_temporary_files()
    if removed_temporary_files:
        logging.getLogger("arcobaleno.storage").warning(
            "Removed %s abandoned temporary upload file(s) during startup",
            removed_temporary_files,
        )
    access_service = AccessService(runtime_root=runtime_root, defaults=config["access"])
    archive_service = ArchiveService(runtime_root=runtime_root)
    settings_service = SettingsService(project_root=runtime_root, config=config)
    recovery_service = RecoveryService(runtime_root=runtime_root)
    event_control_service = EventControlService(runtime_root=runtime_root)
    live_event_service = LiveEventService()
    timeline_service = TimelineService(runtime_root=runtime_root)
    runtime_control_service = RuntimeControlService()
    readiness_service = ReadinessService(
        runtime_root=runtime_root,
        storage_safety_service=storage_safety_service,
        server_state_service=server_state_service,
    )
    application.extensions["arcobaleno_event_service"] = event_service
    application.extensions["arcobaleno_photo_service"] = photo_service
    application.extensions["arcobaleno_access_service"] = access_service
    application.extensions["arcobaleno_archive_service"] = archive_service
    application.extensions["arcobaleno_settings_service"] = settings_service
    application.extensions["arcobaleno_recovery_service"] = recovery_service
    application.extensions["arcobaleno_event_control_service"] = event_control_service
    application.extensions["arcobaleno_live_event_service"] = live_event_service
    application.extensions["arcobaleno_timeline_service"] = timeline_service
    application.extensions["arcobaleno_readiness_service"] = readiness_service
    application.extensions["arcobaleno_storage_safety_service"] = storage_safety_service
    application.extensions["arcobaleno_server_state_service"] = server_state_service
    application.extensions["arcobaleno_runtime_control_service"] = runtime_control_service
    application.register_blueprint(events_blueprint)
    application.register_blueprint(camera_blueprint)
    application.register_blueprint(slideshow_blueprint)
    application.register_blueprint(access_blueprint)
    application.register_blueprint(admin_blueprint)
    application.register_blueprint(production_blueprint)
    application.add_template_filter(_pretty_date, "pretty_date")

    @application.template_filter("filesize")
    def _filesize(value: Any) -> str:
        try:
            size = float(value or 0)
        except (TypeError, ValueError):
            size = 0
        units = ("B", "KB", "MB", "GB", "TB")
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        precision = 0 if index == 0 else 2
        return f"{size:.{precision}f} {units[index]}"

    @application.context_processor
    def shared_template_values():
        nav_event = event_service.get_active_event()
        global_storage_status = storage_safety_service.status(max_upload_bytes)
        return {
            "app_name": config["application"]["name"],
            "version": __version__,
            "operator_url": operator_url,
            "network_url": network_url,
            "network_ip": network_ip,
            "detected_ip": detected_ip,
            "network_ready": network_ready,
            "camera_url": camera_url,
            "slideshow_url": slideshow_url,
            "network_slideshow_url": network_slideshow_url,
            "access_url": access_url,
            "preferred_ip": preferred_ip,
            "network_candidates": network_selection.candidates,
            "network_preferred_matches": not preferred_ip or network_selection.preferred_available,
            "nav_active_event": EventService.public_event(nav_event),
            "global_storage_status": global_storage_status,
            "runtime_root": str(runtime_root),
            "runtime_mode": runtime_mode,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    @application.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

    @application.get("/health")
    def health():
        event = event_service.get_active_event()
        photo_count = photo_service.total_photos(str(event["id"])) if event else 0
        try:
            control = event_control_service.get(str(event["id"])) if event else None
        except Exception:  # noqa: BLE001
            control = {"uploads_open": False} if event else None
        storage = storage_safety_service.status(max_upload_bytes)
        return jsonify(
            {
                "status": "ok",
                "application": config["application"]["name"],
                "version": __version__,
                "active_event": event is not None,
                "photo_count": photo_count,
                "uploads_open": bool(control.get("uploads_open", True)) if control else False,
                "storage_level": storage["level"],
                "storage_accepting_uploads": storage["accepting_uploads"],
                "storage_free_bytes": storage["free_bytes"],
            }
        )

    @application.get("/api/status")
    def api_status():
        event = event_service.get_active_event()
        active_event = EventService.public_event(event)
        photo_count = photo_service.total_photos(str(event["id"])) if event else 0
        try:
            control = event_control_service.get(str(event["id"])) if event else None
        except Exception:  # noqa: BLE001
            control = {"uploads_open": False} if event else None
        storage = storage_safety_service.status(max_upload_bytes)
        return jsonify(
            {
                "status": "online",
                "version": __version__,
                "operator_url": operator_url,
                "network_url": network_url,
                "network_ip": network_ip,
                "detected_ip": detected_ip,
                "network_ready": network_ready,
                "camera_url": camera_url,
                "slideshow_url": slideshow_url,
                "network_slideshow_url": network_slideshow_url,
                "access_url": access_url,
                "preferred_ip": preferred_ip,
                "network_candidates": network_selection.candidates,
                "active_event": active_event,
                "photo_count": photo_count,
                "uploads_open": bool(control.get("uploads_open", True)) if control else False,
                "storage": storage,
                "server_state": server_state_service.status(),
                "runtime_control": runtime_control_service.status(),
                "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )

    @application.errorhandler(400)
    def bad_request(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "ARCOBALENO could not verify that request."}), 400
        return render_template(
            "error.html",
            page_title="Request not accepted",
            error_code="400",
            error_title="Request not accepted",
            error_message=(
                "ARCOBALENO could not verify that request. Return to the dashboard "
                "and try the action again."
            ),
        ), 400

    @application.errorhandler(403)
    def forbidden(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Operator access is required."}), 403
        return render_template(
            "error.html",
            page_title="Operator access required",
            error_code="403",
            error_title="Operator access required",
            error_message=(
                "Event controls are available only on the laptop running ARCOBALENO."
            ),
        ), 403

    @application.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "The requested ARCOBALENO endpoint does not exist."}), 404
        return render_template(
            "error.html",
            page_title="Page not found",
            error_code="404",
            error_title="Page not found",
            error_message="The page you requested does not exist yet.",
        ), 404

    @application.errorhandler(413)
    def upload_too_large(_error):
        limit = config["camera"]["max_upload_megabytes"]
        if request.path.startswith("/api/"):
            return jsonify({"error": f"That photo is larger than {limit} MB."}), 413
        return render_template(
            "error.html",
            page_title="Photo too large",
            error_code="413",
            error_title="Photo too large",
            error_message=f"Choose a photo smaller than {limit} MB and try again.",
        ), 413

    @application.errorhandler(500)
    def internal_error(_error):
        logging.getLogger("arcobaleno").exception("Unhandled web request error")
        if request.path.startswith("/api/"):
            return jsonify({"error": "ARCOBALENO encountered an unexpected error."}), 500
        return render_template(
            "error.html",
            page_title="Something went wrong",
            error_code="500",
            error_title="Something went wrong",
            error_message=(
                "The error was recorded. Return to the ARCOBALENO dashboard and try again."
            ),
        ), 500

    return application
