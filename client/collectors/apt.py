import os
import subprocess
import time
from pathlib import Path

# Where apt keeps its state. Native installs read the real thing at the default.
# Docker clients see the *container's* apt (useless), so to monitor the host they
# mount its /var/lib/apt read-only and set APT_STATE_DIR=/host/var/lib/apt.
APT_STATE_DIR = Path(os.environ.get("APT_STATE_DIR", "/var/lib/apt"))
DEFAULT_STATE_DIR = Path("/var/lib/apt")


def _last_refresh_mtime() -> float | None:
    """Newest mtime of apt's package lists — i.e. when `apt update` last ran.
    Prefer the periodic update-success stamp; fall back to the actual list files.
    Ignore lock/partial (a running container touches the lock, which would look
    like a fresh update and mask a genuinely stale cache)."""
    stamp = APT_STATE_DIR / "periodic" / "update-success-stamp"
    if stamp.exists():
        return stamp.stat().st_mtime

    lists = APT_STATE_DIR / "lists"
    if lists.is_dir():
        mtimes = [
            p.stat().st_mtime
            for p in lists.iterdir()
            if p.is_file() and p.name not in ("lock",)
        ]
        if mtimes:
            return max(mtimes)
    return None


def _upgradable_count() -> int | None:
    """Pending upgrades from the LOCAL cache (no network). `apt-get -s upgrade`
    is present on every Debian/Ubuntu box. Only trustworthy against the real
    host apt, so skip it when APT_STATE_DIR points elsewhere (Docker)."""
    if APT_STATE_DIR != DEFAULT_STATE_DIR:
        return None
    try:
        result = subprocess.run(
            ["apt-get", "-s", "-o", "Debug::NoLocking=true", "upgrade"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # apt prints one "Inst <pkg> ..." line per package it would upgrade.
    return sum(1 for line in result.stdout.splitlines() if line.startswith("Inst "))


def collect() -> dict:
    try:
        refreshed = _last_refresh_mtime()
        upgradable = _upgradable_count()
    except Exception:
        return {"available": False}

    if refreshed is None and upgradable is None:
        # No apt on this box (or Docker without the host mount) — report nothing
        # so the host never flags it. Mirrors tailscale's "not_installed".
        return {"available": False}

    return {
        "available": True,
        "last_update_age_seconds": max(0.0, time.time() - refreshed) if refreshed else None,
        "upgradable": upgradable,
    }
