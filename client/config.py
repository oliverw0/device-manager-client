import os
import sys


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _normalize_host_url(value: str) -> str:
    # Tolerate a bare host:port (a very easy thing to type). Without a scheme,
    # httpx can't build a request and every report silently fails.
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value


class Config:
    def __init__(self) -> None:
        self.host_url: str = _normalize_host_url(_require("HOST_URL"))
        self.api_key: str = _require("API_KEY")
        self.report_interval: int = int(os.environ.get("REPORT_INTERVAL", "60"))
        self.ssh_log_window_minutes: int = int(os.environ.get("SSH_LOG_WINDOW_MINUTES", "15"))
        self.request_timeout: int = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines into os.environ (without overriding what's already
    set). Windows has no systemd EnvironmentFile, so the scheduled task passes
    --env-file instead. Tolerates comments, blank lines, quotes and a BOM."""
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def load_config() -> Config:
    try:
        return Config()
    except ConfigError as exc:
        print(f"[devicemanager-client] {exc}", file=sys.stderr)
        raise SystemExit(1)
