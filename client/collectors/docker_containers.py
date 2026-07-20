from concurrent.futures import ThreadPoolExecutor


def _calc_cpu_percent(stats: dict):
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
                    "cpu_percent": _calc_cpu_percent(stats) if stats else None,
                    "mem_percent": _calc_mem_percent(stats) if stats else None,
                }
            )
        except Exception:
            continue
    return result
