from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

LOGGER = logging.getLogger("arcobaleno.events")
_EVENT_SCHEMA_VERSION = 1
_MAX_TITLE_LENGTH = 100
_MAX_HOSTS_LENGTH = 120
_MAX_VENUE_LENGTH = 160
_MAX_NOTES_LENGTH = 500
_ALLOWED_THEMES = {"default", "floral", "secret-garden", "ocean", "ballroom", "disco"}
_SAFE_ARCHIVE_FOLDER = re.compile(r"^[A-Za-z0-9._-]{1,180}$")


class EventError(RuntimeError):
    """Base class for event-management errors."""


class EventValidationError(EventError):
    """Raised when operator-supplied event information is invalid."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("Event information is invalid.")


class ActiveEventExistsError(EventError):
    """Raised when an event is created while another event is active."""


class NoActiveEventError(EventError):
    """Raised when an operation requires an active event."""


class ArchivedEventNotFoundError(EventError):
    """Raised when an archived workspace cannot be reopened."""


class EventStorageError(EventError):
    """Raised when event data cannot be safely read or written."""


class EventService:
    """Owns persistent event data and the active-to-archive lifecycle."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.current_directory = self.runtime_root / "events" / "current"
        self.archive_directory = self.runtime_root / "events" / "archive"
        self.backup_directory = self.runtime_root / "backups" / "events"
        self.current_event_path = self.current_directory / "event.json"
        self._lock = threading.RLock()

        self.current_directory.mkdir(parents=True, exist_ok=True)
        self.archive_directory.mkdir(parents=True, exist_ok=True)
        self.backup_directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _clean_text(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @staticmethod
    def _clean_notes(value: Any) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(line.rstrip() for line in text.strip().split("\n"))

    @classmethod
    def _validate(cls, values: Mapping[str, Any]) -> dict[str, str]:
        title = cls._clean_text(values.get("title"))
        hosts = cls._clean_text(values.get("hosts"))
        event_date = cls._clean_text(values.get("event_date"))
        venue = cls._clean_text(values.get("venue"))
        notes = cls._clean_notes(values.get("notes"))
        theme = cls._clean_text(values.get("theme") or "default").casefold()

        errors: dict[str, str] = {}

        if not title:
            errors["title"] = "Enter an event title."
        elif len(title) > _MAX_TITLE_LENGTH:
            errors["title"] = f"Use {_MAX_TITLE_LENGTH} characters or fewer."

        if len(hosts) > _MAX_HOSTS_LENGTH:
            errors["hosts"] = f"Use {_MAX_HOSTS_LENGTH} characters or fewer."

        if not event_date:
            errors["event_date"] = "Choose the event date."
        else:
            try:
                date.fromisoformat(event_date)
            except ValueError:
                errors["event_date"] = "Choose a valid event date."

        if len(venue) > _MAX_VENUE_LENGTH:
            errors["venue"] = f"Use {_MAX_VENUE_LENGTH} characters or fewer."

        if len(notes) > _MAX_NOTES_LENGTH:
            errors["notes"] = f"Use {_MAX_NOTES_LENGTH} characters or fewer."

        if theme not in _ALLOWED_THEMES:
            errors["theme"] = "Choose one of the available guest experience themes."

        if errors:
            raise EventValidationError(errors)

        return {
            "title": title,
            "hosts": hosts,
            "event_date": event_date,
            "venue": venue,
            "notes": notes,
            "theme": theme,
        }

    @staticmethod
    def _slugify(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return slug[:48].strip("-") or "event"

    @classmethod
    def _new_event_id(cls, title: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = secrets.token_hex(2)
        return f"{timestamp}-{cls._slugify(title)}-{suffix}"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as error:
            raise EventStorageError(f"Could not read event data from {path}.") from error

        if not isinstance(payload, dict):
            raise EventStorageError(f"Event data in {path} is not a JSON object.")
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
            raise EventStorageError(f"Could not safely write event data to {path}.") from error

    def _backup(self, event: Mapping[str, Any], reason: str) -> None:
        event_id = str(event.get("id") or "unknown-event")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = self.backup_directory / event_id / f"{timestamp}-{reason}.json"
        self._atomic_write_json(backup_path, event)

    def get_active_event(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.current_event_path.exists():
                return None
            return self._read_json(self.current_event_path)

    def create_event(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.current_event_path.exists():
                raise ActiveEventExistsError(
                    "End or archive the current event before creating another one."
                )

            cleaned = self._validate(values)
            timestamp = self._now()
            event = {
                "schema_version": _EVENT_SCHEMA_VERSION,
                "id": self._new_event_id(cleaned["title"]),
                **cleaned,
                "status": "active",
                "created_at": timestamp,
                "updated_at": timestamp,
                "ended_at": None,
            }
            self._atomic_write_json(self.current_event_path, event)
            self._backup(event, "created")
            LOGGER.info("Created active event %s (%s)", event["title"], event["id"])
            return event

    def update_event(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            event = self.get_active_event()
            if event is None:
                raise NoActiveEventError("There is no active event to update.")

            cleaned = self._validate(values)
            self._backup(event, "before-update")
            event.update(cleaned)
            event["updated_at"] = self._now()
            self._atomic_write_json(self.current_event_path, event)
            LOGGER.info("Updated active event %s (%s)", event["title"], event["id"])
            return event

    def _unique_archive_path(self, event_id: str) -> Path:
        candidate = self.archive_directory / event_id
        sequence = 2
        while candidate.exists():
            candidate = self.archive_directory / f"{event_id}-{sequence}"
            sequence += 1
        return candidate

    def end_event(self) -> dict[str, Any]:
        with self._lock:
            event = self.get_active_event()
            if event is None:
                raise NoActiveEventError("There is no active event to archive.")

            timestamp = self._now()
            event["status"] = "archived"
            event["ended_at"] = timestamp
            event["updated_at"] = timestamp
            self._atomic_write_json(self.current_event_path, event)
            self._backup(event, "archived")

            destination = self._unique_archive_path(str(event["id"]))
            try:
                os.replace(self.current_directory, destination)
                self.current_directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                self.current_directory.mkdir(parents=True, exist_ok=True)
                raise EventStorageError("The event could not be moved into the archive.") from error

            LOGGER.info("Archived event %s (%s)", event["title"], event["id"])
            return event


    @staticmethod
    def _clean_archive_folder(folder_name: str) -> str:
        clean_name = str(folder_name or "").strip()
        if not _SAFE_ARCHIVE_FOLDER.fullmatch(clean_name):
            raise ArchivedEventNotFoundError("The archived event identifier is invalid.")
        return clean_name

    @staticmethod
    def _workspace_has_files(path: Path) -> bool:
        try:
            return any(item.is_file() for item in path.rglob("*"))
        except OSError as error:
            raise EventStorageError("The current event workspace could not be inspected.") from error

    def reopen_archived_event(self, folder_name: str) -> dict[str, Any]:
        """Move a completed workspace back into the active slot without losing its history."""

        with self._lock:
            if self.current_event_path.exists():
                raise ActiveEventExistsError(
                    "Finish or archive the current event before reopening another one."
                )

            clean_name = self._clean_archive_folder(folder_name)
            source = self.archive_directory / clean_name
            event_path = source / "event.json"
            if not source.is_dir() or not event_path.is_file():
                raise ArchivedEventNotFoundError("The archived event does not exist.")

            if self.current_directory.exists() and self._workspace_has_files(self.current_directory):
                raise EventStorageError(
                    "The current workspace contains recovery data and cannot be replaced safely."
                )

            original_event = self._read_json(event_path)
            event = dict(original_event)
            timestamp = self._now()
            archive_history = event.get("archive_history")
            if not isinstance(archive_history, list):
                archive_history = []
            archive_history = list(archive_history)
            archive_history.append(
                {
                    "archive_folder": clean_name,
                    "ended_at": event.get("ended_at"),
                    "reopened_at": timestamp,
                }
            )
            event["archive_history"] = archive_history[-20:]
            event["status"] = "active"
            event["ended_at"] = None
            event["reopened_at"] = timestamp
            try:
                previous_reopen_count = max(0, int(event.get("reopen_count") or 0))
            except (TypeError, ValueError):
                previous_reopen_count = 0
            event["reopen_count"] = previous_reopen_count + 1
            event["updated_at"] = timestamp
            if str(event.get("theme") or "default") not in _ALLOWED_THEMES:
                event["theme"] = "default"

            self._backup(original_event, "before-reopen")
            self._atomic_write_json(event_path, event)

            # Never revive an old live/upload state automatically. Preserve the old
            # control record for audit purposes, then let EventControlService create
            # a fresh Setup Mode control file after the workspace is active.
            control_path = source / "control.json"
            control_history_path: Path | None = None
            if control_path.exists():
                history_directory = source / "history" / "controls"
                try:
                    history_directory.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    self._atomic_write_json(event_path, original_event)
                    raise EventStorageError(
                        "The archived event history could not be prepared safely."
                    ) from error
                control_history_path = history_directory / (
                    f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-before-reopen.json"
                )
                try:
                    os.replace(control_path, control_history_path)
                except OSError as error:
                    self._atomic_write_json(event_path, original_event)
                    raise EventStorageError(
                        "The archived event controls could not be prepared safely."
                    ) from error

            try:
                if self.current_directory.exists():
                    shutil.rmtree(self.current_directory)
                os.replace(source, self.current_directory)
                self.archive_directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                self.current_directory.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    self._atomic_write_json(event_path, original_event)
                    if control_history_path and control_history_path.exists():
                        try:
                            os.replace(control_history_path, control_path)
                        except OSError:
                            LOGGER.exception("Could not restore archived controls after reopen failure")
                raise EventStorageError(
                    "The archived event could not be returned to the active workspace."
                ) from error

            self._backup(event, "reopened")
            LOGGER.info("Reopened archived event %s (%s)", event.get("title"), event.get("id"))
            return event

    def list_archived_events(self) -> list[dict[str, Any]]:
        archived: list[dict[str, Any]] = []
        with self._lock:
            for folder in self.archive_directory.iterdir():
                if not folder.is_dir():
                    continue
                event_path = folder / "event.json"
                if not event_path.exists():
                    continue
                try:
                    event = self._read_json(event_path)
                except EventStorageError:
                    LOGGER.exception("Skipping unreadable archived event at %s", event_path)
                    continue
                event = dict(event)
                event["archive_folder"] = folder.name
                archived.append(event)

        archived.sort(
            key=lambda item: str(item.get("ended_at") or item.get("created_at") or ""),
            reverse=True,
        )
        return archived

    def count_archived_events(self) -> int:
        return len(self.list_archived_events())

    @staticmethod
    def public_event(event: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if event is None:
            return None
        allowed_fields = (
            "id",
            "title",
            "hosts",
            "event_date",
            "venue",
            "status",
            "created_at",
            "updated_at",
            "theme",
        )
        public = {field: event.get(field) for field in allowed_fields}
        public["theme"] = str(public.get("theme") or "default") if str(public.get("theme") or "default") in _ALLOWED_THEMES else "default"
        return public
