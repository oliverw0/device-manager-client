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


def collect() -> dict:
    return {
        "hostname": socket.gethostname(),
        "cpu_percent": psutil.cpu_percent(interval=1),
        "mem_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage("/").percent,
        "uptime_seconds": time.time() - psutil.boot_time(),
        "local_ip": _local_ip(),
    }
