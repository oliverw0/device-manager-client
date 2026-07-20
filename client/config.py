import os
import sys


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


class Config:
    def __init__(self) -> None:
        self.host_url: str = _require("HOST_URL").rstrip("/")
        self.api_key: str = _require("API_KEY")
        self.report_interval: int = int(os.environ.get("REPORT_INTERVAL", "60"))
        self.ssh_log_window_minutes: int = int(os.environ.get("SSH_LOG_WINDOW_MINUTES", "15"))
        self.request_timeout: int = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))


def load_config() -> Config:
    try:
        return Config()
    except ConfigError as exc:
        print(f"[devicemanager-client] {exc}", file=sys.stderr)
        raise SystemExit(1)
