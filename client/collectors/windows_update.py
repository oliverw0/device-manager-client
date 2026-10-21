"""Windows Update status, reported in the same shape as the apt collector so the
host's staleness badge and alert work unchanged.

The pending count comes from an *offline* search of the local update cache
(like `apt-get -s upgrade` reading apt's cache): no network, but it can still
take several seconds, so it refreshes on a background thread and collect()
returns the last result immediately.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from ..win import IS_WINDOWS, powershell_json

REFRESH_SECONDS = 3600

_PS = r"""
function Iso($d) { if ($d -and $d.Year -gt 2000) { $d.ToString('yyyy-MM-ddTHH:mm:ss') } else { $null } }
$r = (New-Object -ComObject Microsoft.Update.AutoUpdate).Results
$pending = $null
try {
  $searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
  $searcher.Online = $false
  $pending = $searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Software'").Updates.Count
} catch {}
@{
  searched  = Iso $r.LastSearchSuccessDate
  installed = Iso $r.LastInstallationSuccessDate
  pending   = $pending
  reboot    = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
} | ConvertTo-Json -Compress
"""

_lock = threading.Lock()
_state: dict = {"raw": None, "at": 0.0, "running": False}


def _age(iso: str | None) -> float | None:
    # The WU API documents these as UTC; a few hours' error is irrelevant for a
    # days-scale staleness check either way.
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, time.time() - dt.timestamp())


def summarize(raw: dict | None) -> dict:
    """Map the PowerShell result onto the apt report shape. "Age" is how stale
    the box is: with updates waiting, time since the last successful install;
    with nothing pending, time since Windows last successfully checked (so a
    machine whose Windows Update is broken still ages and gets flagged)."""
    if not raw:
        return {"available": False}
    pending = raw.get("pending")
    if pending:
        age = _age(raw.get("installed")) or _age(raw.get("searched"))
    else:
        age = _age(raw.get("searched"))
    if age is None and pending is None:
        return {"available": False}
    return {
        "available": True,
        "source": "windows_update",
        "last_update_age_seconds": age,
        "upgradable": pending,
        "reboot_required": bool(raw.get("reboot")),
    }


def _refresh() -> None:
    try:
        raw = powershell_json(_PS, timeout=300)
        with _lock:
            if raw is not None or _state["raw"] is None:
                _state["raw"] = raw
            _state["at"] = time.time()
    finally:
        with _lock:
            _state["running"] = False


def collect() -> dict:
    if not IS_WINDOWS:
        return {"available": False}
    with _lock:
        stale = time.time() - _state["at"] > REFRESH_SECONDS
        if stale and not _state["running"]:
            _state["running"] = True
            threading.Thread(target=_refresh, name="windows-update", daemon=True).start()
        raw = _state["raw"]
    return summarize(raw)


if __name__ == "__main__":  # ponytail: summarize() self-check
    def ago(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    assert summarize(None) == {"available": False}
    assert summarize({"searched": None, "installed": None, "pending": None}) == {"available": False}
    up = summarize({"searched": ago(1), "installed": ago(60), "pending": 0, "reboot": False})
    assert up["available"] and up["upgradable"] == 0 and round(up["last_update_age_seconds"] / 86400) == 1
    behind = summarize({"searched": ago(1), "installed": ago(45), "pending": 7, "reboot": True})
    assert behind["upgradable"] == 7 and round(behind["last_update_age_seconds"] / 86400) == 45
    assert behind["reboot_required"] is True
    print("ok")
