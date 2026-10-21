import os
import platform
import socket
import time

import psutil

from ..win import IS_WINDOWS


def _local_ip():
    """Primary LAN IP (the source address on the default route). Used as a
    fallback SSH target when the Tailscale address isn't reachable. No packets
    are sent — the UDP connect just resolves which interface would be used.
    None in the Docker client: there it's the container's bridge address
    (172.17.x), which the host can't use and may even reach another machine
    with; the host falls back to the address the report came from instead."""
    if os.path.exists("/.dockerenv"):
        return None
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


# Mounted filesystems that aren't real drives (snap images, container layers, RAM disks).
_SKIP_FSTYPES = {"squashfs", "overlay", "tmpfs", "devtmpfs", "iso9660", "udf", ""}


def _volume_label(mount: str) -> str | None:
    if not IS_WINDOWS:
        return None
    import ctypes

    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(mount, buf, len(buf), None, None, None, None, 0)
    return buf.value or None if ok else None


def _disks() -> list[dict]:
    """Every fixed drive, system drive first. In the Docker client this only sees
    the container's own mounts, so it usually reports just the root."""
    out, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype.lower() in _SKIP_FSTYPES or part.device in seen:
            continue
        if IS_WINDOWS and "fixed" not in part.opts:  # skips CD drives and USB sticks
            continue
        try:
            du = psutil.disk_usage(part.mountpoint)
        except OSError:  # unmounted / no media
            continue
        seen.add(part.device)
        out.append({
            "mount": part.mountpoint,
            "label": _volume_label(part.mountpoint),
            "fstype": part.fstype,
            "percent": du.percent,
            "used_bytes": du.used,
            "total_bytes": du.total,
        })
    root = _disk_root().lower()
    out.sort(key=lambda d: (d["mount"].lower() != root, d["mount"]))
    return out


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


def _docker_host_os() -> str | None:
    """In the Docker client, /etc/os-release is the image's (Debian). The Docker
    daemon knows the machine's real distro, over the socket we already mount —
    so no extra mount (and no edit to anyone's docker-compose.yml) is needed."""
    try:
        import docker

        return docker.from_env(timeout=5).info().get("OperatingSystem") or None
    except Exception:
        return None


def _linux_version() -> str | None:
    if os.path.exists("/.dockerenv") and not os.path.exists("/host/etc/os-release"):
        found = _docker_host_os()
        if found:
            return found
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


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\0", b"").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _hardware_model() -> str | None:
    """e.g. "Apple MacBook Air (M1, 2020)", "Dell Inc. OptiPlex 7070".
    ARM boards (Apple Silicon, Raspberry Pi) name themselves in the device tree;
    PCs in DMI. Docker hides /sys/firmware, so the Docker client may get None."""
    try:
        if IS_WINDOWS:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as k:
                parts = [str(winreg.QueryValueEx(k, n)[0]).strip() for n in ("SystemManufacturer", "SystemProductName")]
            return " ".join(p for p in parts if p) or None
        model = _read("/sys/firmware/devicetree/base/model")
        if model:
            return model
        vendor, product = _read("/sys/class/dmi/id/sys_vendor"), _read("/sys/class/dmi/id/product_name")
        return " ".join(p for p in (vendor, product) if p and p.lower() not in ("to be filled by o.e.m.", "system product name")) or None
    except Exception:
        return None


def collect() -> dict:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(_disk_root())
    return {
        "hostname": _hostname(),
        "os": "windows" if IS_WINDOWS else platform.system().lower(),
        "os_version": _os_version(),
        # The kernel is the machine's even inside the Docker client (it's shared),
        # so it still identifies e.g. Asahi Linux when /sys/firmware is hidden.
        "kernel": None if IS_WINDOWS else (platform.release() or None),
        "hardware_model": _hardware_model(),
        "cpu_percent": psutil.cpu_percent(interval=1),
        "cpu_cores": psutil.cpu_count(logical=True),
        "mem_percent": vm.percent,
        "mem_used_bytes": vm.total - vm.available,  # matches how .percent is derived
        "mem_total_bytes": vm.total,
        "disk_percent": du.percent,
        "disk_used_bytes": du.used,
        "disk_total_bytes": du.total,
        "disks": _disks(),
        "uptime_seconds": _uptime_seconds(),
        "local_ip": _local_ip(),
    }
