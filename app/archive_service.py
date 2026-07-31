from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("arcobaleno.archive")
_SAFE_FOLDER = re.compile(r"^[A-Za-z0-9._-]{1,180}$")
_PHOTO_ID = re.compile(r"^[a-f0-9]{24}$")
_TRASH_METADATA = ".trash.json"


class ArchiveError(RuntimeError):
    """Base class for archived-event errors."""


class ArchiveNotFoundError(ArchiveError):
    """Raised when an archived workspace cannot be found."""


class ArchiveStorageError(ArchiveError):
    """Raised when archived data cannot be read, moved, or exported."""


class ArchiveService:
    """Provides safe access to completed event workspaces and archive trash."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.archive_root = self.runtime_root / "events" / "archive"
        self.trash_root = self.runtime_root / "events" / "trash"
        self.export_root = self.runtime_root / "exports"
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.trash_root.mkdir(parents=True, exist_ok=True)
        self.export_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _clean_folder_name(folder_name: str) -> str:
        clean_name = str(folder_name or "").strip()
        if not _SAFE_FOLDER.fullmatch(clean_name):
            raise ArchiveNotFoundError("The archived event identifier is invalid.")
        return clean_name

    def _workspace(self, folder_name: str) -> Path:
        clean_name = self._clean_folder_name(folder_name)
        workspace = self.archive_root / clean_name
        if not workspace.is_dir():
            raise ArchiveNotFoundError("The archived event does not exist.")
        return workspace

    def _trash_workspace(self, folder_name: str) -> Path:
        clean_name = self._clean_folder_name(folder_name)
        workspace = self.trash_root / clean_name
        if not workspace.is_dir():
            raise ArchiveNotFoundError("The deleted event does not exist in Trash.")
        return workspace

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ArchiveNotFoundError("The archived event record is missing.") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ArchiveStorageError(f"Could not read archived data from {path.name}.") from error
        if not isinstance(payload, dict):
            raise ArchiveStorageError(f"Archived data in {path.name} is invalid.")
        return payload

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise ArchiveStorageError(f"Could not write archived data to {path.name}.") from error

    @staticmethod
    def _event_from_workspace(workspace: Path) -> dict[str, Any]:
        event = ArchiveService._read_json(workspace / "event.json")
        event = dict(event)
        return event

    @staticmethod
    def _photos_from_workspace(workspace: Path) -> list[dict[str, Any]]:
        manifest_path = workspace / "photos.json"
        if not manifest_path.exists():
            return []
        manifest = ArchiveService._read_json(manifest_path)
        records = manifest.get("photos", [])
        if not isinstance(records, list):
            raise ArchiveStorageError("The archived photo manifest is invalid.")
        photos = [dict(record) for record in records if isinstance(record, dict)]
        photos.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return photos

    @staticmethod
    def _unique_destination(root: Path, preferred_name: str) -> Path:
        candidate = root / preferred_name
        sequence = 2
        while candidate.exists():
            candidate = root / f"{preferred_name}-{sequence}"
            sequence += 1
        return candidate

    def event(self, folder_name: str) -> dict[str, Any]:
        workspace = self._workspace(folder_name)
        event = self._event_from_workspace(workspace)
        event["archive_folder"] = workspace.name
        return event

    def photos(self, folder_name: str) -> list[dict[str, Any]]:
        workspace = self._workspace(folder_name)
        return self._photos_from_workspace(workspace)

    def photo_file(self, folder_name: str, photo_id: str) -> tuple[dict[str, Any], Path]:
        if not _PHOTO_ID.fullmatch(str(photo_id or "")):
            raise ArchiveNotFoundError("The archived photo identifier is invalid.")
        workspace = self._workspace(folder_name)
        for record in self.photos(folder_name):
            if record.get("id") != photo_id:
                continue
            filename = Path(str(record.get("filename") or "")).name
            if not filename:
                break
            photo_path = workspace / "photos" / filename
            if photo_path.is_file():
                return record, photo_path
            break
        raise ArchiveNotFoundError("The archived photo does not exist.")

    def move_to_trash(self, folder_name: str) -> dict[str, Any]:
        workspace = self._workspace(folder_name)
        event = self._event_from_workspace(workspace)
        destination = self._unique_destination(self.trash_root, workspace.name)
        metadata_path = workspace / _TRASH_METADATA
        metadata = {
            "original_archive_folder": workspace.name,
            "deleted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self._write_json(metadata_path, metadata)
        try:
            os.replace(workspace, destination)
        except OSError as error:
            metadata_path.unlink(missing_ok=True)
            raise ArchiveStorageError("The archived event could not be moved to Trash.") from error

        event = dict(event)
        event["trash_folder"] = destination.name
        event["deleted_at"] = metadata["deleted_at"]
        LOGGER.warning(
            "Moved archived event %s (%s) to Trash",
            event.get("title", "Event"),
            destination.name,
        )
        return event

    def list_trashed_events(self) -> list[dict[str, Any]]:
        deleted: list[dict[str, Any]] = []
        try:
            folders = list(self.trash_root.iterdir())
        except OSError as error:
            raise ArchiveStorageError("The archive Trash could not be read.") from error

        for folder in folders:
            if not folder.is_dir():
                continue
            event_path = folder / "event.json"
            if not event_path.exists():
                continue
            try:
                event = self._event_from_workspace(folder)
                photos = self._photos_from_workspace(folder)
                metadata_path = folder / _TRASH_METADATA
                metadata = self._read_json(metadata_path) if metadata_path.exists() else {}
            except (ArchiveNotFoundError, ArchiveStorageError):
                LOGGER.exception("Skipping unreadable deleted event at %s", folder)
                continue

            event["trash_folder"] = folder.name
            event["deleted_at"] = metadata.get("deleted_at")
            event["original_archive_folder"] = metadata.get(
                "original_archive_folder", folder.name
            )
            event["photo_count"] = len(photos)
            event["guest_count"] = len(
                {
                    str(photo.get("guest_name") or "").strip().casefold()
                    for photo in photos
                    if str(photo.get("guest_name") or "").strip()
                }
            )
            deleted.append(event)

        deleted.sort(
            key=lambda item: str(item.get("deleted_at") or item.get("ended_at") or ""),
            reverse=True,
        )
        return deleted

    def count_trashed_events(self) -> int:
        return len(self.list_trashed_events())

    def restore_from_trash(self, folder_name: str) -> dict[str, Any]:
        workspace = self._trash_workspace(folder_name)
        event = self._event_from_workspace(workspace)
        metadata_path = workspace / _TRASH_METADATA
        metadata = self._read_json(metadata_path) if metadata_path.exists() else {}
        preferred_name = str(metadata.get("original_archive_folder") or workspace.name)
        preferred_name = self._clean_folder_name(preferred_name)
        destination = self._unique_destination(self.archive_root, preferred_name)

        try:
            os.replace(workspace, destination)
            (destination / _TRASH_METADATA).unlink(missing_ok=True)
        except OSError as error:
            raise ArchiveStorageError("The deleted event could not be restored.") from error

        event = dict(event)
        event["archive_folder"] = destination.name
        LOGGER.info(
            "Restored deleted event %s (%s) to the archive",
            event.get("title", "Event"),
            destination.name,
        )
        return event

    def permanently_delete(self, folder_name: str) -> dict[str, Any]:
        workspace = self._trash_workspace(folder_name)
        event = self._event_from_workspace(workspace)
        try:
            shutil.rmtree(workspace)
        except OSError as error:
            raise ArchiveStorageError("The deleted event could not be permanently removed.") from error

        LOGGER.warning(
            "Permanently deleted event %s (%s)",
            event.get("title", "Event"),
            workspace.name,
        )
        return event

    @staticmethod
    def _safe_title(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "event")).strip("-") or "event"

    def create_export(self, folder_name: str) -> Path:
        workspace = self._workspace(folder_name)
        event = self.event(folder_name)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        export_path = self.export_root / (
            f"{timestamp}-{self._safe_title(event.get('title'))}-{workspace.name}.zip"
        )
        temporary_path = export_path.with_name(f".{export_path.name}.{secrets.token_hex(4)}.tmp")
        try:
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in workspace.rglob("*"):
                    if path.is_file() and not path.name.startswith(".upload-"):
                        archive.write(path, arcname=path.relative_to(workspace))
                archive.writestr(
                    "EXPORT-INFO.txt",
                    "ARCOBALENO archived event export\n"
                    f"Event: {event.get('title', 'Event')}\n"
                    f"Archived workspace: {workspace.name}\n"
                    f"Exported: {datetime.now().astimezone().isoformat(timespec='seconds')}\n",
                )
            os.replace(temporary_path, export_path)
        except OSError as error:
            temporary_path.unlink(missing_ok=True)
            raise ArchiveStorageError("The archived event could not be exported.") from error

        LOGGER.info("Created archived event export %s", export_path.name)
        return export_path
