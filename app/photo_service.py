from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pillow_heif = None

LOGGER = logging.getLogger("arcobaleno.photos")
_MANIFEST_SCHEMA_VERSION = 3
_MAX_GUEST_NAME_LENGTH = 80
_UPLOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,120}$")
_PHOTO_ID_PATTERN = re.compile(r"^[a-f0-9]{24}$")
_ALLOWED_MODERATION_MODES = {"family", "instant", "curated"}
_FEED_HISTORY_LIMIT = 2500
_THUMBNAIL_MAX_SIZE = (480, 480)
_THUMBNAIL_QUALITY = 72
_MAX_IMAGE_PIXELS = 150_000_000
_MAX_IMAGE_EDGE = 32_768
_IMAGE_FORMATS_BY_EXTENSION = {
    ".jpg": {"JPEG"},
    ".png": {"PNG"},
    ".webp": {"WEBP"},
    ".heic": {"HEIF", "HEIC"},
    ".avif": {"AVIF", "HEIF"},
}


class PhotoError(RuntimeError):
    """Base class for photo storage errors."""


class PhotoValidationError(PhotoError):
    """Raised when a guest upload is missing or invalid."""


class PhotoLimitReachedError(PhotoError):
    """Raised when a device has reached the event photo limit."""


class PhotoIntegrityError(PhotoValidationError):
    """Raised when an uploaded image is corrupt, unsafe, or cannot be decoded."""


class PhotoStorageError(PhotoError):
    """Raised when a photo cannot be saved safely."""


class PhotoCapacityError(PhotoStorageError):
    """Raised when the event drive cannot safely accept another photo."""


class NoActivePhotoWorkspaceError(PhotoError):
    """Raised when photo operations are attempted without an active event."""


class PhotoNotFoundError(PhotoError):
    """Raised when a slideshow photo record or file cannot be found."""


class PhotoService:
    """Stores originals, lightweight thumbnails, moderation state, and a durable feed."""

    def __init__(self, runtime_root: Path, max_upload_bytes: int, photo_limit: int):
        self.runtime_root = Path(runtime_root)
        self.current_directory = self.runtime_root / "events" / "current"
        self.photos_directory = self.current_directory / "photos"
        self.thumbnails_directory = self.current_directory / "thumbnails"
        self.manifest_path = self.current_directory / "photos.json"
        self.max_upload_bytes = max_upload_bytes
        self.photo_limit = photo_limit
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")

    @staticmethod
    def _clean_guest_name(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @staticmethod
    def _device_hash(device_id: str) -> str:
        return hashlib.sha256(device_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_capacity_error(error: BaseException) -> bool:
        return isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}

    @classmethod
    def _storage_exception(cls, error: BaseException, message: str) -> PhotoStorageError:
        if cls._is_capacity_error(error):
            return PhotoCapacityError(
                "The event server is out of safe storage space. Free space before guests continue."
            )
        return PhotoStorageError(message)

    @classmethod
    def _atomic_write_json(cls, path: Path, payload: Mapping[str, Any]) -> None:
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
            raise cls._storage_exception(error, f"Could not safely write {path}.") from error

    def _empty_manifest(self, event_id: str) -> dict[str, Any]:
        return {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "event_id": event_id,
            "updated_at": self._now(),
            "feed_sequence": 0,
            "feed_changes": [],
            "quota_resets": {},
            "quota_reset_at": "",
            "photos": [],
        }

    @staticmethod
    def _normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(record)
        item.setdefault("moderation_status", "approved")
        if item.get("moderation_status") not in {"pending", "approved", "hidden"}:
            item["moderation_status"] = "approved"
        item.setdefault("moderation_mode", "instant")
        item.setdefault("auto_approve_at", "")
        item.setdefault("reviewed_at", "")
        item.setdefault("review_source", "")
        item["favorite"] = bool(item.get("favorite", False))
        return item

    def _upgrade_manifest(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        changed = payload.get("schema_version") != _MANIFEST_SCHEMA_VERSION
        photos: list[dict[str, Any]] = []
        for raw in payload.get("photos", []):
            if not isinstance(raw, dict):
                changed = True
                continue
            normalized = self._normalize_record(raw)
            if normalized != raw:
                changed = True
            photos.append(normalized)
        payload["photos"] = photos
        payload["schema_version"] = _MANIFEST_SCHEMA_VERSION

        try:
            feed_sequence = max(0, int(payload.get("feed_sequence") or 0))
        except (TypeError, ValueError):
            feed_sequence = 0
            changed = True
        feed_changes = payload.get("feed_changes")
        if not isinstance(feed_changes, list):
            feed_changes = []
            changed = True

        if not feed_changes and photos:
            for record in photos[-_FEED_HISTORY_LIMIT:]:
                if record.get("moderation_status") != "approved":
                    continue
                feed_sequence += 1
                feed_changes.append(
                    {
                        "sequence": feed_sequence,
                        "action": "upsert",
                        "photo_id": str(record.get("id") or ""),
                    }
                )
            changed = True

        payload["feed_sequence"] = feed_sequence
        payload["feed_changes"] = [
            dict(item) for item in feed_changes[-_FEED_HISTORY_LIMIT:] if isinstance(item, dict)
        ]

        raw_resets = payload.get("quota_resets")
        quota_resets: dict[str, int] = {}
        if isinstance(raw_resets, dict):
            for raw_device_hash, raw_baseline in raw_resets.items():
                device_hash = str(raw_device_hash or "").strip().casefold()
                if not re.fullmatch(r"[a-f0-9]{64}", device_hash):
                    changed = True
                    continue
                try:
                    baseline = max(0, int(raw_baseline))
                except (TypeError, ValueError):
                    changed = True
                    continue
                quota_resets[device_hash] = baseline
        elif raw_resets is not None:
            changed = True
        payload["quota_resets"] = quota_resets
        payload["quota_reset_at"] = str(payload.get("quota_reset_at") or "")
        payload.setdefault("updated_at", self._now())
        return payload, changed

    def _load_manifest(self, event_id: str) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return self._empty_manifest(event_id)

        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PhotoStorageError("The current photo manifest could not be read.") from error

        if not isinstance(payload, dict) or not isinstance(payload.get("photos"), list):
            raise PhotoStorageError("The current photo manifest is invalid.")
        if str(payload.get("event_id") or "") != event_id:
            raise PhotoStorageError("The current photo manifest belongs to another event.")

        payload, changed = self._upgrade_manifest(payload)
        if changed:
            payload["updated_at"] = self._now()
            self._atomic_write_json(self.manifest_path, payload)
        return payload

    @staticmethod
    def _detect_image_type(header: bytes) -> tuple[str, str]:
        if header.startswith(b"\xff\xd8\xff"):
            return ".jpg", "image/jpeg"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png", "image/png"
        if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return ".webp", "image/webp"
        if len(header) >= 12 and header[4:8] == b"ftyp":
            brand = header[8:12].lower()
            if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
                return ".heic", "image/heic"
            if brand in {b"avif", b"avis"}:
                return ".avif", "image/avif"
        raise PhotoValidationError(
            "That file is not a supported camera image. Use JPEG, PNG, WebP, HEIC, or AVIF."
        )

    @staticmethod
    def _integrity_message() -> str:
        return "This photo could not be read safely. Retake it or choose another photo."

    def _validate_image_integrity(self, source_path: Path, extension: str) -> tuple[int, int]:
        """Fully decode an upload before it becomes part of the event workspace."""

        expected_formats = _IMAGE_FORMATS_BY_EXTENSION.get(extension, set())
        try:
            with Image.open(source_path) as image:
                detected_format = str(image.format or "").upper()
                if expected_formats and detected_format not in expected_formats:
                    raise PhotoIntegrityError(self._integrity_message())

                # Multi-image HEIF containers are common on phones. ARCOBALENO
                # validates and displays the primary frame instead of rejecting them.
                if int(getattr(image, "n_frames", 1) or 1) > 1:
                    image.seek(0)

                width, height = (int(image.width), int(image.height))
                if width <= 0 or height <= 0:
                    raise PhotoIntegrityError(self._integrity_message())
                if width > _MAX_IMAGE_EDGE or height > _MAX_IMAGE_EDGE:
                    raise PhotoIntegrityError(
                        "That photo is too large to process safely. Choose a smaller version."
                    )
                if width * height > _MAX_IMAGE_PIXELS:
                    raise PhotoIntegrityError(
                        "That photo has too many pixels to process safely. Choose a smaller version."
                    )

                # load() forces Pillow to decode the complete displayed frame. This
                # catches truncated files that have a convincing image header.
                image.load()
                return width, height
        except PhotoError:
            raise
        except (Image.DecompressionBombError, UnidentifiedImageError, SyntaxError, ValueError) as error:
            raise PhotoIntegrityError(self._integrity_message()) from error
        except OSError as error:
            if self._is_capacity_error(error):
                raise self._storage_exception(
                    error,
                    "ARCOBALENO could not validate the uploaded photo safely.",
                ) from error
            raise PhotoIntegrityError(self._integrity_message()) from error

    def _stream_to_temporary_file(self, stream: BinaryIO) -> tuple[Path, int, str]:
        self.photos_directory.mkdir(parents=True, exist_ok=True)
        temporary_path = self.photos_directory / f".upload-{secrets.token_hex(12)}.tmp"
        digest = hashlib.sha256()
        total = 0

        try:
            with temporary_path.open("xb") as destination:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise PhotoValidationError(
                            f"The photo is larger than {self.max_upload_bytes // (1024 * 1024)} MB."
                        )
                    destination.write(chunk)
                    digest.update(chunk)

            if total == 0:
                raise PhotoValidationError("Choose or take a photo before sending.")
            return temporary_path, total, digest.hexdigest()
        except PhotoError:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except OSError as error:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise self._storage_exception(
                error,
                "ARCOBALENO could not write the temporary upload safely.",
            ) from error

    @staticmethod
    def _device_upload_counts(manifest: Mapping[str, Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in manifest.get("photos", []):
            if not isinstance(item, dict):
                continue
            device_hash = str(item.get("device_hash") or "").strip().casefold()
            if not re.fullmatch(r"[a-f0-9]{64}", device_hash):
                continue
            counts[device_hash] = counts.get(device_hash, 0) + 1
        return counts

    @staticmethod
    def _quota_baseline(manifest: Mapping[str, Any], device_hash: str) -> int:
        resets = manifest.get("quota_resets")
        if not isinstance(resets, Mapping):
            return 0
        try:
            return max(0, int(resets.get(device_hash, 0)))
        except (TypeError, ValueError):
            return 0

    def _remaining_from_manifest(self, manifest: Mapping[str, Any], device_hash: str) -> int | None:
        if self.photo_limit == 0:
            return None
        used = self._device_upload_counts(manifest).get(device_hash, 0)
        baseline = self._quota_baseline(manifest, device_hash)
        used_since_reset = max(0, used - baseline)
        return max(0, self.photo_limit - used_since_reset)

    def allowance_summary(self, event_id: str) -> dict[str, int | bool]:
        """Return operator-facing information about per-device shot allowances."""

        with self._lock:
            manifest = self._load_manifest(event_id)
            counts = self._device_upload_counts(manifest)
            consumed = {
                device_hash: max(0, used - self._quota_baseline(manifest, device_hash))
                for device_hash, used in counts.items()
            }
            return {
                "photo_limit": int(self.photo_limit),
                "unlimited": self.photo_limit == 0,
                "device_count": len(counts),
                "devices_with_used_allowance": sum(1 for value in consumed.values() if value > 0),
                "exhausted_devices": (
                    0
                    if self.photo_limit == 0
                    else sum(1 for value in consumed.values() if value >= self.photo_limit)
                ),
            }

    def reset_guest_allowances(self, event_id: str) -> dict[str, int | bool]:
        """Give known guest devices the full current allowance without deleting photos."""

        with self._lock:
            manifest = self._load_manifest(event_id)
            if self.photo_limit == 0:
                return {
                    "photo_limit": 0,
                    "unlimited": True,
                    "devices_reset": 0,
                    "changed": False,
                }

            counts = self._device_upload_counts(manifest)
            resets = dict(manifest.get("quota_resets") or {})
            devices_reset = 0
            for device_hash, used in counts.items():
                baseline = self._quota_baseline(manifest, device_hash)
                if max(0, used - baseline) <= 0:
                    continue
                resets[device_hash] = used
                devices_reset += 1

            if devices_reset == 0:
                return {
                    "photo_limit": int(self.photo_limit),
                    "unlimited": False,
                    "devices_reset": 0,
                    "changed": False,
                }

            manifest["quota_resets"] = resets
            manifest["quota_reset_at"] = self._now()
            manifest["updated_at"] = self._now()
            self._atomic_write_json(self.manifest_path, manifest)
            LOGGER.info(
                "Reset guest shot allowances for %s device(s) in event %s",
                devices_reset,
                event_id,
            )
            return {
                "photo_limit": int(self.photo_limit),
                "unlimited": False,
                "devices_reset": devices_reset,
                "changed": True,
            }

    @staticmethod
    def _thumbnail_filename(photo_id: str) -> str:
        return f"{photo_id}.jpg"

    def _make_thumbnail(self, source_path: Path, photo_id: str) -> Path:
        self.thumbnails_directory.mkdir(parents=True, exist_ok=True)
        destination = self.thumbnails_directory / self._thumbnail_filename(photo_id)
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}.tmp")
        try:
            with Image.open(source_path) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode not in {"RGB", "L"}:
                    background = Image.new("RGB", image.size, "white")
                    if "A" in image.getbands():
                        background.paste(image, mask=image.getchannel("A"))
                    else:
                        background.paste(image)
                    image = background
                elif image.mode == "L":
                    image = image.convert("RGB")
                image.thumbnail(_THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)
                image.save(
                    temporary,
                    format="JPEG",
                    quality=_THUMBNAIL_QUALITY,
                    optimize=True,
                    progressive=True,
                )
            os.replace(temporary, destination)
            return destination
        except Exception as error:  # noqa: BLE001
            temporary.unlink(missing_ok=True)
            if isinstance(error, PhotoError):
                raise
            if isinstance(error, OSError):
                raise self._storage_exception(
                    error,
                    "The photo thumbnail could not be created safely.",
                ) from error
            raise PhotoStorageError("The photo thumbnail could not be created.") from error

    @staticmethod
    def _append_feed_change(manifest: dict[str, Any], action: str, photo_id: str) -> int:
        sequence = int(manifest.get("feed_sequence") or 0) + 1
        changes = manifest.setdefault("feed_changes", [])
        changes.append({"sequence": sequence, "action": action, "photo_id": photo_id})
        if len(changes) > _FEED_HISTORY_LIMIT:
            del changes[:-_FEED_HISTORY_LIMIT]
        manifest["feed_sequence"] = sequence
        return sequence

    def _promote_due_photos(self, manifest: dict[str, Any]) -> bool:
        now = datetime.now().astimezone()
        changed = False
        for record in manifest.get("photos", []):
            if not isinstance(record, dict) or record.get("moderation_status") != "pending":
                continue
            auto_at = str(record.get("auto_approve_at") or "")
            if not auto_at:
                continue
            try:
                due = datetime.fromisoformat(auto_at)
            except ValueError:
                continue
            if due > now:
                continue
            record["moderation_status"] = "approved"
            record["reviewed_at"] = self._now()
            record["review_source"] = "automatic-buffer"
            self._append_feed_change(manifest, "upsert", str(record.get("id") or ""))
            changed = True
        if changed:
            manifest["updated_at"] = self._now()
        return changed

    def status(self, event_id: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            manifest = self._load_manifest(event_id)
            remaining = self._remaining_from_manifest(manifest, self._device_hash(device_id))
            return {
                "total_photos": len(manifest["photos"]),
                "photo_limit": self.photo_limit,
                "remaining": remaining,
                "unlimited": remaining is None,
                "allowed": remaining is None or remaining > 0,
            }

    def total_photos(self, event_id: str) -> int:
        with self._lock:
            return len(self._load_manifest(event_id)["photos"])

    def counts(self, event_id: str) -> dict[str, int]:
        with self._lock:
            manifest = self._load_manifest(event_id)
            if self._promote_due_photos(manifest):
                self._atomic_write_json(self.manifest_path, manifest)
            photos = [item for item in manifest.get("photos", []) if isinstance(item, dict)]
            return {
                "total": len(photos),
                "approved": sum(1 for item in photos if item.get("moderation_status") == "approved"),
                "pending": sum(1 for item in photos if item.get("moderation_status") == "pending"),
                "hidden": sum(1 for item in photos if item.get("moderation_status") == "hidden"),
                "favorites": sum(1 for item in photos if bool(item.get("favorite"))),
            }

    def list_photos(
        self,
        event_id: str,
        *,
        limit: int | None = None,
        before: str = "",
        around: str = "",
        window_minutes: int = 15,
    ) -> list[dict[str, Any]]:
        """Return current-event photos newest first, optionally in lightweight batches."""

        with self._lock:
            manifest = self._load_manifest(event_id)
            if self._promote_due_photos(manifest):
                self._atomic_write_json(self.manifest_path, manifest)
            photos = [self._normalize_record(item) for item in manifest["photos"] if isinstance(item, dict)]

        photos.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if before:
            photos = [item for item in photos if str(item.get("created_at") or "") < before]
        if around:
            try:
                center = datetime.fromisoformat(around)
                if center.tzinfo is None:
                    center = center.astimezone()
                delta = timedelta(minutes=max(1, min(120, int(window_minutes))))
                photos = [
                    item
                    for item in photos
                    if self._record_datetime(item) is not None
                    and center - delta <= self._record_datetime(item) <= center + delta
                ]
            except (TypeError, ValueError):
                pass
        if limit is not None:
            photos = photos[: max(1, min(250, int(limit)))]
        return photos

    @staticmethod
    def _record_datetime(record: Mapping[str, Any]) -> datetime | None:
        try:
            value = datetime.fromisoformat(str(record.get("created_at") or ""))
        except ValueError:
            return None
        if value.tzinfo is None:
            value = value.astimezone()
        return value

    def unique_guest_count(self, event_id: str) -> int:
        with self._lock:
            manifest = self._load_manifest(event_id)
            guests = {
                str(item.get("guest_name") or "").strip().casefold()
                for item in manifest.get("photos", [])
                if isinstance(item, dict) and str(item.get("guest_name") or "").strip()
            }
        return len(guests)

    def latest_photo(self, event_id: str) -> dict[str, Any] | None:
        photos = self.list_photos(event_id, limit=1)
        return photos[0] if photos else None

    def save_upload(
        self,
        *,
        event_id: str,
        stream: BinaryIO,
        device_id: str,
        guest_name: Any,
        client_upload_id: Any,
        original_filename: str = "",
        moderation_mode: str = "family",
        moderation_delay_seconds: int = 10,
    ) -> dict[str, Any]:
        name = self._clean_guest_name(guest_name)
        if not name:
            raise PhotoValidationError("Enter your name before sending the photo.")
        if len(name) > _MAX_GUEST_NAME_LENGTH:
            raise PhotoValidationError(
                f"Guest names must use {_MAX_GUEST_NAME_LENGTH} characters or fewer."
            )

        upload_id = str(client_upload_id or "").strip()
        if not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise PhotoValidationError("The upload session expired. Retake the photo and try again.")

        clean_mode = moderation_mode if moderation_mode in _ALLOWED_MODERATION_MODES else "family"
        delay = max(0, min(120, int(moderation_delay_seconds or 0)))
        device_hash = self._device_hash(device_id)

        with self._lock:
            manifest = self._load_manifest(event_id)

            for existing in manifest["photos"]:
                if (
                    isinstance(existing, dict)
                    and existing.get("device_hash") == device_hash
                    and existing.get("client_upload_id") == upload_id
                ):
                    result = self._normalize_record(existing)
                    result["duplicate"] = True
                    result.update(self.status(event_id, device_id))
                    return result

            remaining_before = self._remaining_from_manifest(manifest, device_hash)
            if remaining_before == 0:
                raise PhotoLimitReachedError("This device has reached the event photo limit.")

            temporary_path, size_bytes, sha256 = self._stream_to_temporary_file(stream)
            final_path: Path | None = None
            thumbnail_path: Path | None = None
            committed = False
            try:
                with temporary_path.open("rb") as source:
                    extension, media_type = self._detect_image_type(source.read(32))
                image_width, image_height = self._validate_image_integrity(
                    temporary_path,
                    extension,
                )

                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
                photo_id = secrets.token_hex(12)
                filename = f"{timestamp}-{photo_id[:8]}{extension}"
                final_path = self.photos_directory / filename
                os.replace(temporary_path, final_path)

                moderation_status = "approved" if clean_mode == "instant" else "pending"
                auto_approve_at = ""
                if clean_mode == "family":
                    if delay == 0:
                        moderation_status = "approved"
                    else:
                        auto_approve_at = (
                            datetime.now().astimezone() + timedelta(seconds=delay)
                        ).isoformat(timespec="milliseconds")

                record = {
                    "id": photo_id,
                    "event_id": event_id,
                    "filename": filename,
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "width": image_width,
                    "height": image_height,
                    "guest_name": name,
                    "device_hash": device_hash,
                    "client_upload_id": upload_id,
                    "original_filename": Path(original_filename or "camera-photo").name[:180],
                    "created_at": self._now(),
                    "moderation_status": moderation_status,
                    "moderation_mode": clean_mode,
                    "auto_approve_at": auto_approve_at,
                    "reviewed_at": self._now() if moderation_status == "approved" else "",
                    "review_source": "instant" if moderation_status == "approved" else "",
                    "favorite": False,
                }
                manifest["photos"].append(record)
                if moderation_status == "approved":
                    self._append_feed_change(manifest, "upsert", photo_id)
                manifest["updated_at"] = self._now()

                # A decodable thumbnail is required for safe, lightweight live
                # moderation. Rejecting unreadable images prevents broken cards
                # and avoids loading full originals in the operator browser.
                thumbnail_path = self._make_thumbnail(final_path, photo_id)
                self._atomic_write_json(self.manifest_path, manifest)
                committed = True

                LOGGER.info(
                    "Stored photo %s for event %s from %s (%s bytes, %s)",
                    photo_id,
                    event_id,
                    name,
                    size_bytes,
                    moderation_status,
                )
            except Exception as error:  # noqa: BLE001
                temporary_path.unlink(missing_ok=True)
                if not committed:
                    if final_path is not None:
                        final_path.unlink(missing_ok=True)
                    if thumbnail_path is not None:
                        thumbnail_path.unlink(missing_ok=True)
                if isinstance(error, PhotoError):
                    raise
                if isinstance(error, OSError):
                    raise self._storage_exception(
                        error,
                        "ARCOBALENO could not commit the photo safely.",
                    ) from error
                raise

            result = dict(record)
            result["duplicate"] = False
            result.update(self.status(event_id, device_id))
            return result

    def cleanup_temporary_files(self) -> int:
        """Remove abandoned upload and thumbnail temp files left by an interrupted process."""

        removed = 0
        candidates = []
        if self.photos_directory.exists():
            candidates.extend(self.photos_directory.glob(".upload-*.tmp"))
        if self.thumbnails_directory.exists():
            candidates.extend(self.thumbnails_directory.glob(".*.tmp"))

        for candidate in candidates:
            try:
                candidate.unlink(missing_ok=True)
                removed += 1
            except OSError:
                LOGGER.warning("Could not remove abandoned temporary file %s", candidate)
        return removed

    @staticmethod
    def _public_photo(record: Mapping[str, Any]) -> dict[str, Any]:
        """Return only slideshow-safe photo metadata."""

        return {
            "id": str(record.get("id") or ""),
            "media_type": str(record.get("media_type") or "application/octet-stream"),
            "size_bytes": int(record.get("size_bytes") or 0),
            "guest_name": str(record.get("guest_name") or "Guest"),
            "created_at": str(record.get("created_at") or ""),
        }

    @staticmethod
    def operator_photo(record: Mapping[str, Any]) -> dict[str, Any]:
        item = PhotoService._normalize_record(record)
        return {
            "id": str(item.get("id") or ""),
            "filename": str(item.get("filename") or ""),
            "media_type": str(item.get("media_type") or "application/octet-stream"),
            "size_bytes": int(item.get("size_bytes") or 0),
            "guest_name": str(item.get("guest_name") or "Guest"),
            "created_at": str(item.get("created_at") or ""),
            "moderation_status": str(item.get("moderation_status") or "approved"),
            "moderation_mode": str(item.get("moderation_mode") or "instant"),
            "auto_approve_at": str(item.get("auto_approve_at") or ""),
            "favorite": bool(item.get("favorite")),
            "session_key": str(item.get("device_hash") or "")[:10],
        }

    def slideshow_feed(self, event_id: str, cursor: int = 0) -> dict[str, Any]:
        """Return compact slideshow feed changes after a durable sequence cursor."""

        with self._lock:
            manifest = self._load_manifest(event_id)
            if self._promote_due_photos(manifest):
                self._atomic_write_json(self.manifest_path, manifest)

            records = {
                str(item.get("id") or ""): item
                for item in manifest.get("photos", [])
                if isinstance(item, dict) and item.get("id")
            }
            sequence = int(manifest.get("feed_sequence") or 0)
            requested = max(0, int(cursor))
            changes = [dict(item) for item in manifest.get("feed_changes", []) if isinstance(item, dict)]
            oldest = int(changes[0].get("sequence") or sequence) if changes else sequence
            reset = requested > sequence or (requested and requested < max(0, oldest - 1))

            if reset or requested == 0:
                photos = [
                    self._public_photo(record)
                    for record in records.values()
                    if record.get("moderation_status") == "approved"
                ]
                photos.sort(key=lambda item: str(item.get("created_at") or ""))
                removed_ids: list[str] = []
            else:
                latest_actions: dict[str, str] = {}
                action_order: list[str] = []
                for change in changes:
                    if int(change.get("sequence") or 0) <= requested:
                        continue
                    photo_id = str(change.get("photo_id") or "")
                    if not photo_id:
                        continue
                    if photo_id not in latest_actions:
                        action_order.append(photo_id)
                    latest_actions[photo_id] = str(change.get("action") or "")
                photos = []
                removed_ids = []
                for photo_id in action_order:
                    action = latest_actions[photo_id]
                    record = records.get(photo_id)
                    if action == "upsert" and record and record.get("moderation_status") == "approved":
                        photos.append(self._public_photo(record))
                    else:
                        removed_ids.append(photo_id)

            approved_total = sum(
                1 for record in records.values() if record.get("moderation_status") == "approved"
            )
            return {
                "event_id": event_id,
                "cursor": sequence,
                "total_photos": approved_total,
                "reset": bool(reset or requested == 0),
                "updated_at": str(manifest.get("updated_at") or ""),
                "photos": photos,
                "removed_ids": removed_ids,
            }

    def moderate_photo(self, event_id: str, photo_id: str, action: str) -> dict[str, Any]:
        clean_id = str(photo_id or "").strip()
        if not _PHOTO_ID_PATTERN.fullmatch(clean_id):
            raise PhotoNotFoundError("The photo identifier is invalid.")
        clean_action = str(action or "").strip().casefold()
        if clean_action not in {"approve", "hide", "unhide", "favorite", "unfavorite"}:
            raise PhotoValidationError("That moderation action is not supported.")

        with self._lock:
            manifest = self._load_manifest(event_id)
            for record in manifest.get("photos", []):
                if not isinstance(record, dict) or record.get("id") != clean_id:
                    continue
                if clean_action == "approve":
                    record["moderation_status"] = "approved"
                    record["auto_approve_at"] = ""
                    record["reviewed_at"] = self._now()
                    record["review_source"] = "operator"
                    self._append_feed_change(manifest, "upsert", clean_id)
                elif clean_action == "hide":
                    record["moderation_status"] = "hidden"
                    record["auto_approve_at"] = ""
                    record["reviewed_at"] = self._now()
                    record["review_source"] = "operator"
                    self._append_feed_change(manifest, "remove", clean_id)
                elif clean_action == "unhide":
                    record["moderation_status"] = "approved"
                    record["reviewed_at"] = self._now()
                    record["review_source"] = "operator"
                    self._append_feed_change(manifest, "upsert", clean_id)
                elif clean_action == "favorite":
                    record["favorite"] = True
                elif clean_action == "unfavorite":
                    record["favorite"] = False
                manifest["updated_at"] = self._now()
                self._atomic_write_json(self.manifest_path, manifest)
                return self._normalize_record(record)
        raise PhotoNotFoundError("The photo does not exist.")

    def delete_photo(self, event_id: str, photo_id: str) -> dict[str, Any]:
        """Delete one current-event photo, thumbnail, and manifest entry."""

        clean_id = str(photo_id or "").strip()
        if not _PHOTO_ID_PATTERN.fullmatch(clean_id):
            raise PhotoNotFoundError("The photo identifier is invalid.")

        with self._lock:
            manifest = self._load_manifest(event_id)
            photos = manifest.get("photos", [])
            if not isinstance(photos, list):
                raise PhotoStorageError("The current photo manifest is invalid.")

            for index, record in enumerate(photos):
                if not isinstance(record, dict) or record.get("id") != clean_id:
                    continue

                filename = Path(str(record.get("filename") or "")).name
                if not filename:
                    raise PhotoNotFoundError("The photo file name is missing.")
                path = self.photos_directory / filename
                thumbnail = self.thumbnails_directory / self._thumbnail_filename(clean_id)
                try:
                    path.unlink(missing_ok=True)
                    thumbnail.unlink(missing_ok=True)
                except OSError as error:
                    raise PhotoStorageError("The photo file could not be removed.") from error

                deleted = dict(record)
                del photos[index]
                self._append_feed_change(manifest, "remove", clean_id)
                manifest["updated_at"] = self._now()
                self._atomic_write_json(self.manifest_path, manifest)
                LOGGER.info("Deleted photo %s for event %s", clean_id, event_id)
                return deleted

        raise PhotoNotFoundError("The photo does not exist.")

    def create_event_export(self, event: Mapping[str, Any]) -> Path:
        """Create a zip export containing event metadata and original photos."""

        event_id = str(event.get("id") or "").strip()
        if not event_id:
            raise PhotoStorageError("The event identifier is invalid.")

        export_root = self.runtime_root / "exports"
        export_root.mkdir(parents=True, exist_ok=True)

        safe_title = re.sub(r"[^A-Za-z0-9._-]+", "-", str(event.get("title") or "event")).strip("-") or "event"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = export_root / f"{timestamp}-{safe_title}-{event_id[:8]}.zip"

        manifest = self._load_manifest(event_id)
        try:
            import zipfile

            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for metadata_name in ("event.json", "photos.json", "control.json", "timeline.json", "access.json"):
                    metadata_path = self.current_directory / metadata_name
                    if metadata_path.exists():
                        archive.write(metadata_path, arcname=metadata_name)
                archive.writestr(
                    "README.txt",
                    "ARCOBALENO event export\n"
                    f"Event: {event.get('title', 'Event')}\n"
                    f"Exported: {self._now()}\n"
                    f"Photo count: {len(manifest.get('photos', []))}\n",
                )
                for record in manifest.get("photos", []):
                    if not isinstance(record, dict):
                        continue
                    filename = Path(str(record.get("filename") or "")).name
                    if not filename:
                        continue
                    photo_path = self.photos_directory / filename
                    if photo_path.is_file():
                        archive.write(photo_path, arcname=f"photos/{filename}")
        except OSError as error:
            raise PhotoStorageError("ARCOBALENO could not create the export archive.") from error

        LOGGER.info("Created event export %s", zip_path.name)
        return zip_path

    def photo_file(self, event_id: str, photo_id: str) -> tuple[dict[str, Any], Path]:
        """Resolve one current-event photo by its opaque manifest identifier."""

        clean_id = str(photo_id or "").strip()
        if not _PHOTO_ID_PATTERN.fullmatch(clean_id):
            raise PhotoNotFoundError("The photo identifier is invalid.")

        with self._lock:
            manifest = self._load_manifest(event_id)
            for record in manifest["photos"]:
                if not isinstance(record, dict) or record.get("id") != clean_id:
                    continue

                filename = Path(str(record.get("filename") or "")).name
                if not filename:
                    raise PhotoNotFoundError("The photo file name is missing.")
                path = self.photos_directory / filename
                if not path.is_file():
                    raise PhotoNotFoundError("The photo file is missing.")
                return self._normalize_record(record), path

        raise PhotoNotFoundError("The photo does not exist.")

    def thumbnail_file(self, event_id: str, photo_id: str) -> tuple[dict[str, Any], Path]:
        record, photo_path = self.photo_file(event_id, photo_id)
        thumbnail = self.thumbnails_directory / self._thumbnail_filename(photo_id)
        if not thumbnail.is_file():
            self._make_thumbnail(photo_path, photo_id)
        if not thumbnail.is_file():
            raise PhotoNotFoundError("The photo thumbnail is missing.")
        return record, thumbnail
