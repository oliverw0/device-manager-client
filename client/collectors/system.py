import socket
import time

import psutil


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


def collect() -> dict:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    return {
        "hostname": _hostname(),
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
