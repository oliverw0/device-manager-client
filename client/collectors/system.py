import os
import platform
import socket
import time

import psutil

from ..win import IS_WINDOWS


def _local_ip():
    """Primary LAN IP (the source address on the default route). Used as a
    fallback SSH target when the Tailscale address isn't reachable. No packets
    are sent — the UDP connect just resolves which interface would be used."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _hostname() -> str:
    """Real machine name. In a container socket.gethostname() is the container
    id, so prefer the host's /etc/hostname when it's mounted at /host (see
    docker-compose.yml). Native installs fall through to socket."""
    try:
        with open("/host/etc/hostname") as f:
            name = f.read().strip()
            if name:
                return name
    except OSError:
        pass
    return socket.gethostname()


def _uptime_seconds() -> float:
    """Host uptime. /proc/uptime is the host's even inside a container, and
    doesn't depend on psutil's boot_time (which can read container-scoped)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return time.time() - psutil.boot_time()


def _disk_root() -> str:
    # "/" on Windows means the current drive, which for a service may not be C:.
    if IS_WINDOWS:
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def _windows_version() -> str | None:
    """e.g. "Windows Server 2022 Datacenter (21H2)" or "Windows 11 Pro (24H2)".
    ProductName still says "Windows 10" on Windows 11, so fix it up by build."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            def get(name):
                try:
                    return str(winreg.QueryValueEx(k, name)[0])
                except OSError:
                    return ""
            name = get("ProductName")
            build = get("CurrentBuildNumber")
            display = get("DisplayVersion") or get("ReleaseId")
    except OSError:
        return None
    if name.startswith("Windows 10") and build.isdigit() and int(build) >= 22000:
        name = "Windows 11" + name[len("Windows 10"):]
    return f"{name} ({display})" if display else (name or None)


def _linux_version() -> str | None:
    for path in ("/host/etc/os-release", "/etc/os-release"):
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"') or None
        except OSError:
            continue
    return None


def _os_version() -> str | None:
    try:
        return _windows_version() if IS_WINDOWS else _linux_version()
    except Exception:
        return None


def collect() -> dict:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(_disk_root())
    return {
        "hostname": _hostname(),
        "os": "windows" if IS_WINDOWS else platform.system().lower(),
        "os_version": _os_version(),
        "cpu_percent": psutil.cpu_percent(interval=1),
        "cpu_cores": psutil.cpu_count(logical=True),
        "mem_percent": vm.percent,
        "mem_used_bytes": vm.total - vm.available,  # matches how .percent is derived
        "mem_total_bytes": vm.total,
        "disk_percent": du.percent,
        "disk_used_bytes": du.used,
        "disk_total_bytes": du.total,
        "uptime_seconds": _uptime_seconds(),
        "local_ip": _local_ip(),
    }
