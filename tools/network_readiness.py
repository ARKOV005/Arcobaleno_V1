"""ARCOBALENO Preview 18: read-only LAN readiness diagnostic (standard library)."""
from __future__ import annotations
import argparse
import ipaddress
import json
import socket
from datetime import datetime, timezone

def ipv4_addresses():
    candidates = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # Route selection only; sends no traffic.
            candidates.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.add(entry[4][0])
    except OSError:
        pass
    return sorted(x for x in candidates if not ipaddress.ip_address(x).is_loopback)

def evaluate(addresses, port, expected_ip=None):
    private = [x for x in addresses if ipaddress.ip_address(x).is_private and not ipaddress.ip_address(x).is_link_local]
    selected = expected_ip if expected_ip in addresses else (private[0] if private else None)
    checks = {
        "local_ipv4_available": bool(addresses),
        "private_lan_ipv4_available": bool(private),
        "expected_ip_matches": expected_ip is None or expected_ip in addresses,
        "valid_port": 1 <= port <= 65535,
    }
    return {"ready_for_qr": all(checks.values()) and selected is not None,
            "selected_ipv4": selected,
            "qr_base_url": f"http://{selected}:{port}" if selected and checks["valid_port"] else None,
            "checks": checks, "available_ipv4": addresses}

def main():
    ap = argparse.ArgumentParser(description="ARCOBALENO non-invasive local IPv4 preflight")
    ap.add_argument("--port", type=int, default=5000, help="Confirm against current server config")
    ap.add_argument("--expected-ip", help="Optional reserved/static IPv4 to verify")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.expected_ip:
        try:
            ipaddress.IPv4Address(args.expected_ip)
        except ipaddress.AddressValueError:
            ap.error("--expected-ip must be a valid IPv4")
    result = evaluate(ipv4_addresses(), args.port, args.expected_ip)
    result["checked_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["warning"] = "IP-only preflight: does not verify server bind, gateway, Wi-Fi isolation, reachability or QR scan."
    print(json.dumps(result, indent=2) if args.json else "\n".join(f"{k}: {v}" for k,v in result.items()))
    raise SystemExit(0 if result["ready_for_qr"] else 2)

if __name__ == "__main__":
    main()
