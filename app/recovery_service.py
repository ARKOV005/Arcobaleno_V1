from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

LOGGER = logging.getLogger("arcobaleno.recovery")
_SAFE_BACKUP = re.compile(r"^[A-Za-z0-9._-]+\.zip$")
_CHECKPOINT_FILES = ("event.json", "photos.json", "control.json", "access.json", "timeline.json")


class RecoveryError(RuntimeError):
    """Raised when a recovery snapshot cannot be created or accessed."""


class RecoveryService:
    """Creates portable event snapshots and reports local storage health."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.current_workspace = self.runtime_root / "events" / "current"
        self.backup_root = self.runtime_root / "backups" / "manual"
        self.automatic_root = self.runtime_root / "backups" / "automatic"
        self.logs_root = self.runtime_root / "logs"
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.automatic_root.mkdir(parents=True, exist_ok=True)
        self._checkpoint_lock = threading.RLock()
        self.checkpoint_retention = 12

    def diagnostics(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.runtime_root)
        backups = self.list_backups()
        event_file = self.current_workspace / "event.json"
        photos_folder = self.current_workspace / "photos"
        photo_files = list(photos_folder.glob("*")) if photos_folder.exists() else []
        photo_bytes = sum(path.stat().st_size for path in photo_files if path.is_file())
        active_event_id = ""
        if event_file.is_file():
            try:
                import json
                payload = json.loads(event_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    active_event_id = str(payload.get("id") or "").strip()
            except (OSError, ValueError):
                active_event_id = ""
        automatic = self.list_automatic_checkpoints(active_event_id) if active_event_id else []
        return {
            "disk_total_bytes": usage.total,
            "disk_used_bytes": usage.used,
            "disk_free_bytes": usage.free,
            "disk_free_gb": round(usage.free / (1024 ** 3), 2),
            "disk_used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
            "active_event_record": event_file.exists(),
            "active_photo_files": len([path for path in photo_files if path.is_file()]),
            "active_photo_bytes": photo_bytes,
            "backup_count": len(backups),
            "latest_backup": backups[0] if backups else None,
            "automatic_checkpoint_count": len(automatic),
            "latest_automatic_checkpoint": automatic[0] if automatic else None,
            "log_available": (self.logs_root / "arcobaleno.log").is_file(),
        }

    @staticmethod
    def _safe_title(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "event")).strip("-") or "event"

    def create_backup(self, event: Mapping[str, Any]) -> Path:
        event_id = str(event.get("id") or "").strip()
        if not event_id or not (self.current_workspace / "event.json").is_file():
            raise RecoveryError("There is no active event workspace to back up.")

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        filename = f"{timestamp}-{self._safe_title(event.get('title'))}-{event_id[:8]}.zip"
        destination = self.backup_root / filename
        temporary_path = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}.tmp")
        try:
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in self.current_workspace.rglob("*"):
                    if path.is_file() and not path.name.startswith(".upload-"):
                        archive.write(path, arcname=path.relative_to(self.current_workspace))
                archive.writestr(
                    "RECOVERY-INFO.txt",
                    "ARCOBALENO manual recovery snapshot\n"
                    f"Event: {event.get('title', 'Event')}\n"
                    f"Event ID: {event_id}\n"
                    f"Created: {datetime.now().astimezone().isoformat(timespec='seconds')}\n",
                )
            os.replace(temporary_path, destination)
        except OSError as error:
            temporary_path.unlink(missing_ok=True)
            raise RecoveryError("ARCOBALENO could not create the recovery snapshot.") from error
        LOGGER.info("Created recovery snapshot %s", destination.name)
        return destination

    @staticmethod
    def _safe_reason(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "checkpoint")).strip("-") or "checkpoint"

    @staticmethod
    def _checkpoint_timestamp() -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]

    def _event_checkpoint_root(self, event_id: str) -> Path:
        clean_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(event_id or "")).strip("-")
        if not clean_id:
            raise RecoveryError("The active event does not have a valid ID.")
        return self.automatic_root / clean_id

    def _checkpoint_fingerprint(self) -> str:
        import hashlib

        digest = hashlib.sha256()
        found = False
        for name in _CHECKPOINT_FILES:
            source = self.current_workspace / name
            if not source.is_file():
                continue
            found = True
            digest.update(name.encode("utf-8"))
            try:
                digest.update(source.read_bytes())
            except OSError as error:
                raise RecoveryError("ARCOBALENO could not read the event metadata checkpoint.") from error
        if not found:
            raise RecoveryError("There is no active event metadata to protect.")
        return digest.hexdigest()

    @staticmethod
    def _friendly_checkpoint_time(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(str(value or ""))
        except ValueError:
            return "Recently"
        return parsed.astimezone().strftime("%b %d, %I:%M %p").replace(" 0", " ")

    def list_automatic_checkpoints(self, event_id: str) -> list[dict[str, Any]]:
        if not str(event_id or "").strip():
            return []
        try:
            event_root = self._event_checkpoint_root(event_id)
        except RecoveryError:
            return []
        checkpoints: list[dict[str, Any]] = []
        if not event_root.is_dir():
            return checkpoints
        for folder in event_root.iterdir():
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            info_path = folder / "CHECKPOINT.json"
            try:
                import json
                info = json.loads(info_path.read_text(encoding="utf-8"))
                if not isinstance(info, dict):
                    continue
                size_bytes = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
            except (OSError, ValueError):
                continue
            checkpoints.append(
                {
                    "name": folder.name,
                    "event_id": str(info.get("event_id") or ""),
                    "event_title": str(info.get("event_title") or "Event"),
                    "reason": str(info.get("reason") or "checkpoint"),
                    "created_at": str(info.get("created_at") or ""),
                    "created_label": self._friendly_checkpoint_time(str(info.get("created_at") or "")),
                    "fingerprint": str(info.get("fingerprint") or ""),
                    "files": list(info.get("files") or []),
                    "size_bytes": size_bytes,
                }
            )
        checkpoints.sort(key=lambda item: item["created_at"], reverse=True)
        return checkpoints

    def create_automatic_checkpoint(self, event: Mapping[str, Any], reason: str) -> dict[str, Any]:
        """Create a small rolling metadata checkpoint without duplicating photo originals."""

        event_id = str(event.get("id") or "").strip()
        if not event_id or not (self.current_workspace / "event.json").is_file():
            raise RecoveryError("There is no active event workspace to checkpoint.")

        with self._checkpoint_lock:
            fingerprint = self._checkpoint_fingerprint()
            existing = self.list_automatic_checkpoints(event_id)
            if existing and existing[0].get("fingerprint") == fingerprint:
                return {**existing[0], "created": False}

            event_root = self._event_checkpoint_root(event_id)
            event_root.mkdir(parents=True, exist_ok=True)
            timestamp = self._checkpoint_timestamp()
            clean_reason = self._safe_reason(reason)
            destination = event_root / f"{timestamp}-{clean_reason}-{secrets.token_hex(2)}"
            temporary = event_root / f".{destination.name}.{secrets.token_hex(4)}.tmp"
            copied: list[str] = []
            try:
                temporary.mkdir(parents=True, exist_ok=False)
                for name in _CHECKPOINT_FILES:
                    source = self.current_workspace / name
                    if source.is_file():
                        shutil.copy2(source, temporary / name)
                        copied.append(name)
                if not copied:
                    raise RecoveryError("There is no active event metadata to checkpoint.")

                import json
                info = {
                    "schema_version": 1,
                    "event_id": event_id,
                    "event_title": str(event.get("title") or "Event"),
                    "reason": clean_reason,
                    "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "fingerprint": fingerprint,
                    "files": copied,
                    "contains_photo_originals": False,
                }
                (temporary / "CHECKPOINT.json").write_text(
                    json.dumps(info, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, destination)
            except RecoveryError:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            except OSError as error:
                shutil.rmtree(temporary, ignore_errors=True)
                raise RecoveryError("ARCOBALENO could not create the automatic checkpoint.") from error

            checkpoints = self.list_automatic_checkpoints(event_id)
            for old in checkpoints[self.checkpoint_retention :]:
                old_path = event_root / str(old.get("name") or "")
                if old_path.is_dir():
                    shutil.rmtree(old_path, ignore_errors=True)

            LOGGER.info(
                "Created automatic metadata checkpoint %s for event %s (%s)",
                destination.name,
                event_id,
                clean_reason,
            )
            result = self.list_automatic_checkpoints(event_id)[0]
            return {**result, "created": True}

    def list_backups(self) -> list[dict[str, Any]]:
        backups: list[dict[str, Any]] = []
        for path in self.backup_root.glob("*.zip"):
            try:
                stat = path.stat()
            except OSError:
                continue
            backups.append(
                {
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                }
            )
        backups.sort(key=lambda item: item["created_at"], reverse=True)
        return backups

    def backup_file(self, filename: str) -> Path:
        clean_name = str(filename or "").strip()
        if not _SAFE_BACKUP.fullmatch(clean_name):
            raise RecoveryError("The recovery snapshot name is invalid.")
        path = self.backup_root / clean_name
        if not path.is_file():
            raise RecoveryError("The recovery snapshot does not exist.")
        return path

    def log_file(self) -> Path:
        path = self.logs_root / "arcobaleno.log"
        if not path.is_file():
            raise RecoveryError("No ARCOBALENO log file is available yet.")
        return path
