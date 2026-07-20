import socket
import time

import psutil


def collect() -> dict:
    return {
        "hostname": socket.gethostname(),
        "cpu_percent": psutil.cpu_percent(interval=1),
        "mem_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage("/").percent,
        "uptime_seconds": time.time() - psutil.boot_time(),
    }
