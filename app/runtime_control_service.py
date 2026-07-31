from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable

LOGGER = logging.getLogger("arcobaleno.runtime_control")


class RuntimeControlError(RuntimeError):
    """Raised when the running ARCOBALENO process cannot accept a control action."""


class RuntimeControlService:
    """Coordinates process-level controls without exposing them to guest routes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._shutdown_callback: Callable[[], None] | None = None
        self._shutdown_requested = False
        self._shutdown_requested_at = ""

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def bind_shutdown(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("The shutdown callback must be callable.")
        with self._lock:
            self._shutdown_callback = callback

    @property
    def available(self) -> bool:
        with self._lock:
            return self._shutdown_callback is not None

    def request_shutdown(self) -> dict[str, Any]:
        with self._lock:
            if self._shutdown_requested:
                return self.status()
            callback = self._shutdown_callback
            if callback is None:
                raise RuntimeControlError(
                    "Safe shutdown is not available in this ARCOBALENO runtime."
                )
            self._shutdown_requested = True
            self._shutdown_requested_at = self._now()

        try:
            callback()
        except Exception as error:  # noqa: BLE001
            with self._lock:
                self._shutdown_requested = False
                self._shutdown_requested_at = ""
            LOGGER.exception("The safe-shutdown callback failed")
            raise RuntimeControlError("ARCOBALENO could not begin safe shutdown.") from error

        LOGGER.info("Safe shutdown requested from the operator interface.")
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "available": self._shutdown_callback is not None,
                "shutdown_requested": self._shutdown_requested,
                "shutdown_requested_at": self._shutdown_requested_at,
            }
