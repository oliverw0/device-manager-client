import os
import time

from ..win import IS_WINDOWS, powershell_json

NOLOGIN_SHELLS = ("nologin", "false", "sync", "shutdown", "halt")

# Built-in Windows accounts nobody logs in as, even when enabled.
WINDOWS_SKIP = {"defaultaccount", "wdagutilityaccount", "guest", "defaultuser0"}

# Enabled local accounts + members of the local Administrators group (by SID,
# so it works on non-English Windows). ADSI rather than Get-LocalGroupMember,
# which throws on orphaned/Azure AD members.
_WINDOWS_ACCOUNTS_PS = r"""
$users = @(Get-CimInstance Win32_UserAccount -Filter "LocalAccount=True AND Disabled=False" | ForEach-Object { $_.Name })
$admins = @()
try {
  $g = (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544').Translate([Security.Principal.NTAccount]).Value.Split([char]92)[-1]
  $admins = @(([ADSI]"WinNT://$env:COMPUTERNAME/$g,group").psbase.Invoke('Members') |
    ForEach-Object { $_.GetType().InvokeMember('Name', 'GetProperty', $null, $_, $null) })
} catch {}
@{ users = $users; admins = $admins } | ConvertTo-Json -Compress
"""
_WIN_CACHE: dict = {"at": 0.0, "value": None}
_WIN_TTL = 600  # accounts rarely change; don't spawn PowerShell every report


def _windows_accounts() -> tuple[list[str], list[str]]:
    """(enabled login users, the subset that are local admins), cached."""
    now = time.time()
    if _WIN_CACHE["value"] is None or now - _WIN_CACHE["at"] > _WIN_TTL:
        data = powershell_json(_WINDOWS_ACCOUNTS_PS) or {}
        users = [u for u in (data.get("users") or []) if u and u.lower() not in WINDOWS_SKIP]
        admin_names = {a.lower() for a in (data.get("admins") or []) if a}
        if users or _WIN_CACHE["value"] is None:
            _WIN_CACHE.update(at=now, value=(users, [u for u in users if u.lower() in admin_names]))
    return _WIN_CACHE["value"]


def windows_admin_user() -> str | None:
    """Account the host uses for updates on Windows (where there's no root).
    SSH_USER (set by install.ps1 when it authorized the host's key) wins, else
    the first enabled local administrator."""
    if not IS_WINDOWS:
        return None
    configured = os.environ.get("SSH_USER", "").strip()
    if configured:
        return configured
    _, admins = _windows_accounts()
    return sorted(admins, key=str.lower)[0] if admins else None


def _windows_users() -> list[str]:
    """The account install.ps1 authorized (SSH_USER) first - the terminal
    dropdown preselects the first entry - then other admins, then everyone
    else. SSH_EXTRA_USERS (comma separated, e.g. domain accounts like
    "CORP\\alice") go last, so the host will accept them too."""
    users, admins = _windows_accounts()
    admin_set = set(admins)
    ordered = sorted(admins, key=str.lower) + sorted((u for u in users if u not in admin_set), key=str.lower)
    configured = os.environ.get("SSH_USER", "").strip()
    if configured:
        ordered = [configured] + [u for u in ordered if u.lower() != configured.lower()]
    for name in (e.strip() for e in os.environ.get("SSH_EXTRA_USERS", "").split(",")):
        if name and name.lower() not in {u.lower() for u in ordered}:
            ordered.append(name)
    return ordered


def _keep(uid: int, shell: str) -> bool:
    """Root plus regular accounts (uid >= 1000) that have a real login shell."""
    if uid != 0 and uid < 1000:
        return False
    name = shell.rsplit("/", 1)[-1]
    return bool(name) and name not in NOLOGIN_SHELLS


def _from_passwd_file(path: str) -> list[str] | None:
    """Parse an /etc/passwd file (name:x:uid:gid:gecos:home:shell). Returns None
    if it can't be read, so the caller can fall back."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None
    users = set()
    for line in lines:
        parts = line.rstrip("\n").split(":")
        if len(parts) < 7:
            continue
        try:
            uid = int(parts[2])
        except ValueError:
            continue
        if _keep(uid, parts[6]):
            users.add(parts[0])
    return sorted(users)


def collect() -> list[str]:
    """Login-capable users on this machine, for the terminal user dropdown.

    In a container `pwd`/`/etc/passwd` are the CONTAINER's, so prefer the host's
    passwd mounted at /host (see docker-compose.yml) — otherwise the dropdown
    only ever shows `root`. Native installs fall through to `pwd`.
    Windows lists its enabled local accounts instead.
    Best-effort: returns [] if nothing can be read.
    """
    if IS_WINDOWS:
        return _windows_users()

    host_users = _from_passwd_file("/host/etc/passwd")
    if host_users is not None:
        return host_users

    try:
        import pwd
    except ImportError:
        return []
    try:
        entries = pwd.getpwall()
    except Exception:
        return []
    return sorted({e.pw_name for e in entries if _keep(e.pw_uid, e.pw_shell or "")})


if __name__ == "__main__":  # ponytail: filter self-check
    assert _keep(0, "/bin/bash")          # root
    assert _keep(1000, "/bin/bash")       # regular login user
    assert not _keep(1000, "/usr/sbin/nologin")
    assert not _keep(33, "/bin/bash")     # system uid < 1000
    assert not _keep(1000, "")            # no shell
    print("ok")
