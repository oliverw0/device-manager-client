import logging
import sys
import time

from .config import load_config
from .sender import gather_report, send_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("devicemanager.client")


def run_once() -> int:
    """Gather and send a single report, then exit. Returns a process exit code.
    Useful as a connectivity self-test: `python -m client.main --once`."""
    config = load_config()
    logger.info("One-shot test: reporting to %s", config.host_url)
    report = gather_report(config)
    logger.info(
        "Collected: cpu=%.0f%% mem=%.0f%% tailscale=%s containers=%d",
        report["system"]["cpu_percent"],
        report["system"]["mem_percent"],
        report["tailscale"].get("backend_state"),
        len(report["docker_containers"]),
    )
    if send_report(config, report):
        logger.info("SUCCESS: host accepted the report. This device should now show online.")
        return 0
    logger.error("FAILED: see the error above. The report was not accepted by the host.")
    return 1


def run_forever() -> None:
    config = load_config()
    logger.info("Starting devicemanager client, reporting to %s every %ss", config.host_url, config.report_interval)
    while True:
        try:
            report = gather_report(config)
            ok = send_report(config, report)
            if ok:
                logger.info("Report sent (cpu=%.0f%% mem=%.0f%%)", report["system"]["cpu_percent"], report["system"]["mem_percent"])
        except Exception:
            logger.exception("Unexpected error while collecting/sending report")
        time.sleep(config.report_interval)


if __name__ == "__main__":
    if "--once" in sys.argv:
        raise SystemExit(run_once())
    run_forever()
