NOLOGIN_SHELLS = ("nologin", "false", "sync", "shutdown", "halt")


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
    Best-effort: returns [] on non-Unix or if nothing can be read.
    """
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
