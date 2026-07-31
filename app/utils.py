from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

RUNTIME_RELATIVE_DIRECTORIES = (
    Path("events/current"),
    Path("events/archive"),
    Path("exports"),
    Path("logs"),
    Path("backups"),
    Path("fonts"),
)


@dataclass(frozen=True)
class LocalNetworkSelection:
    """The address ARCOBALENO advertises and the addresses Windows currently owns.

    A configured preferred IP is authoritative. When it is temporarily unavailable,
    ARCOBALENO keeps advertising the reserved address instead of silently exposing a
    VPN or virtual-adapter address in guest QR codes.
    """

    selected_ip: str
    preferred_ip: str
    candidates: tuple[str, ...]
    detected_ip: str

    @property
    def preferred_available(self) -> bool:
        return bool(self.preferred_ip and self.preferred_ip in self.candidates)

    @property
    def ready(self) -> bool:
        if self.preferred_ip:
            return self.preferred_available
        return bool(self.detected_ip and not self.detected_ip.startswith("127."))

    @property
    def usable(self) -> bool:
        return self.ready and bool(self.selected_ip and not self.selected_ip.startswith("127."))

    @property
    def using_reserved_ip(self) -> bool:
        return bool(self.preferred_ip)


def ensure_runtime_directories(project_root: Path) -> dict[str, Path]:
    created: dict[str, Path] = {}
    for relative_path in RUNTIME_RELATIVE_DIRECTORIES:
        absolute_path = project_root / relative_path
        absolute_path.mkdir(parents=True, exist_ok=True)
        created[str(relative_path).replace("\\", "/")] = absolute_path
    return created


def _valid_local_ipv4(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if address.version != 4 or address.is_loopback or address.is_unspecified or address.is_link_local:
        return None
    return str(address)


def _windows_ipv4_addresses() -> Iterable[str]:
    """Ask Windows for every assigned IPv4 address without depending on UI language."""

    if os.name != "nt":
        return ()

    command = (
        "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Where-Object { $_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '127.*' } | "
        "Select-Object -ExpandProperty IPAddress"
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())


def _candidate_ipv4_addresses() -> Iterable[str]:
    discovered: list[str] = []

    def add(value: str) -> None:
        address = _valid_local_ipv4(value)
        if address and address not in discovered:
            discovered.append(address)

    for address in _windows_ipv4_addresses():
        add(address)

    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(result[4][0])
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.settimeout(1)
            connection.connect(("1.1.1.1", 80))
            add(connection.getsockname()[0])
    except OSError:
        pass

    return discovered


def discover_local_ipv4_addresses() -> tuple[str, ...]:
    return tuple(_candidate_ipv4_addresses())


def _auto_priority(address: str) -> tuple[int, str]:
    """Prefer common event-LAN ranges over VPN-style 10.x fallbacks."""

    if address.startswith("192.168."):
        return (0, address)
    if address.startswith("10."):
        return (1, address)
    if address.startswith("172."):
        try:
            second_octet = int(address.split(".")[1])
        except (IndexError, ValueError):
            second_octet = -1
        if 16 <= second_octet <= 31:
            return (2, address)
    return (3, address)


def select_local_network(preferred_ip: str = "") -> LocalNetworkSelection:
    candidates = discover_local_ipv4_addresses()
    preferred_ip = preferred_ip.strip()
    detected_ip = sorted(candidates, key=_auto_priority)[0] if candidates else "127.0.0.1"

    # A reservation is a deliberate operator choice. Never replace it in QR links
    # with a VPN, Docker, Hyper-V, or other adapter merely because it is online.
    selected_ip = preferred_ip or detected_ip

    return LocalNetworkSelection(
        selected_ip=selected_ip,
        preferred_ip=preferred_ip,
        candidates=candidates,
        detected_ip=detected_ip,
    )


def detect_local_ip(preferred_ip: str = "") -> str:
    return select_local_network(preferred_ip).selected_ip


def build_http_url(host: str, port: int) -> str:
    default_port = port == 80
    return f"http://{host}" if default_port else f"http://{host}:{port}"
