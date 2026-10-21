"""Windows helpers shared by the collectors. Everything here is a no-op off
Windows, so collectors can call it unconditionally."""
import base64
import json
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

# No console window flashing up if the client is ever run interactively.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_powershell(script: str, timeout: int = 30) -> str | None:
    """Run a Windows PowerShell 5.1 script (present on every supported desktop
    and Server build) and return stdout, or None on any failure. The script is
    passed with -EncodedCommand so no quoting survives to bite us."""
    if not IS_WINDOWS:
        return None
    script = "$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def powershell_json(script: str, timeout: int = 30):
    """run_powershell for a script whose output is one ConvertTo-Json blob."""
    out = run_powershell("[Console]::OutputEncoding=[Text.Encoding]::UTF8\n" + script, timeout)
    if not out or not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None
