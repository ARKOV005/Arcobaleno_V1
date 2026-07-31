from __future__ import annotations

import copy
import ipaddress
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Mapping

from app.config import ConfigurationError, _validate


class SettingsValidationError(ValueError):
    """Raised when operator settings are incomplete or invalid."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("Application settings are invalid.")


class SettingsStorageError(RuntimeError):
    """Raised when settings cannot be saved safely."""


class SettingsService:
    """Validates and persists the supported operator settings."""

    def __init__(self, project_root: Path, config: dict[str, Any]):
        self.project_root = Path(project_root)
        self.config_path = self.project_root / "config.json"
        self.config = config
        self._lock = threading.RLock()

    @staticmethod
    def _integer(values: Mapping[str, Any], key: str, minimum: int, maximum: int, errors: dict[str, str]) -> int:
        raw = str(values.get(key, "")).strip()
        try:
            value = int(raw)
        except ValueError:
            errors[key] = "Enter a whole number."
            return minimum
        if not minimum <= value <= maximum:
            errors[key] = f"Use a value from {minimum} to {maximum}."
        return value

    @staticmethod
    def _number(values: Mapping[str, Any], key: str, minimum: float, maximum: float, errors: dict[str, str]) -> float:
        raw = str(values.get(key, "")).strip()
        try:
            value = float(raw)
        except ValueError:
            errors[key] = "Enter a number."
            return minimum
        if not minimum <= value <= maximum:
            errors[key] = f"Use a value from {minimum:g} to {maximum:g}."
        return value

    def form_values(self) -> dict[str, Any]:
        return {
            "photo_limit_per_device": self.config["camera"]["photo_limit_per_device"],
            "max_upload_megabytes": self.config["camera"]["max_upload_megabytes"],
            "seconds_per_photo": self.config["slideshow"]["seconds_per_photo"],
            "poll_interval_seconds": self.config["slideshow"]["poll_interval_seconds"],
            "show_guest_name": self.config["slideshow"]["show_guest_name"],
            "show_timestamp": self.config["slideshow"]["show_timestamp"],
            "preferred_ip": self.config["server"].get("preferred_ip", ""),
            "open_browser": self.config["launcher"]["open_browser"],
        }

    def validate(self, values: Mapping[str, Any]) -> dict[str, Any]:
        errors: dict[str, str] = {}
        photo_limit = self._integer(values, "photo_limit_per_device", 0, 1000, errors)
        max_upload = self._integer(values, "max_upload_megabytes", 1, 250, errors)
        seconds = self._number(values, "seconds_per_photo", 2, 60, errors)
        poll_seconds = self._number(values, "poll_interval_seconds", 0.5, 30, errors)
        preferred_ip = str(values.get("preferred_ip", "")).strip()
        if preferred_ip:
            try:
                parsed = ipaddress.ip_address(preferred_ip)
                if parsed.version != 4:
                    raise ValueError
            except ValueError:
                errors["preferred_ip"] = "Enter a valid IPv4 address, such as 192.168.0.106."

        if errors:
            raise SettingsValidationError(errors)

        return {
            "photo_limit_per_device": photo_limit,
            "max_upload_megabytes": max_upload,
            "seconds_per_photo": seconds,
            "poll_interval_seconds": poll_seconds,
            "show_guest_name": str(values.get("show_guest_name", "")).lower() in {"true", "1", "on", "yes"},
            "show_timestamp": str(values.get("show_timestamp", "")).lower() in {"true", "1", "on", "yes"},
            "preferred_ip": preferred_ip,
            "open_browser": str(values.get("open_browser", "")).lower() in {"true", "1", "on", "yes"},
        }


    def save_photo_limit(self, value: Any) -> int:
        """Persist and apply the guest photo limit without a server restart."""

        errors: dict[str, str] = {}
        limit = self._integer(
            {"photo_limit_per_device": value},
            "photo_limit_per_device",
            0,
            1000,
            errors,
        )
        if errors:
            raise SettingsValidationError(errors)

        with self._lock:
            candidate = copy.deepcopy(self.config)
            candidate["camera"]["photo_limit_per_device"] = limit
            try:
                _validate(candidate)
            except ConfigurationError as error:
                raise SettingsValidationError({"photo_limit_per_device": str(error)}) from error

            temporary_path = self.config_path.with_name(
                f".{self.config_path.name}.{secrets.token_hex(4)}.tmp"
            )
            try:
                temporary_path.write_text(
                    json.dumps(candidate, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary_path, self.config_path)
            except OSError as error:
                temporary_path.unlink(missing_ok=True)
                raise SettingsStorageError("ARCOBALENO could not save config.json.") from error

            self.config.clear()
            self.config.update(candidate)
        return limit

    def save(self, values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        cleaned = self.validate(values)
        candidate = copy.deepcopy(self.config)
        previous_ip = str(candidate["server"].get("preferred_ip", ""))
        previous_browser = bool(candidate["launcher"].get("open_browser", True))

        candidate["camera"]["photo_limit_per_device"] = cleaned["photo_limit_per_device"]
        candidate["camera"]["max_upload_megabytes"] = cleaned["max_upload_megabytes"]
        candidate["slideshow"]["seconds_per_photo"] = cleaned["seconds_per_photo"]
        candidate["slideshow"]["poll_interval_seconds"] = cleaned["poll_interval_seconds"]
        candidate["slideshow"]["show_guest_name"] = cleaned["show_guest_name"]
        candidate["slideshow"]["show_timestamp"] = cleaned["show_timestamp"]
        candidate["server"]["preferred_ip"] = cleaned["preferred_ip"]
        candidate["launcher"]["open_browser"] = cleaned["open_browser"]

        try:
            _validate(candidate)
        except ConfigurationError as error:
            raise SettingsValidationError({"general": str(error)}) from error

        temporary_path = self.config_path.with_name(f".{self.config_path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary_path.write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary_path, self.config_path)
        except OSError as error:
            temporary_path.unlink(missing_ok=True)
            raise SettingsStorageError("ARCOBALENO could not save config.json.") from error

        self.config.clear()
        self.config.update(candidate)
        restart_required = previous_ip != cleaned["preferred_ip"] or previous_browser != cleaned["open_browser"]
        return cleaned, restart_required
