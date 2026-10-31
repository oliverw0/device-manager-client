"""Power draw from the local power exporter, if this machine runs one.

The exporter is power/pve_power.py (installed as the pve-power service when
power tracking is enabled for the device on the dashboard). It serves wall
watts, cumulative kWh, a per-component breakdown and each Proxmox guest's share
on :9105/json. POWER_URL overrides the address ("off" disables); machines
without an exporter just report nothing.
"""
import json
import os
import socket
import struct
import time

import httpx

from ..win import IS_WINDOWS

PORT = 9105


def _docker_gateway() -> str | None:
    """The Docker host as seen from inside a container (the default route's
    gateway): the exporter listens on all interfaces, so it's reachable there."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if parts[1] == "00000000":  # default route
                    return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except (OSError, IndexError, ValueError):
        pass
    return None


def _urls() -> list[str]:
    if os.environ.get("POWER_URL"):
        return [os.environ["POWER_URL"]]
    urls = [f"http://127.0.0.1:{PORT}/json"]
    if os.path.exists("/.dockerenv"):
        gw = _docker_gateway()
        if gw:
            urls.append(f"http://{gw}:{PORT}/json")
    return urls


def _windows_reading() -> dict | None:
    """Windows: power/win_power.ps1 writes its readings to a file every few
    seconds instead of serving HTTP. Ignored once stale (the exporter stopped)."""
    path = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "DeviceManagerClient", "power.json")
    try:
        if time.time() - os.path.getmtime(path) > 60:
            return None
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _readings():
    if IS_WINDOWS and not os.environ.get("POWER_URL"):
        yield _windows_reading()
        return
    for url in _urls():
        if url.lower() in ("off", "none"):
            return
        try:
            yield httpx.get(url, timeout=2).json()
        except (httpx.HTTPError, ValueError):
            continue


def collect() -> dict | None:
    for data in _readings():
        try:
            wall = float(data["wall_w"])
        except (ValueError, KeyError, TypeError):
            continue
        return {
            "wall_w": wall,
            "energy_kwh": data.get("energy_kwh"),
            "dc_load_w": data.get("dc_load_w"),
            "psu_efficiency": data.get("psu_efficiency"),
            "psu": data.get("psu"),                    # Windows: which PSU curve the estimate uses
            "cpu_measured": data.get("cpu_measured"),  # Windows: False if the CPU sensor isn't readable
            "components": {k: v for k, v in (data.get("components") or {}).items() if isinstance(v, (int, float))},
            "guests": [
                {"name": g.get("name"), "type": g.get("type"), "vmid": g.get("vmid"), "cpu_w": g.get("cpu_w")}
                for g in (data.get("guests") or []) if isinstance(g, dict)
            ][:20],
        }
    return None
