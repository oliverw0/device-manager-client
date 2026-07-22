NOLOGIN_SHELLS = ("nologin", "false", "sync", "shutdown", "halt")


def collect() -> list[str]:
    """Login-capable users on this machine, for the terminal user dropdown.

    Root plus regular accounts (uid >= 1000) that have a real login shell.
    Best-effort: returns [] on non-Unix or if /etc/passwd can't be read.
    """
    try:
        import pwd
    except ImportError:
        return []

    users = set()
    try:
        entries = pwd.getpwall()
    except Exception:
        return []

    for entry in entries:
        if entry.pw_uid != 0 and entry.pw_uid < 1000:
            continue
        shell = (entry.pw_shell or "").rsplit("/", 1)[-1]
        if shell in NOLOGIN_SHELLS or not shell:
            continue
        users.add(entry.pw_name)

    return sorted(users)
