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

    result = []
    for c in containers:
        try:
            image_tags = c.image.tags
            image = image_tags[0] if image_tags else c.image.short_id
            started_at = (c.attrs.get("State") or {}).get("StartedAt")
            result.append(
                {
                    "name": c.name,
                    "image": image,
                    "status": c.status,
                    "started_at": started_at,
                }
            )
        except Exception:
            continue
    return result
