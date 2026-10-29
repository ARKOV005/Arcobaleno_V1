"""Server-authoritative slideshow photo selection shared by all display browsers."""
from __future__ import annotations
import random
import threading
import time

class DisplaySyncService:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions = {}

    def snapshot(self, event_id, approved_ids, duration, command_sequence=0, command=""):
        now = time.monotonic()
        duration = max(2.0, float(duration))
        approved = tuple(sorted(set(approved_ids)))
        with self._lock:
            state = self._sessions.get(event_id)
            if state is None:
                state = {"order": [], "index": 0, "changed": now,
                         "sequence": int(command_sequence), "revision": 0}
                self._sessions = {event_id: state}
            previous = set(state["order"])
            current = [x for x in state["order"] if x in approved]
            additions = [x for x in approved if x not in previous]
            # A shared shuffle happens only on the server, never per browser.
            random.shuffle(additions)
            current.extend(additions)
            if current != state["order"]:
                old_id = (state["order"][state["index"]]
                          if state["order"] and state["index"] < len(state["order"]) else None)
                state["order"] = current
                state["index"] = current.index(old_id) if old_id in current else 0
                state["changed"] = now
                state["revision"] += 1
            seq = int(command_sequence)
            if seq > state["sequence"]:
                if current:
                    if command == "next":
                        state["index"] = (state["index"] + 1) % len(current)
                    elif command == "previous":
                        state["index"] = (state["index"] - 1) % len(current)
                    elif command == "restart":
                        state["index"] = 0
                    state["changed"] = now
                    state["revision"] += 1
                state["sequence"] = seq
            if len(current) > 1:
                elapsed = max(0, now - state["changed"])
                steps = int(elapsed // duration)
                if steps:
                    state["index"] = (state["index"] + steps) % len(current)
                    state["changed"] += steps * duration
                    state["revision"] += steps
            return {"photo_id": current[state["index"]] if current else None,
                    "revision": state["revision"], "mode": "server"}
