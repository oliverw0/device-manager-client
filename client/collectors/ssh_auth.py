import html
import re
import subprocess
from pathlib import Path

from ..win import IS_WINDOWS

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


EVENT_TIME_RE = re.compile(r"SystemTime=['\"]([^'\"]+)['\"]")
EVENT_DATA_RE = re.compile(r"<Data[^>]*>(.*?)</Data>", re.S)


def _parse_wevtutil_xml(xml: str) -> list[str]:
    """wevtutil /f:xml prints bare <Event> elements back to back (no root).
    Flatten each into the journal-style "<timestamp> <message>" line the parser
    below expects. sshd's text lives in the EventData payload."""
    lines = []
    for chunk in xml.split("<Event ")[1:]:
        ts = EVENT_TIME_RE.search(chunk)
        message = " ".join(html.unescape(d) for d in EVENT_DATA_RE.findall(chunk))
        if ts and message:
            lines.append(f"{ts.group(1)} {message}")
    return lines


def _lines_from_windows_eventlog(window_minutes: int) -> list[str]:
    """Windows OpenSSH logs to the OpenSSH/Operational channel with the same
    "Accepted publickey for ..." text as Linux. XML rather than /f:text, whose
    field labels are localized."""
    query = f"*[System[TimeCreated[timediff(@SystemTime) <= {window_minutes * 60 * 1000}]]]"
    try:
        result = subprocess.run(
            ["wevtutil", "qe", "OpenSSH/Operational", f"/q:{query}", "/f:xml"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:  # channel missing = OpenSSH server not installed
        return []
    return _parse_wevtutil_xml(result.stdout)


def collect(window_minutes: int = 15) -> dict:
    if IS_WINDOWS:
        lines = _lines_from_windows_eventlog(window_minutes)
    else:
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


if __name__ == "__main__":  # ponytail: Windows event-log parser self-check
    sample = (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
        "<TimeCreated SystemTime='2026-10-20T06:00:00.1234567Z'/></System><EventData>"
        "<Data Name='process'>sshd</Data><Data Name='payload'>Accepted publickey for oliver from 100.64.0.5 port 50123 ssh2: ED25519 SHA256:abc</Data>"
        "</EventData></Event>"
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
        "<TimeCreated SystemTime='2026-10-20T06:01:00Z'/></System><EventData>"
        "<Data Name='process'>sshd</Data><Data Name='payload'>Failed password for invalid user admin from 10.0.0.9 port 4 ssh2</Data>"
        "</EventData></Event>"
    )
    lines = _parse_wevtutil_xml(sample)
    assert lines[0].startswith("2026-10-20T06:00:00.1234567Z sshd Accepted publickey for oliver"), lines
    assert len(lines) == 2
    assert ACCEPTED_RE.search(lines[0]).groups() == ("oliver", "100.64.0.5")
    assert FAILED_RE.search(lines[1]).group(2) == "10.0.0.9"
    print("ok")
