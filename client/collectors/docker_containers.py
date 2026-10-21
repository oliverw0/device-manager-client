from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


def _parse_ts(value: str):
    # Docker's RFC3339 has nanoseconds; fromisoformat only takes microseconds.
    head, _, frac = value.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}")


def _calc_windows_cpu_percent(stats: dict):
    """Windows containers report no system_cpu_usage; CPU time is in 100ns
    ticks, compared against wall time x processor count (as `docker stats`)."""
    try:
        elapsed_100ns = (_parse_ts(stats["read"]) - _parse_ts(stats["preread"])).total_seconds() * 1e7
        cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        possible = elapsed_100ns * (stats.get("num_procs") or 1)
        if possible > 0 and cpu_delta >= 0:
            return round(cpu_delta / possible * 100, 1)
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _calc_windows_mem_percent(stats: dict):
    """Windows containers have no memory limit in stats; report private working
    set as a share of host RAM."""
    try:
        import psutil

        used = stats["memory_stats"]["privateworkingset"]
        return round(used / psutil.virtual_memory().total * 100, 1)
    except (KeyError, TypeError, ZeroDivisionError, ImportError):
        return None


def _is_windows_stats(stats: dict) -> bool:
    return "num_procs" in stats and "system_cpu_usage" not in (stats.get("cpu_stats") or {})


def _calc_cpu_percent(stats: dict):
    if _is_windows_stats(stats):
        return _calc_windows_cpu_percent(stats)
    # Same formula `docker stats` uses. Needs a current + previous sample, which
    # container.stats(stream=False) provides in one call.
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        system_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or 1
        if system_delta > 0 and cpu_delta >= 0:
            return round((cpu_delta / system_delta) * ncpu * 100, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return None


def _calc_mem_percent(stats: dict):
    if _is_windows_stats(stats):
        return _calc_windows_mem_percent(stats)
    try:
        mem = stats["memory_stats"]
        usage = mem["usage"]
        # Match `docker stats`: subtract page cache from usage.
        substats = mem.get("stats") or {}
        cache = substats.get("inactive_file") or substats.get("cache") or 0
        used = max(0, usage - cache)
        limit = mem.get("limit") or 0
        if limit > 0:
            return round(used / limit * 100, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return None


def _fetch_stats(container):
    try:
        return container.id, container.stats(stream=False)
    except Exception:
        return container.id, None


def collect() -> list[dict]:
    try:
        import docker
    except ImportError:
        return []

    try:
        client = docker.from_env()
        containers = client.containers.list(all=True)
    except Exception:
        return []

    # Only running containers have live stats. stats(stream=False) blocks ~1s each
    # (the daemon samples twice for the CPU delta), so fetch them concurrently.
    running = [c for c in containers if c.status == "running"]
    stats_map = {}
    if running:
        try:
            with ThreadPoolExecutor(max_workers=min(8, len(running))) as pool:
                for cid, stats in pool.map(_fetch_stats, running):
                    stats_map[cid] = stats
        except Exception:
            stats_map = {}

    result = []
    for c in containers:
        try:
            image_tags = c.image.tags
            image = image_tags[0] if image_tags else c.image.short_id
            started_at = (c.attrs.get("State") or {}).get("StartedAt")
            stats = stats_map.get(c.id)
            result.append(
                {
                    "name": c.name,
                    "image": image,
                    "status": c.status,
                    "started_at": started_at,
                    "stack": (c.labels or {}).get("com.docker.compose.project"),
                    "cpu_percent": _calc_cpu_percent(stats) if stats else None,
                    "mem_percent": _calc_mem_percent(stats) if stats else None,
                }
            )
        except Exception:
            continue
    return result


if __name__ == "__main__":  # ponytail: Windows-container stats self-check
    win = {
        "read": "2026-10-20T06:00:01.000000000Z", "preread": "2026-10-20T06:00:00.000000000Z",
        "num_procs": 4,
        "cpu_stats": {"cpu_usage": {"total_usage": 30_000_000}},
        "precpu_stats": {"cpu_usage": {"total_usage": 10_000_000}},
        "memory_stats": {"privateworkingset": 0},
    }
    # 2e7 ticks of CPU over 1s (1e7 ticks) x 4 procs = 50%
    assert _calc_cpu_percent(win) == 50.0, _calc_cpu_percent(win)
    linux = {
        "cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 2000, "online_cpus": 2},
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
    }
    assert _calc_cpu_percent(linux) == 20.0
    print("ok")
