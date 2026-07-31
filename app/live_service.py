from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Iterator


class LiveEventService:
    """Small in-memory event bus for operator pages and display presence.

    Durable state remains in the event JSON files. This service only carries
    lightweight notifications so browsers can update without repeatedly
    reloading complete pages.
    """

    def __init__(self, history_size: int = 600):
        self._lock = threading.RLock()
        self._sequence = 0
        self._history: deque[dict[str, Any]] = deque(maxlen=max(100, history_size))
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._displays: dict[tuple[str, str], dict[str, Any]] = {}
        self._displayed_photo_ids: dict[str, set[str]] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")

    def publish(self, event_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        clean_event_id = str(event_id or "").strip()
        with self._lock:
            self._sequence += 1
            event = {
                "sequence": self._sequence,
                "event_id": clean_event_id,
                "type": str(event_type or "update"),
                "created_at": self._now(),
                "payload": dict(payload or {}),
            }
            self._history.append(event)
            for subscriber in tuple(self._subscribers):
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        subscriber.put_nowait(event)
                    except queue.Full:
                        pass
            return dict(event)

    def latest_sequence(self, event_id: str) -> int:
        clean_event_id = str(event_id or "").strip()
        with self._lock:
            for item in reversed(self._history):
                if str(item.get("event_id") or "") == clean_event_id:
                    return int(item.get("sequence") or 0)
        return 0

    def stream(self, event_id: str, since: int = 0) -> Iterator[str]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=120)
        clean_event_id = str(event_id or "").strip()
        with self._lock:
            replay = [
                dict(item)
                for item in self._history
                if int(item.get("sequence") or 0) > max(0, int(since))
                and str(item.get("event_id") or "") == clean_event_id
            ]
            self._subscribers.add(subscriber)

        try:
            for event in replay:
                yield self._format_sse(event)
            ready_sequence = int(replay[-1].get("sequence") or since) if replay else max(0, int(since))
            ready = {
                "sequence": ready_sequence,
                "event_id": clean_event_id,
                "type": "stream.ready",
                "created_at": self._now(),
                "payload": {"connected": True},
            }
            yield self._format_sse(ready)
            while True:
                try:
                    event = subscriber.get(timeout=15)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if str(event.get("event_id") or "") != clean_event_id:
                    continue
                yield self._format_sse(event)
        finally:
            with self._lock:
                self._subscribers.discard(subscriber)

    @staticmethod
    def _format_sse(event: dict[str, Any]) -> str:
        sequence = int(event.get("sequence") or 0)
        event_type = str(event.get("type") or "update").replace("\n", " ")
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"id: {sequence}\nevent: {event_type}\ndata: {payload}\n\n"

    def register_display(
        self,
        *,
        event_id: str,
        display_id: str,
        mode: str,
        current_photo_id: str = "",
        state: str = "",
        label: str = "",
    ) -> bool:
        now = time.monotonic()
        key = (str(event_id or ""), str(display_id or ""))
        if not key[0] or len(key[1]) < 8:
            return False
        clean_mode = mode if mode in {"wired", "network", "preview"} else "network"
        with self._lock:
            previous = self._displays.get(key)
            current = {
                "event_id": key[0],
                "display_id": key[1],
                "mode": clean_mode,
                "label": str(label or "")[:80],
                "current_photo_id": str(current_photo_id or "")[:64],
                "state": str(state or "")[:40],
                "last_seen_monotonic": now,
                "last_seen": self._now(),
            }
            self._displays[key] = current
            photo_id = current["current_photo_id"]
            if photo_id:
                self._displayed_photo_ids.setdefault(key[0], set()).add(photo_id)
            if previous is None:
                return True
            return any(
                previous.get(field) != current.get(field)
                for field in ("mode", "label", "current_photo_id", "state")
            )

    def display_status(self, event_id: str, stale_after_seconds: float = 18.0) -> dict[str, Any]:
        clean_event_id = str(event_id or "")
        now = time.monotonic()
        with self._lock:
            stale_keys = [
                key
                for key, value in self._displays.items()
                if now - float(value.get("last_seen_monotonic") or 0) > stale_after_seconds
            ]
            for key in stale_keys:
                self._displays.pop(key, None)

            sessions = [
                {
                    key: value.get(key)
                    for key in ("display_id", "mode", "label", "current_photo_id", "state", "last_seen")
                }
                for (stored_event_id, _), value in self._displays.items()
                if stored_event_id == clean_event_id
            ]
            sessions.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
            counts = {"wired": 0, "network": 0, "preview": 0}
            for item in sessions:
                mode = str(item.get("mode") or "network")
                counts[mode] = counts.get(mode, 0) + 1
            return {
                "total": len(sessions),
                "wired": counts.get("wired", 0),
                "network": counts.get("network", 0),
                "preview": counts.get("preview", 0),
                "displayed_unique": len(self._displayed_photo_ids.get(clean_event_id, set())),
                "sessions": sessions,
            }
