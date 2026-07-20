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
    except httpx.HTTPError:
        logger.exception("Failed to send report to %s", url)
        return False
