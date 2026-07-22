import json
import os
import subprocess

import httpx

# tailscaled exposes a LocalAPI over this unix socket that returns the same JSON
# shape as `tailscale status --json`. Talking to it directly means the client
# does NOT need the `tailscale` CLI binary installed - which is exactly the case
# inside the slim Docker image. Mount the host's /var/run/tailscale into the
# container (see client/docker-compose.yml) and this just works.
DEFAULT_SOCKET = "/var/run/tailscale/tailscaled.sock"


def _parse_status(data: dict) -> dict:
    backend_state = data.get("BackendState", "unknown")
    self_info = data.get("Self", {}) or {}
    tailnet = (data.get("CurrentTailnet") or {}).get("Name")

    exit_node = None
    for peer in (data.get("Peer") or {}).values():
        if peer.get("ExitNode"):
            exit_node = peer.get("HostName") or peer.get("DNSName")
            break

    # MagicDNS name for this node, e.g. "plex-server.tnet-name.ts.net."
    # (tailscaled reports it with a trailing dot; strip it for display/copy).
    dns_name = (self_info.get("DNSName") or "").rstrip(".") or None

    return {
        "connected": backend_state == "Running",
        "backend_state": backend_state,
        "ips": self_info.get("TailscaleIPs", []),
        "dns_name": dns_name,
        "tailnet": tailnet,
        "exit_node": exit_node,
    }


def _via_localapi() -> dict | None:
    """Query tailscaled's LocalAPI over its unix socket. Returns parsed status,
    or None if the socket isn't present/reachable (so the caller can fall back)."""
    sock_path = os.environ.get("TAILSCALE_SOCKET", DEFAULT_SOCKET)
    if not os.path.exists(sock_path):
        return None
    try:
        transport = httpx.HTTPTransport(uds=sock_path)
        with httpx.Client(transport=transport, timeout=5) as client:
            # Host is arbitrary but must be set; tailscaled's Go client uses this value.
            resp = client.get("http://local-tailscaled.sock/localapi/v0/status")
        if resp.status_code != 200:
            return None
        return _parse_status(resp.json())
    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        return None


def _via_cli() -> dict | None:
    """Fall back to the `tailscale` CLI (native installs where the binary is on PATH)."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return {"connected": False, "backend_state": "timeout", "ips": []}

    if result.returncode != 0:
        return {"connected": False, "backend_state": "error", "ips": []}

    try:
        return _parse_status(json.loads(result.stdout))
    except json.JSONDecodeError:
        return {"connected": False, "backend_state": "unparseable", "ips": []}


def collect() -> dict:
    # Never let a Tailscale problem crash the client's report cycle: any
    # unexpected error here just means "we couldn't read Tailscale", which
    # is reported as not_installed and surfaced as "No Tailscale client".
    try:
        # Prefer the socket (works in Docker without the CLI); fall back to the CLI.
        result = _via_localapi()
        if result is not None:
            return result

        result = _via_cli()
        if result is not None:
            return result
    except Exception:
        pass

    # Neither the socket nor the CLI is available (or reading them failed).
    return {"connected": False, "backend_state": "not_installed", "ips": []}
