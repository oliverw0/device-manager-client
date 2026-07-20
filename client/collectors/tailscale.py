import json
import subprocess


def collect() -> dict:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return {"connected": False, "backend_state": "not_installed", "ips": []}
    except subprocess.TimeoutExpired:
        return {"connected": False, "backend_state": "timeout", "ips": []}

    if result.returncode != 0:
        return {"connected": False, "backend_state": "error", "ips": []}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"connected": False, "backend_state": "unparseable", "ips": []}

    backend_state = data.get("BackendState", "unknown")
    self_info = data.get("Self", {}) or {}
    tailnet = (data.get("CurrentTailnet") or {}).get("Name")

    exit_node = None
    for peer in (data.get("Peer") or {}).values():
        if peer.get("ExitNode"):
            exit_node = peer.get("HostName") or peer.get("DNSName")
            break

    return {
        "connected": backend_state == "Running",
        "backend_state": backend_state,
        "ips": self_info.get("TailscaleIPs", []),
        "tailnet": tailnet,
        "exit_node": exit_node,
    }
