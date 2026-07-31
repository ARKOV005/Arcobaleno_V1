from __future__ import annotations

import io
import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import qrcode
from qrcode.constants import ERROR_CORRECT_Q

_ACCESS_SCHEMA_VERSION = 1
_MAX_SSID_LENGTH = 32
_MAX_PASSWORD_LENGTH = 64
_ALLOWED_SECURITY = {"WPA", "nopass"}


class AccessError(RuntimeError):
    """Base class for QR and event-access errors."""


class AccessValidationError(AccessError):
    """Raised when Wi-Fi information is incomplete or invalid."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("Event-access information is invalid.")


class AccessStorageError(AccessError):
    """Raised when access settings cannot be read or written safely."""


class AccessService:
    """Owns event-specific Wi-Fi settings and QR generation."""

    def __init__(self, runtime_root: Path, defaults: Mapping[str, Any]):
        self.runtime_root = Path(runtime_root)
        self.settings_path = self.runtime_root / "events" / "current" / "access.json"
        self.default_wifi = {
            "ssid": str(defaults.get("wifi_ssid") or "ARCOBALENO").strip(),
            "security": str(defaults.get("wifi_security") or "WPA").strip(),
            "password": str(defaults.get("wifi_password") or ""),
            "hidden": bool(defaults.get("wifi_hidden", False)),
        }
        self.qr_box_size = int(defaults.get("qr_box_size", 12))
        self.qr_border_modules = int(defaults.get("qr_border_modules", 4))
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _clean_single_line(value: Any) -> str:
        return str(value or "").replace("\r", "").replace("\n", "").strip()

    @staticmethod
    def _default_payload(event_id: str, wifi: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": _ACCESS_SCHEMA_VERSION,
            "event_id": event_id,
            "wifi": dict(wifi),
            "created_at": None,
            "updated_at": None,
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as error:
            raise AccessStorageError(f"Could not read access settings from {path}.") from error

        if not isinstance(payload, dict):
            raise AccessStorageError("Access settings are not a JSON object.")
        return payload

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, path)
        except OSError as error:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise AccessStorageError("Could not safely save event-access settings.") from error

    @classmethod
    def validate_wifi(cls, values: Mapping[str, Any]) -> dict[str, Any]:
        ssid = cls._clean_single_line(values.get("ssid"))
        security = cls._clean_single_line(values.get("security")) or "WPA"
        password = str(values.get("password") or "")
        hidden = bool(values.get("hidden", False))
        errors: dict[str, str] = {}

        if not ssid:
            errors["ssid"] = "Enter the exact Wi-Fi network name shown by Omada."
        elif len(ssid) > _MAX_SSID_LENGTH:
            errors["ssid"] = f"Use {_MAX_SSID_LENGTH} characters or fewer."

        if security not in _ALLOWED_SECURITY:
            errors["security"] = "Choose WPA/WPA2 or Open network."

        if security == "WPA":
            is_64_hex = len(password) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", password)
            if not (8 <= len(password) <= 63 or is_64_hex):
                errors["password"] = "WPA/WPA2 passwords must contain 8 to 63 characters."
        elif password:
            password = ""

        if len(password) > _MAX_PASSWORD_LENGTH:
            errors["password"] = f"Use {_MAX_PASSWORD_LENGTH} characters or fewer."

        if errors:
            raise AccessValidationError(errors)

        return {
            "ssid": ssid,
            "security": security,
            "password": password,
            "hidden": hidden,
        }

    def get_settings(self, event_id: str) -> dict[str, Any]:
        with self._lock:
            if not self.settings_path.exists():
                return self._default_payload(event_id, self.default_wifi)

            payload = self._read_json(self.settings_path)
            if str(payload.get("event_id") or "") != event_id:
                raise AccessStorageError("Access settings belong to a different event.")

            wifi = payload.get("wifi")
            if not isinstance(wifi, dict):
                raise AccessStorageError("Access settings do not contain Wi-Fi information.")

            merged_wifi = {**self.default_wifi, **wifi}
            payload = dict(payload)
            payload["wifi"] = merged_wifi
            return payload

    def save_settings(self, event_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            wifi = self.validate_wifi(values)
            existing = self.get_settings(event_id)
            timestamp = self._now()
            payload = {
                "schema_version": _ACCESS_SCHEMA_VERSION,
                "event_id": event_id,
                "wifi": wifi,
                "created_at": existing.get("created_at") or timestamp,
                "updated_at": timestamp,
            }
            self._atomic_write_json(self.settings_path, payload)
            return payload

    @classmethod
    def wifi_ready(cls, wifi: Mapping[str, Any]) -> bool:
        try:
            cls.validate_wifi(wifi)
        except AccessValidationError:
            return False
        return True

    @staticmethod
    def _wifi_escape(value: str) -> str:
        escaped = value.replace("\\", "\\\\")
        for character in (";", ",", ":", '"'):
            escaped = escaped.replace(character, f"\\{character}")
        return escaped

    @classmethod
    def wifi_payload(cls, wifi: Mapping[str, Any]) -> str:
        cleaned = cls.validate_wifi(wifi)
        security = cleaned["security"]
        ssid = cls._wifi_escape(cleaned["ssid"])
        hidden = "true" if cleaned["hidden"] else "false"
        fields = [f"WIFI:T:{security}", f"S:{ssid}"]
        if security != "nopass":
            fields.append(f"P:{cls._wifi_escape(cleaned['password'])}")
        fields.append(f"H:{hidden}")
        return ";".join(fields) + ";;"

    def qr_png(self, payload: str) -> bytes:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_Q,
            box_size=self.qr_box_size,
            border=self.qr_border_modules,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def filename_slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return slug[:48].strip("-") or "event"
