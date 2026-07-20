import logging
import time

from .config import load_config
from .sender import gather_report, send_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("devicemanager.client")


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
    run_forever()
