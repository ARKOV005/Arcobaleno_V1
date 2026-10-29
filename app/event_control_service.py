from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

_CONTROL_SCHEMA_VERSION = 2
_MAX_MESSAGE_LENGTH = 180
_DEFAULT_PAUSE_MESSAGE = "Photo uploads are temporarily paused. Please try again shortly."
_ALLOWED_EVENT_MODES = {"setup", "live"}
_ALLOWED_MODERATION_MODES = {"family", "instant", "curated"}
_ALLOWED_SLIDESHOW_STATES = {"playing", "paused", "black", "logo", "timeline", "marker", "emergency", "instructions"}
_ALLOWED_SLIDESHOW_COMMANDS = {"", "next", "previous", "restart"}


class EventControlError(RuntimeError):
    """Raised when event-control data cannot be read or saved safely."""


class EventControlService:
    """Stores durable live-event, moderation, upload, and slideshow controls."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.control_path = self.runtime_root / "events" / "current" / "control.json"
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _clean_message(value: Any) -> str:
        text = " ".join(str(value or "").strip().split())
        return text[:_MAX_MESSAGE_LENGTH]

    @staticmethod
    def _default(event_id: str) -> dict[str, Any]:
        return {
            "schema_version": _CONTROL_SCHEMA_VERSION,
            "event_id": event_id,
            "event_mode": "setup",
            "uploads_open": False,
            "guest_downloads_enabled": False,
            "pause_message": _DEFAULT_PAUSE_MESSAGE,
            "moderation_mode": "family",
            "moderation_delay_seconds": 10,
            "slideshow_state": "logo",
            "instructions_started_at": "",
            "slideshow_command": "",
            "slideshow_command_sequence": 0,
            "slideshow_title": "ARCOBALENO",
            "slideshow_message": "Waiting for the celebration to begin.",
            "featured_marker_id": "",
            "live_started_at": None,
            "live_stopped_at": None,
            "created_at": None,
            "updated_at": None,
        }

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
            raise EventControlError("Event controls could not be saved safely.") from error

    def get(self, event_id: str) -> dict[str, Any]:
        with self._lock:
            if not self.control_path.exists():
                return self._default(event_id)
            try:
                payload = json.loads(self.control_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise EventControlError("Event controls could not be read.") from error
            if not isinstance(payload, dict):
                raise EventControlError("Event controls are invalid.")
            if str(payload.get("event_id") or "") != event_id:
                raise EventControlError("Event controls belong to another event.")

            merged = self._default(event_id)
            merged.update(payload)
            # Upgrade a pre-production control file without interrupting an already-open event.
            if "event_mode" not in payload:
                merged["event_mode"] = "live" if bool(payload.get("uploads_open", True)) else "setup"
            merged["schema_version"] = _CONTROL_SCHEMA_VERSION
            merged["event_mode"] = merged.get("event_mode") if merged.get("event_mode") in _ALLOWED_EVENT_MODES else "setup"
            merged["uploads_open"] = bool(merged.get("uploads_open", False))
            merged["pause_message"] = self._clean_message(merged.get("pause_message")) or _DEFAULT_PAUSE_MESSAGE
            merged["moderation_mode"] = merged.get("moderation_mode") if merged.get("moderation_mode") in _ALLOWED_MODERATION_MODES else "family"
            try:
                delay = int(merged.get("moderation_delay_seconds") or 0)
            except (TypeError, ValueError):
                delay = 10
            merged["moderation_delay_seconds"] = max(0, min(120, delay))
            merged["slideshow_state"] = merged.get("slideshow_state") if merged.get("slideshow_state") in _ALLOWED_SLIDESHOW_STATES else "playing"
            merged["slideshow_command"] = merged.get("slideshow_command") if merged.get("slideshow_command") in _ALLOWED_SLIDESHOW_COMMANDS else ""
            try:
                merged["slideshow_command_sequence"] = max(0, int(merged.get("slideshow_command_sequence") or 0))
            except (TypeError, ValueError):
                merged["slideshow_command_sequence"] = 0
            merged["slideshow_title"] = self._clean_message(merged.get("slideshow_title")) or "ARCOBALENO"
            merged["slideshow_message"] = self._clean_message(merged.get("slideshow_message"))
            merged["featured_marker_id"] = str(merged.get("featured_marker_id") or "")[:64]
            return merged

    def update(self, event_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            existing = self.get(event_id)
            timestamp = self._now()
            payload = dict(existing)

            if "event_mode" in changes:
                mode = str(changes["event_mode"] or "")
                if mode not in _ALLOWED_EVENT_MODES:
                    raise EventControlError("The event mode is invalid.")
                payload["event_mode"] = mode
            if "guest_downloads_enabled" in changes:
                payload["guest_downloads_enabled"] = bool(changes["guest_downloads_enabled"])
            if "uploads_open" in changes:
                payload["uploads_open"] = bool(changes["uploads_open"])
            if "pause_message" in changes:
                payload["pause_message"] = self._clean_message(changes["pause_message"]) or _DEFAULT_PAUSE_MESSAGE
            if "moderation_mode" in changes:
                mode = str(changes["moderation_mode"] or "")
                if mode not in _ALLOWED_MODERATION_MODES:
                    raise EventControlError("The moderation mode is invalid.")
                payload["moderation_mode"] = mode
            if "moderation_delay_seconds" in changes:
                try:
                    delay = int(changes["moderation_delay_seconds"])
                except (TypeError, ValueError) as error:
                    raise EventControlError("The moderation delay must be a number.") from error
                payload["moderation_delay_seconds"] = max(0, min(120, delay))
            if "slideshow_state" in changes:
                state = str(changes["slideshow_state"] or "")
                if state not in _ALLOWED_SLIDESHOW_STATES:
                    raise EventControlError("The slideshow state is invalid.")
                payload["slideshow_state"] = state
                if state == "instructions":
                    payload["instructions_started_at"] = datetime.now().astimezone().isoformat(timespec="microseconds")
            if "slideshow_title" in changes:
                payload["slideshow_title"] = self._clean_message(changes["slideshow_title"]) or "ARCOBALENO"
            if "slideshow_message" in changes:
                payload["slideshow_message"] = self._clean_message(changes["slideshow_message"])
            if "featured_marker_id" in changes:
                payload["featured_marker_id"] = str(changes["featured_marker_id"] or "")[:64]
            if "live_started_at" in changes:
                payload["live_started_at"] = changes["live_started_at"]
            if "live_stopped_at" in changes:
                payload["live_stopped_at"] = changes["live_stopped_at"]

            payload["schema_version"] = _CONTROL_SCHEMA_VERSION
            payload["event_id"] = event_id
            payload["created_at"] = existing.get("created_at") or timestamp
            payload["updated_at"] = timestamp
            self._atomic_write(self.control_path, payload)
            return payload

    def save(self, event_id: str, *, uploads_open: bool, pause_message: Any = "") -> dict[str, Any]:
        return self.update(
            event_id,
            uploads_open=uploads_open,
            pause_message=pause_message,
        )

    def set_event_mode(self, event_id: str, mode: str) -> dict[str, Any]:
        clean_mode = str(mode or "").strip().casefold()
        if clean_mode not in _ALLOWED_EVENT_MODES:
            raise EventControlError("The event mode is invalid.")
        timestamp = self._now()
        if clean_mode == "live":
            return self.update(
                event_id,
                event_mode="live",
                uploads_open=True,
                slideshow_state="playing",
                live_started_at=timestamp,
                live_stopped_at=None,
            )
        return self.update(
            event_id,
            event_mode="setup",
            uploads_open=False,
            slideshow_state="logo",
            live_stopped_at=timestamp,
        )

    def send_slideshow_command(self, event_id: str, command: str) -> dict[str, Any]:
        if command not in _ALLOWED_SLIDESHOW_COMMANDS - {""}:
            raise EventControlError("The slideshow command is invalid.")
        with self._lock:
            existing = self.get(event_id)
            payload = dict(existing)
            payload["slideshow_command"] = command
            payload["slideshow_command_sequence"] = int(existing.get("slideshow_command_sequence") or 0) + 1
            payload["updated_at"] = self._now()
            payload["created_at"] = existing.get("created_at") or payload["updated_at"]
            payload["schema_version"] = _CONTROL_SCHEMA_VERSION
            self._atomic_write(self.control_path, payload)
            return payload
