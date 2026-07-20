import logging

import httpx

from .config import Config
from .collectors import docker_containers, ssh_auth, system, tailscale

logger = logging.getLogger("devicemanager.client")


def gather_report(config: Config) -> dict:
    return {
        "system": system.collect(),
        "tailscale": tailscale.collect(),
        "ssh_auth": ssh_auth.collect(config.ssh_log_window_minutes),
        "docker_containers": docker_containers.collect(),
    }


def send_report(config: Config, report: dict) -> bool:
    url = f"{config.host_url}/api/v1/report"
    headers = {"X-API-Key": config.api_key}
    try:
        response = httpx.post(url, json=report, headers=headers, timeout=config.request_timeout)
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            logger.error(
                "Host rejected the report: HTTP 401 Unauthorized. The API_KEY is wrong, or this "
                "device was deleted / had its key rotated on the host. Copy the current key from "
                "the device's page on the dashboard into %s.",
                "the client config",
            )
        else:
            logger.error("Host returned HTTP %s for %s: %s", status, url, exc.response.text[:200])
        return False
    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.error(
            "Could not connect to the host at %s. Check that: (1) HOST_URL is correct and reachable "
            "from this machine, (2) the host is running and its port is published, (3) no firewall or "
            "Tailscale ACL is blocking it. Try: curl -v %s",
            url, config.host_url,
        )
        return False
    except httpx.HTTPError as exc:
        logger.error("Failed to send report to %s: %s", url, exc)
        return False
