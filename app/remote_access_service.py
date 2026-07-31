from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


class RemoteAccessError(RuntimeError):
    """Raised when mobile operator access settings cannot be read or saved."""


class RemoteAccessService:
    """Owns the persistent same-network mobile operator PIN and enable switch."""

    def __init__(self, runtime_root: Path, *, enabled_by_default: bool = True):
        self.runtime_root = Path(runtime_root)
        self.settings_path = self.runtime_root / "operator_remote.json"
        self.enabled_by_default = bool(enabled_by_default)
        self._lock = threading.RLock()
        self._ensure_settings()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _new_pin() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(
                json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise RemoteAccessError("Mobile operator settings could not be saved safely.") from error

    def _default(self) -> dict[str, Any]:
        timestamp = self._now()
        return {
            "schema_version": 1,
            "enabled": self.enabled_by_default,
            "pin": self._new_pin(),
            "created_at": timestamp,
            "updated_at": timestamp,
            "pin_updated_at": timestamp,
        }

    def _ensure_settings(self) -> None:
        with self._lock:
            if self.settings_path.exists():
                self.get()
                return
            self._atomic_write(self.settings_path, self._default())

    def get(self) -> dict[str, Any]:
        with self._lock:
            if not self.settings_path.exists():
                payload = self._default()
                self._atomic_write(self.settings_path, payload)
                return payload
            try:
                payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RemoteAccessError("Mobile operator settings could not be read.") from error
            if not isinstance(payload, dict):
                raise RemoteAccessError("Mobile operator settings are invalid.")

            changed = False
            pin = str(payload.get("pin") or "")
            if len(pin) != 6 or not pin.isdigit():
                pin = self._new_pin()
                payload["pin"] = pin
                payload["pin_updated_at"] = self._now()
                changed = True
            if not isinstance(payload.get("enabled"), bool):
                payload["enabled"] = self.enabled_by_default
                changed = True
            payload["schema_version"] = 1
            payload.setdefault("created_at", self._now())
            payload.setdefault("updated_at", self._now())
            payload.setdefault("pin_updated_at", payload["updated_at"])
            if changed:
                payload["updated_at"] = self._now()
                self._atomic_write(self.settings_path, payload)
            return dict(payload)

    def verify_pin(self, supplied: Any) -> bool:
        settings = self.get()
        if not settings.get("enabled"):
            return False
        clean = "".join(character for character in str(supplied or "") if character.isdigit())[:6]
        return len(clean) == 6 and hmac.compare_digest(str(settings["pin"]), clean)

    def reset_pin(self) -> dict[str, Any]:
        with self._lock:
            payload = self.get()
            timestamp = self._now()
            payload["pin"] = self._new_pin()
            payload["pin_updated_at"] = timestamp
            payload["updated_at"] = timestamp
            self._atomic_write(self.settings_path, payload)
            return dict(payload)

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            payload = self.get()
            payload["enabled"] = bool(enabled)
            payload["updated_at"] = self._now()
            self._atomic_write(self.settings_path, payload)
            return dict(payload)

    def operator_status(self, remote_url: str) -> dict[str, Any]:
        payload = self.get()
        return {
            "enabled": bool(payload.get("enabled")),
            "pin": str(payload.get("pin") or ""),
            "url": remote_url,
            "pin_updated_at": payload.get("pin_updated_at"),
        }
