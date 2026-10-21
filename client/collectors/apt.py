import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("devicemanager.client")

# Where apt keeps its state. Native installs read the real thing at the default.
# Docker clients see the *container's* apt (useless), so to monitor the host they
# mount its /var/lib/apt read-only and set APT_STATE_DIR=/host/var/lib/apt.
APT_STATE_DIR = Path(os.environ.get("APT_STATE_DIR", "/var/lib/apt"))
DEFAULT_STATE_DIR = Path("/var/lib/apt")
# Inside the Docker client, /var/lib/apt is the image's, not the machine's.
IN_CONTAINER = Path("/.dockerenv").exists()

# A native root client keeps the package lists fresh itself, so the upgradable
# count is real and the lists never go stale: `apt-get update` when they're
# older than a day, tried at most once an hour, in the background so a slow
# mirror never holds up a report.
REFRESH_AFTER = 24 * 3600
RETRY_AFTER = 3600
_refresh = {"tried": 0.0, "running": False}


def _manages_apt() -> bool:
    """True when this client is looking at the machine's own apt."""
    return APT_STATE_DIR == DEFAULT_STATE_DIR and not IN_CONTAINER


def _last_refresh_mtime() -> float | None:
    """Newest mtime of apt's package lists — i.e. when `apt update` last ran.
    Prefer the periodic update-success stamp; fall back to the actual list files.
    Ignore lock/partial (a running container touches the lock, which would look
    like a fresh update and mask a genuinely stale cache)."""
    if APT_STATE_DIR == DEFAULT_STATE_DIR and IN_CONTAINER:
        return None  # the image's lists, not the machine's
    stamp = APT_STATE_DIR / "periodic" / "update-success-stamp"
    lists = APT_STATE_DIR / "lists"
    mtimes = [stamp.stat().st_mtime] if stamp.exists() else []
    if lists.is_dir():
        mtimes += [p.stat().st_mtime for p in lists.iterdir() if p.is_file() and p.name != "lock"]
    return max(mtimes) if mtimes else None


def _run_update():
    try:
        result = subprocess.run(["apt-get", "update", "-qq"], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.warning("apt-get update failed (exit %s): %s", result.returncode, result.stderr.strip()[-300:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("apt-get update failed: %s", exc)
    finally:
        _refresh["running"] = False


def _refresh_if_stale(refreshed: float | None, now: float | None = None) -> bool:
    """Start a background `apt-get update` if the lists are old. Needs root."""
    now = now or time.time()
    if not _manages_apt() or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return False
    if _refresh["running"] or now - _refresh["tried"] < RETRY_AFTER:
        return False
    if refreshed is not None and now - refreshed < REFRESH_AFTER:
        return False
    _refresh.update(tried=now, running=True)
    threading.Thread(target=_run_update, name="apt-update", daemon=True).start()
    return True


def _upgradable_count() -> int | None:
    """Pending upgrades from the LOCAL cache (no network). `apt-get -s upgrade`
    is present on every Debian/Ubuntu box. Only trustworthy against the real
    machine's apt, so skip it in Docker or when APT_STATE_DIR points elsewhere."""
    if not _manages_apt():
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
        _refresh_if_stale(refreshed)
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


if __name__ == "__main__":  # ponytail: self-check of the refresh decision (no apt needed)
    import unittest.mock as m

    with m.patch.object(os, "geteuid", lambda: 0, create=True), \
         m.patch(__name__ + ".IN_CONTAINER", False), \
         m.patch(__name__ + ".APT_STATE_DIR", DEFAULT_STATE_DIR), \
         m.patch("threading.Thread") as thread:
        now = 1_000_000.0
        assert not _refresh_if_stale(now - 3600, now)                  # fresh: leave it
        assert _refresh_if_stale(now - 3 * 86400, now)                 # stale: refresh
        _refresh["running"] = False
        assert not _refresh_if_stale(now - 3 * 86400, now + 60)        # tried a minute ago: wait
        assert _refresh_if_stale(None, now + RETRY_AFTER + 1)          # never refreshed, retry due
        assert thread.call_count == 2
    _refresh.update(tried=0.0, running=False)
    with m.patch.object(os, "geteuid", lambda: 0, create=True), m.patch(__name__ + ".IN_CONTAINER", True):
        assert not _refresh_if_stale(None) and _upgradable_count() is None and _last_refresh_mtime() is None  # Docker, no mount
    with m.patch.object(os, "geteuid", lambda: 1000, create=True), \
         m.patch(__name__ + ".IN_CONTAINER", False), m.patch(__name__ + ".APT_STATE_DIR", DEFAULT_STATE_DIR):
        assert not _refresh_if_stale(None)                             # not root: can't
    print("ok")
