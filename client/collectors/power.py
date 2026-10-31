"""Power draw from the local power exporter, if this machine runs one.

The exporter is power/pve_power.py (installed as the pve-power service when
power tracking is enabled for the device on the dashboard). It serves wall
watts, cumulative kWh, a per-component breakdown and each Proxmox guest's share
on :9105/json. POWER_URL overrides the address ("off" disables); machines
without an exporter just report nothing.
"""
import os
import socket
import struct

import httpx

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


def collect() -> dict | None:
    for url in _urls():
        if url.lower() in ("off", "none"):
            return None
        try:
            data = httpx.get(url, timeout=2).json()
            wall = float(data["wall_w"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            continue
        return {
            "wall_w": wall,
            "energy_kwh": data.get("energy_kwh"),
            "dc_load_w": data.get("dc_load_w"),
            "psu_efficiency": data.get("psu_efficiency"),
            "components": {k: v for k, v in (data.get("components") or {}).items() if isinstance(v, (int, float))},
            "guests": [
                {"name": g.get("name"), "type": g.get("type"), "vmid": g.get("vmid"), "cpu_w": g.get("cpu_w")}
                for g in (data.get("guests") or []) if isinstance(g, dict)
            ][:20],
        }
    return None
