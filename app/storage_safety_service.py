from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


class StorageSafetyService:
    """Reports live disk safety and blocks uploads before storage becomes critical."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        warning_free_bytes: int,
        critical_free_bytes: int,
        upload_reserve_bytes: int,
    ):
        self.runtime_root = Path(runtime_root)
        self.warning_free_bytes = max(0, int(warning_free_bytes))
        self.critical_free_bytes = max(0, int(critical_free_bytes))
        self.upload_reserve_bytes = max(0, int(upload_reserve_bytes))

    def status(self, incoming_bytes: int = 0) -> dict[str, Any]:
        usage = shutil.disk_usage(self.runtime_root)
        free_bytes = int(usage.free)
        incoming_bytes = max(0, int(incoming_bytes))
        required_free_bytes = max(
            self.critical_free_bytes,
            self.upload_reserve_bytes + incoming_bytes,
        )
        accepting_uploads = free_bytes >= required_free_bytes

        if not accepting_uploads:
            level = "critical"
            message = (
                "The event server is low on storage. Please tell the operator before "
                "taking another photo."
            )
        elif free_bytes < self.warning_free_bytes:
            level = "warning"
            message = "Server storage is getting low. The operator should free space soon."
        else:
            level = "healthy"
            message = "Server storage is healthy."

        return {
            "level": level,
            "accepting_uploads": accepting_uploads,
            "message": message,
            "free_bytes": free_bytes,
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "required_free_bytes": required_free_bytes,
            "warning_free_bytes": self.warning_free_bytes,
            "critical_free_bytes": self.critical_free_bytes,
            "upload_reserve_bytes": self.upload_reserve_bytes,
        }
