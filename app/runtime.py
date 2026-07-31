from __future__ import annotations

import _thread
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from waitress import serve

from app import __version__
from app.app import create_app
from app.config import load_config
from app.event_service import EventService, EventStorageError
from app.logging_setup import configure_logging
from app.server_state_service import ServerStateService
from app.utils import build_http_url, ensure_runtime_directories, select_local_network


def _health_payload(url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_socket:
            test_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_socket.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _open_browser_when_ready(
    operator_url: str,
    browser_url: str,
    delay_seconds: float,
    logger: logging.Logger,
    first_run_marker: Path | None = None,
) -> None:
    health_url = f"{operator_url}/health"
    deadline = time.monotonic() + 30

    while time.monotonic() < deadline:
        if _health_payload(health_url):
            if delay_seconds:
                time.sleep(delay_seconds)
            logger.info("Opening operator browser at %s", browser_url)
            webbrowser.open(browser_url, new=1)
            if first_run_marker is not None:
                try:
                    first_run_marker.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not clear the first-run marker at %s", first_run_marker)
            return
        time.sleep(0.25)

    logger.warning("The browser was not opened because the server health check timed out.")


def run(bundle_root: Path, runtime_root: Path | None = None) -> int:
    bundle_root = Path(bundle_root)
    runtime_root = Path(runtime_root or bundle_root)
    directories = ensure_runtime_directories(runtime_root)
    config = load_config(runtime_root)
    logger = configure_logging(
        project_root=runtime_root,
        level_name=config["logging"]["level"],
        keep_days=config["logging"]["keep_days"],
    )

    server_config = config["server"]
    host = server_config["host"]
    port = server_config["port"]
    operator_url = build_http_url("127.0.0.1", port)
    health_url = f"{operator_url}/health"
    first_run_marker = runtime_root / ".first_run_pending"
    browser_url = f"{operator_url}/settings" if first_run_marker.exists() else operator_url

    existing = _health_payload(health_url)
    if existing and existing.get("application") == config["application"]["name"]:
        logger.info("ARCOBALENO is already running at %s", operator_url)
        if config["launcher"]["open_browser"]:
            webbrowser.open(operator_url, new=1)
        return 0

    if not _port_is_available(port):
        raise RuntimeError(
            f"Port {port} is already being used by another program. "
            "Close that program or change server.port in config.json."
        )

    preferred_ip = str(server_config.get("preferred_ip", "")).strip()
    network_selection = select_local_network(preferred_ip)
    network_ip = network_selection.selected_ip
    network_url = build_http_url(network_ip, port)

    logger.info("ARCOBALENO %s starting", __version__)
    logger.info("Bundle folder: %s", bundle_root)
    logger.info("Writable data folder: %s", runtime_root)
    logger.info("Runtime folders ready: %s", ", ".join(directories))
    logger.info("Operator URL: %s", operator_url)
    logger.info("Guest network URL: %s", network_url)
    logger.info("Detected IPv4 addresses: %s", ", ".join(network_selection.candidates) or "none")
    if preferred_ip and not network_selection.preferred_available:
        logger.warning(
            "Reserved event IP %s is not assigned to this laptop. Guest links remain locked to the reservation; detected alternate address: %s",
            preferred_ip,
            network_selection.detected_ip,
        )

    print()
    print("ARCOBALENO IS STARTING")
    print(f"Operator page : {operator_url}")
    print(f"Network page  : {network_url}")
    if preferred_ip and not network_selection.preferred_available:
        print(f"NETWORK NOTE  : Reserved IP {preferred_ip} is not active on this laptop.")
        print(f"                No VPN or alternate address will be placed in guest QR codes.")
        print(f"                Detected alternate: {network_selection.detected_ip}")
    print("Keep this window open while ARCOBALENO is running.")
    print("Use Settings > Shut down ARCOBALENO, or press Ctrl+C to stop the server.")
    print()

    server_state_service = ServerStateService(runtime_root)
    server_state: dict[str, Any] = {}
    startup_active_event_id = ""
    try:
        startup_event = EventService(runtime_root).get_active_event()
        if startup_event is not None:
            startup_active_event_id = str(startup_event.get("id") or "").strip()
    except EventStorageError:
        logger.exception("Could not inspect the active event before startup recovery checks.")
    try:
        server_state = server_state_service.begin(
            __version__,
            active_event_id=startup_active_event_id,
        )
    except OSError:
        logger.exception("Could not create the server-session state file.")
    if server_state.get("previous_unclean_shutdown"):
        logger.warning("The previous ARCOBALENO server session did not close cleanly.")

    application = create_app(bundle_root, config, runtime_root)
    runtime_control = application.extensions["arcobaleno_runtime_control_service"]

    def schedule_safe_shutdown() -> None:
        def interrupt_server() -> None:
            # Give Waitress enough time to deliver the shutdown confirmation page
            # before interrupting its main event loop. The normal finally block
            # then records the session as cleanly stopped.
            time.sleep(1.25)
            _thread.interrupt_main()

        threading.Thread(
            target=interrupt_server,
            name="arcobaleno-safe-shutdown",
            daemon=True,
        ).start()

    runtime_control.bind_shutdown(schedule_safe_shutdown)

    if config["launcher"]["open_browser"]:
        browser_thread = threading.Thread(
            target=_open_browser_when_ready,
            args=(
                operator_url,
                browser_url,
                config["launcher"]["browser_delay_seconds"],
                logger,
                first_run_marker if first_run_marker.exists() else None,
            ),
            name="arcobaleno-browser-opener",
            daemon=True,
        )
        browser_thread.start()

    try:
        serve(
            application,
            host=host,
            port=port,
            threads=server_config["threads"],
            ident=f"ARCOBALENO/{__version__}",
            clear_untrusted_proxy_headers=True,
        )
    except KeyboardInterrupt:
        print("\n[ARCOBALENO] Stopping the server safely...")
        logger.info("ARCOBALENO stopped by operator.")
        return 0
    finally:
        try:
            server_state_service.finish()
            logger.info("ARCOBALENO server state was closed cleanly.")
            print("[ARCOBALENO] Server stopped. Event data is safe.")
        except OSError:
            logger.exception("Could not record the clean server shutdown state.")

    return 0
