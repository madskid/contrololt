"""Configuration from environment variables."""
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("smartolt.config")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _persisted(filename: str, env: str, nbytes: int = 32) -> str:
    """Read from the environment; if missing, generate once and save in data/ (chmod 600)."""
    value = os.getenv(env)
    if value:
        return value
    path = DATA_DIR / filename
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(nbytes)
    path.write_text(value)
    path.chmod(0o600)
    log.warning("%s is not set -> generated automatically and saved to %s", env, path)
    return value


class Settings:
    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{DATA_DIR}/smartolt.db")
        self.redis_url = os.getenv("REDIS_URL", "")
        self.secret_key = _persisted(".secret_key", "SECRET_KEY")
        self.admin_user = os.getenv("ADMIN_USER", "admin")
        self.admin_pass = _persisted(".admin_pass", "ADMIN_PASS", 9)
        self.https_only = _bool("HTTPS_ONLY", False)
        # True = run the poller/scheduler inside the web process (simple mode).
        # False = run separately with `python -m app.worker` (recommended for production).
        self.run_workers = _bool("RUN_WORKERS", True)
        self.poll_interval = int(os.getenv("POLL_INTERVAL", "60"))          # Status and signal polling interval in seconds
        self.full_sync_hours = float(os.getenv("FULL_SYNC_HOURS", "1"))      # Re-fetch running-config
        self.cli_rx_every = int(os.getenv("CLI_RX_EVERY", "10"))            # CLI: sample Rx per PON every N polls (default ~10 minutes)
        self.autosave_time = os.getenv("AUTOSAVE_TIME", "02:30")             # HH:MM local time
        self.timezone = os.getenv("TZ_NAME", "Asia/Jakarta")
        self.max_olt_concurrency = int(os.getenv("MAX_OLT_CONCURRENCY", "8"))
        self.graph_interval = int(os.getenv("GRAPH_INTERVAL", "300"))        # Seconds between graph samples
        self.snmp_timeout = int(os.getenv("SNMP_TIMEOUT", "5"))
        self.snmp_concurrency = int(os.getenv("SNMP_CONCURRENCY", "16"))
        self.telnet_timeout = int(os.getenv("TELNET_TIMEOUT", "10"))
        self.telnet_idle_close = int(os.getenv("TELNET_IDLE_CLOSE", "90"))


settings = Settings()
