from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "application": {
        "name": "ARCOBALENO",
        "environment": "production",
    },
    "server": {
        "host": "0.0.0.0",
        "port": 5000,
        "preferred_ip": "",
        "threads": 8,
    },
    "launcher": {
        "open_browser": True,
        "browser_delay_seconds": 0.5,
    },
    "camera": {
        "photo_limit_per_device": 21,
        "max_upload_megabytes": 60,
    },
    "slideshow": {
        "seconds_per_photo": 7,
        "poll_interval_seconds": 2,
        "show_guest_name": True,
        "show_timestamp": True,
    },
    "access": {
        "wifi_ssid": "ARCOBALENO",
        "wifi_security": "WPA",
        "wifi_password": "",
        "wifi_hidden": False,
        "qr_box_size": 12,
        "qr_border_modules": 4,
    },
    "remote": {
        "enabled": True,
        "session_hours": 12,
        "poll_interval_seconds": 3,
    },
    "logging": {
        "level": "INFO",
        "keep_days": 14,
    },
    "safety": {
        "warning_free_gigabytes": 5,
        "critical_free_gigabytes": 1,
        "upload_reserve_megabytes": 512,
    },
}


class ConfigurationError(ValueError):
    """Raised when config.json contains an invalid value."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate(config: dict[str, Any]) -> None:
    server = config["server"]
    launcher = config["launcher"]
    camera = config["camera"]
    slideshow = config["slideshow"]
    access = config["access"]
    remote = config["remote"]
    logging_config = config["logging"]
    safety = config["safety"]

    host = server.get("host")
    port = server.get("port")
    threads = server.get("threads")
    preferred_ip = server.get("preferred_ip")

    if not isinstance(host, str) or not host.strip():
        raise ConfigurationError("server.host must be a non-empty string.")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigurationError("server.port must be a number from 1 to 65535.")
    if not isinstance(threads, int) or not 2 <= threads <= 64:
        raise ConfigurationError("server.threads must be a number from 2 to 64.")
    if not isinstance(preferred_ip, str):
        raise ConfigurationError("server.preferred_ip must be text.")
    photo_limit = camera.get("photo_limit_per_device")
    max_upload_megabytes = camera.get("max_upload_megabytes")
    if not isinstance(photo_limit, int) or not 0 <= photo_limit <= 1000:
        raise ConfigurationError("camera.photo_limit_per_device must be from 0 to 1000.")
    if not isinstance(max_upload_megabytes, int) or not 1 <= max_upload_megabytes <= 250:
        raise ConfigurationError("camera.max_upload_megabytes must be from 1 to 250.")
    seconds_per_photo = slideshow.get("seconds_per_photo")
    poll_interval_seconds = slideshow.get("poll_interval_seconds")
    if not isinstance(seconds_per_photo, (int, float)) or not 2 <= seconds_per_photo <= 60:
        raise ConfigurationError("slideshow.seconds_per_photo must be from 2 to 60.")
    if not isinstance(poll_interval_seconds, (int, float)) or not 0.5 <= poll_interval_seconds <= 30:
        raise ConfigurationError("slideshow.poll_interval_seconds must be from 0.5 to 30.")
    if not isinstance(slideshow.get("show_guest_name"), bool):
        raise ConfigurationError("slideshow.show_guest_name must be true or false.")
    if not isinstance(slideshow.get("show_timestamp"), bool):
        raise ConfigurationError("slideshow.show_timestamp must be true or false.")
    wifi_ssid = access.get("wifi_ssid")
    wifi_security = access.get("wifi_security")
    wifi_password = access.get("wifi_password")
    wifi_hidden = access.get("wifi_hidden")
    qr_box_size = access.get("qr_box_size")
    qr_border_modules = access.get("qr_border_modules")
    if not isinstance(wifi_ssid, str) or len(wifi_ssid) > 32:
        raise ConfigurationError("access.wifi_ssid must be text with 32 characters or fewer.")
    if wifi_security not in {"WPA", "nopass"}:
        raise ConfigurationError("access.wifi_security must be WPA or nopass.")
    if not isinstance(wifi_password, str) or len(wifi_password) > 64:
        raise ConfigurationError("access.wifi_password must be text with 64 characters or fewer.")
    if not isinstance(wifi_hidden, bool):
        raise ConfigurationError("access.wifi_hidden must be true or false.")
    if not isinstance(qr_box_size, int) or not 4 <= qr_box_size <= 24:
        raise ConfigurationError("access.qr_box_size must be from 4 to 24.")
    if not isinstance(qr_border_modules, int) or not 4 <= qr_border_modules <= 12:
        raise ConfigurationError("access.qr_border_modules must be from 4 to 12.")
    if not isinstance(remote.get("enabled"), bool):
        raise ConfigurationError("remote.enabled must be true or false.")
    session_hours = remote.get("session_hours")
    if not isinstance(session_hours, (int, float)) or not 0.25 <= session_hours <= 24:
        raise ConfigurationError("remote.session_hours must be from 0.25 to 24.")
    remote_poll = remote.get("poll_interval_seconds")
    if not isinstance(remote_poll, (int, float)) or not 1 <= remote_poll <= 30:
        raise ConfigurationError("remote.poll_interval_seconds must be from 1 to 30.")
    if not isinstance(launcher.get("open_browser"), bool):
        raise ConfigurationError("launcher.open_browser must be true or false.")
    delay = launcher.get("browser_delay_seconds")
    if not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
        raise ConfigurationError("launcher.browser_delay_seconds must be from 0 to 30.")
    if str(logging_config.get("level", "")).upper() not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        raise ConfigurationError("logging.level is invalid.")
    keep_days = logging_config.get("keep_days")
    if not isinstance(keep_days, int) or not 1 <= keep_days <= 365:
        raise ConfigurationError("logging.keep_days must be a number from 1 to 365.")
    warning_free_gigabytes = safety.get("warning_free_gigabytes")
    critical_free_gigabytes = safety.get("critical_free_gigabytes")
    upload_reserve_megabytes = safety.get("upload_reserve_megabytes")
    if not isinstance(warning_free_gigabytes, (int, float)) or not 1 <= warning_free_gigabytes <= 1000:
        raise ConfigurationError("safety.warning_free_gigabytes must be from 1 to 1000.")
    if not isinstance(critical_free_gigabytes, (int, float)) or not 0.25 <= critical_free_gigabytes <= 100:
        raise ConfigurationError("safety.critical_free_gigabytes must be from 0.25 to 100.")
    if critical_free_gigabytes >= warning_free_gigabytes:
        raise ConfigurationError("safety.critical_free_gigabytes must be lower than the warning level.")
    if not isinstance(upload_reserve_megabytes, int) or not 64 <= upload_reserve_megabytes <= 16384:
        raise ConfigurationError("safety.upload_reserve_megabytes must be from 64 to 16384.")


def load_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "config.json"
    if not config_path.exists():
        config_path.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2) + "\n",
            encoding="utf-8",
        )

    try:
        user_config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"config.json is not valid JSON near line {error.lineno}, column {error.colno}."
        ) from error

    if not isinstance(user_config, dict):
        raise ConfigurationError("config.json must contain one JSON object.")

    config = _deep_merge(DEFAULT_CONFIG, user_config)

    host_override = os.getenv("ARCOBALENO_HOST")
    if host_override is not None:
        config["server"]["host"] = host_override.strip()

    if os.getenv("ARCOBALENO_PORT"):
        try:
            config["server"]["port"] = int(os.environ["ARCOBALENO_PORT"])
        except ValueError as error:
            raise ConfigurationError("ARCOBALENO_PORT must be a number.") from error

    open_browser_override = os.getenv("ARCOBALENO_OPEN_BROWSER")
    if open_browser_override is not None:
        normalized = open_browser_override.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            config["launcher"]["open_browser"] = True
        elif normalized in {"0", "false", "no", "off"}:
            config["launcher"]["open_browser"] = False
        else:
            raise ConfigurationError(
                "ARCOBALENO_OPEN_BROWSER must be true/false, yes/no, on/off, or 1/0."
            )

    _validate(config)
    return config
