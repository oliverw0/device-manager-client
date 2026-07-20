import re
import subprocess
from pathlib import Path

ACCEPTED_RE = re.compile(r"Accepted (?:password|publickey) for (\S+) from (\S+)")
FAILED_RE = re.compile(r"Failed password for (?:invalid user )?(\S+) from (\S+)")

AUTH_LOG_PATHS = [Path("/var/log/auth.log"), Path("/var/log/secure")]


def _lines_from_journalctl(window_minutes: int) -> list[str]:
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u", "ssh", "-u", "sshd",
                "--since", f"-{window_minutes}min",
                "--no-pager",
                "-o", "short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _lines_from_auth_log(max_lines: int = 500) -> list[str]:
    for path in AUTH_LOG_PATHS:
        if path.exists():
            try:
                with path.open("r", errors="ignore") as f:
                    return f.readlines()[-max_lines:]
            except PermissionError:
                return []
    return []


def collect(window_minutes: int = 15) -> dict:
    lines = _lines_from_journalctl(window_minutes)
    if not lines:
        lines = _lines_from_auth_log()

    accepted_count = 0
    failed_count = 0
    last_accepted = None
    last_failed = None

    for line in lines:
        timestamp = line.split(" ", 1)[0] if line else ""

        m = ACCEPTED_RE.search(line)
        if m:
            accepted_count += 1
            last_accepted = {"user": m.group(1), "ip": m.group(2), "at": timestamp}
            continue

        m = FAILED_RE.search(line)
        if m:
            failed_count += 1
            last_failed = {"ip": m.group(2), "at": timestamp}

    return {
        "recent_accepted_count": accepted_count,
        "recent_failed_count": failed_count,
        "last_accepted_user": last_accepted["user"] if last_accepted else None,
        "last_accepted_ip": last_accepted["ip"] if last_accepted else None,
        "last_accepted_at": last_accepted["at"] if last_accepted else None,
        "last_failed_ip": last_failed["ip"] if last_failed else None,
        "last_failed_at": last_failed["at"] if last_failed else None,
    }
