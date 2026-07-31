from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any


class ServerStateService:
    """Tracks clean and interrupted server shutdowns using an atomic state file."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.state_path = self.runtime_root / "logs" / "server-state.json"

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _read(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{secrets.token_hex(4)}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def begin(
        self,
        version: str,
        active_event_id: str | None = None,
    ) -> dict[str, Any]:
        previous = self._read()
        previous_unclean = previous.get("status") in {"running", "stopping"}
        clean_active_event_id = str(active_event_id or "").strip()
        payload = {
            "status": "running",
            "version": str(version),
            "pid": os.getpid(),
            "started_at": self._now(),
            "previous_unclean_shutdown": bool(previous_unclean),
            "previous_started_at": str(previous.get("started_at") or ""),
            # Recovery belongs only to the event that already existed when this
            # server session started. An event created later in the same session
            # must never be mislabeled as recovered.
            "recovered_event_id": clean_active_event_id if previous_unclean else "",
        }
        self._write(payload)
        return payload

    def mark_stopping(self, reason: str = "operator") -> dict[str, Any]:
        payload = self._read()
        payload.update(
            {
                "status": "stopping",
                "shutdown_requested_at": self._now(),
                "shutdown_reason": str(reason or "operator").strip() or "operator",
                "clean_shutdown": False,
            }
        )
        self._write(payload)
        return payload

    def cancel_stopping(self) -> dict[str, Any]:
        payload = self._read()
        if payload.get("status") == "stopping":
            payload.update(
                {
                    "status": "running",
                    "shutdown_cancelled_at": self._now(),
                    "clean_shutdown": False,
                }
            )
        self._write(payload)
        return payload

    def finish(self) -> dict[str, Any]:
        payload = self._read()
        payload.update(
            {
                "status": "stopped",
                "stopped_at": self._now(),
                "clean_shutdown": True,
            }
        )
        self._write(payload)
        return payload

    def acknowledge_recovery(self, event_id: str) -> dict[str, Any]:
        payload = self._read()
        payload.update(
            {
                "recovery_acknowledged_at": self._now(),
                "recovery_acknowledged_event_id": str(event_id or "").strip(),
            }
        )
        self._write(payload)
        return payload

    def recovery_attention(self, event_id: str | None) -> dict[str, Any]:
        payload = self._read()
        clean_event_id = str(event_id or "").strip()
        previous_unclean = bool(payload.get("previous_unclean_shutdown"))
        recovered_event_id = str(payload.get("recovered_event_id") or "").strip()
        acknowledged_event_id = str(payload.get("recovery_acknowledged_event_id") or "").strip()
        acknowledged = bool(payload.get("recovery_acknowledged_at")) and acknowledged_event_id == clean_event_id
        return {
            "required": bool(
                clean_event_id
                and previous_unclean
                and recovered_event_id == clean_event_id
                and not acknowledged
            ),
            "previous_unclean_shutdown": previous_unclean,
            "previous_started_at": str(payload.get("previous_started_at") or ""),
            "recovered_event_id": recovered_event_id,
            "acknowledged": acknowledged,
        }

    def status(self) -> dict[str, Any]:
        return self._read()
