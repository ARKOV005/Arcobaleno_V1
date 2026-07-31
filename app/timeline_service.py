from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

_TIMELINE_SCHEMA_VERSION = 1
_MAX_TITLE_LENGTH = 100
_MAX_DETAIL_LENGTH = 240
_ALLOWED_ICONS = {"spark", "camera", "meal", "dance", "cake", "heart", "music", "toast", "marker"}


class TimelineError(RuntimeError):
    """Raised when event timeline data cannot be read or saved."""


class TimelineValidationError(TimelineError):
    """Raised when an operator timeline marker is invalid."""


class TimelineService:
    """Durable event milestones and operator-defined schedule markers."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.timeline_path = self.runtime_root / "events" / "current" / "timeline.json"
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _clean_text(value: Any, maximum: int) -> str:
        return " ".join(str(value or "").strip().split())[:maximum]

    @staticmethod
    def _empty(event_id: str) -> dict[str, Any]:
        return {
            "schema_version": _TIMELINE_SCHEMA_VERSION,
            "event_id": event_id,
            "updated_at": TimelineService._now(),
            "entries": [],
        }

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise TimelineError("The event timeline could not be saved safely.") from error

    def _load(self, event_id: str) -> dict[str, Any]:
        if not self.timeline_path.exists():
            return self._empty(event_id)
        try:
            payload = json.loads(self.timeline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TimelineError("The event timeline could not be read.") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise TimelineError("The event timeline is invalid.")
        if str(payload.get("event_id") or "") != event_id:
            raise TimelineError("The event timeline belongs to another event.")
        return payload

    def list_entries(self, event_id: str, *, newest_first: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            entries = [dict(item) for item in self._load(event_id).get("entries", []) if isinstance(item, dict)]
        entries.sort(
            key=lambda item: (
                str(item.get("scheduled_time") or "99:99"),
                str(item.get("created_at") or ""),
            )
        )
        if newest_first:
            entries.reverse()
        return entries

    def public_entries(self, event_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.list_entries(event_id)
            if bool(item.get("public", True)) and str(item.get("kind") or "") == "marker"
        ]

    def append(
        self,
        event_id: str,
        *,
        title: Any,
        detail: Any = "",
        kind: str = "system",
        icon: str = "spark",
        scheduled_time: Any = "",
        public: bool = True,
        photo_id: str = "",
    ) -> dict[str, Any]:
        clean_title = self._clean_text(title, _MAX_TITLE_LENGTH)
        if not clean_title:
            raise TimelineValidationError("Enter a timeline title.")
        clean_detail = self._clean_text(detail, _MAX_DETAIL_LENGTH)
        clean_kind = kind if kind in {"system", "milestone", "marker", "moderation"} else "system"
        clean_icon = icon if icon in _ALLOWED_ICONS else "marker"
        clean_time = self._clean_text(scheduled_time, 5)
        if clean_time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clean_time):
            raise TimelineValidationError("Use a valid 24-hour time such as 19:30.")

        with self._lock:
            payload = self._load(event_id)
            entry = {
                "id": secrets.token_hex(8),
                "event_id": event_id,
                "title": clean_title,
                "detail": clean_detail,
                "kind": clean_kind,
                "icon": clean_icon,
                "scheduled_time": clean_time,
                "public": bool(public),
                "photo_id": str(photo_id or "")[:64],
                "created_at": self._now(),
            }
            payload["entries"].append(entry)
            payload["updated_at"] = self._now()
            self._atomic_write(self.timeline_path, payload)
            return dict(entry)

    def delete(self, event_id: str, entry_id: str) -> dict[str, Any]:
        clean_id = str(entry_id or "")
        with self._lock:
            payload = self._load(event_id)
            for index, entry in enumerate(payload.get("entries", [])):
                if isinstance(entry, dict) and str(entry.get("id") or "") == clean_id:
                    removed = dict(entry)
                    del payload["entries"][index]
                    payload["updated_at"] = self._now()
                    self._atomic_write(self.timeline_path, payload)
                    return removed
        raise TimelineError("That timeline entry does not exist.")

    def get(self, event_id: str, entry_id: str) -> dict[str, Any] | None:
        for entry in self.list_entries(event_id):
            if str(entry.get("id") or "") == str(entry_id or ""):
                return entry
        return None
