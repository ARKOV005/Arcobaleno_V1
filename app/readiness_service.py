from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from app.server_state_service import ServerStateService
from app.storage_safety_service import StorageSafetyService


class ReadinessService:
    """Runs non-destructive checks before and during an event."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        storage_safety_service: StorageSafetyService,
        server_state_service: ServerStateService,
    ):
        self.runtime_root = Path(runtime_root)
        self.current_root = self.runtime_root / "events" / "current"
        self.storage_safety_service = storage_safety_service
        self.server_state_service = server_state_service

    @staticmethod
    def _check(key: str, label: str, status: str, detail: str) -> dict[str, str]:
        return {"key": key, "label": label, "status": status, "detail": detail}

    @staticmethod
    def _writable(directory: Path) -> bool:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".arcobaleno-write-test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            probe.unlink(missing_ok=True)
            return False

    def run(
        self,
        *,
        active_event: Mapping[str, Any] | None,
        network_ip: str,
        preferred_ip: str,
        network_candidates: list[str],
        network_ready: bool,
        access_settings: Mapping[str, Any] | None,
        access_ready: bool,
        control: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        checks: list[dict[str, str]] = []

        if active_event:
            checks.append(self._check("active-event", "Active event", "pass", str(active_event.get("title") or "Event workspace ready.")))
        else:
            checks.append(self._check("active-event", "Active event", "fail", "Create an event before guests arrive."))

        event_record = self.current_root / "event.json"
        if active_event and event_record.is_file():
            try:
                payload = json.loads(event_record.read_text(encoding="utf-8"))
                valid = isinstance(payload, dict) and str(payload.get("id") or "") == str(active_event.get("id") or "")
            except (OSError, json.JSONDecodeError):
                valid = False
            checks.append(self._check("event-record", "Event record", "pass" if valid else "fail", "Event data is readable." if valid else "The active event record is missing or unreadable."))
        else:
            checks.append(self._check("event-record", "Event record", "fail" if active_event else "warn", "No active event record is available."))

        if preferred_ip and not network_ready:
            checks.append(self._check("network-ip", "Reserved event server", "fail", f"The reserved address {preferred_ip} is not assigned to this laptop. ARCOBALENO is refusing to advertise another adapter in guest QR codes."))
        elif network_ip in {"127.0.0.1", "0.0.0.0", "localhost"}:
            checks.append(self._check("network-ip", "Event network", "fail", "No usable event-network address was detected."))
        else:
            if preferred_ip:
                candidate_note = f"Reserved event server {preferred_ip} is assigned and verified."
            else:
                candidate_note = f"Automatically selected {network_ip}."
                if len(network_candidates) > 1:
                    candidate_note += f" {len(network_candidates)} local addresses were found. Configure a reserved event IP for production use."
            checks.append(self._check("network-ip", "Event network", "pass", candidate_note))

        if active_event and access_ready:
            ssid = str(((access_settings or {}).get("wifi") or {}).get("ssid") or "")
            checks.append(self._check("qr-access", "QR and Wi-Fi setup", "pass", f"Guest access is configured for {ssid}."))
        elif active_event:
            checks.append(self._check("qr-access", "QR and Wi-Fi setup", "fail", "Save the exact Omada Wi-Fi details before printing QR cards."))
        else:
            checks.append(self._check("qr-access", "QR and Wi-Fi setup", "warn", "Create an event before configuring access."))

        storage = self.storage_safety_service.status()
        free_gb = storage["free_bytes"] / (1024 ** 3)
        if storage["level"] == "critical":
            storage_status = "fail"
            storage_detail = f"Only {free_gb:.2f} GB remains. New uploads are blocked until space is freed."
        elif storage["level"] == "warning":
            storage_status = "warn"
            storage_detail = f"{free_gb:.2f} GB remains. Free more space before a large event."
        else:
            storage_status = "pass"
            storage_detail = f"{free_gb:.2f} GB of free storage is available."
        checks.append(self._check("storage", "Live storage safety", storage_status, storage_detail))

        server_state = self.server_state_service.status()
        if server_state.get("previous_unclean_shutdown"):
            checks.append(self._check(
                "server-shutdown",
                "Previous server shutdown",
                "warn",
                "The previous server session did not close cleanly. Confirm the active event and latest photos before guests arrive.",
            ))
        else:
            checks.append(self._check(
                "server-shutdown",
                "Server session state",
                "pass",
                "No interrupted previous server session was detected.",
            ))

        writable_targets = {
            "photo-storage": (self.current_root / "photos", "Photo storage"),
            "exports": (self.runtime_root / "exports", "Export folder"),
            "backups": (self.runtime_root / "backups", "Backup folder"),
            "logs": (self.runtime_root / "logs", "Log folder"),
        }
        for key, (path, label) in writable_targets.items():
            writable = self._writable(path)
            checks.append(self._check(key, label, "pass" if writable else "fail", "Writable and ready." if writable else f"Windows blocked writing to {path}."))

        uploads_open = bool((control or {}).get("uploads_open", True))
        checks.append(self._check("uploads", "Guest uploads", "pass" if uploads_open else "warn", "Uploads are open to guests." if uploads_open else "Uploads are paused by the operator."))

        fails = sum(1 for item in checks if item["status"] == "fail")
        warnings = sum(1 for item in checks if item["status"] == "warn")
        passes = sum(1 for item in checks if item["status"] == "pass")
        if fails:
            overall = "not-ready"
        elif warnings:
            overall = "ready-with-warnings"
        else:
            overall = "ready"

        return {
            "overall": overall,
            "passes": passes,
            "warnings": warnings,
            "fails": fails,
            "checks": checks,
        }
