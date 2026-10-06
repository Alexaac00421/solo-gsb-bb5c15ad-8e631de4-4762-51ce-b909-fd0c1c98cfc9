"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    daily_cap_cents: int
    transfer_gap_minutes: int
    db_connect_retries: int

    @property
    def conninfo_kwargs(self) -> dict[str, object]:
        return {
            "host": self.postgres_host,
            "port": self.postgres_port,
            "dbname": self.postgres_db,
            "user": self.postgres_user,
            "password": self.postgres_password,
            # Store/interpret timestamps in Beijing time; instants stay unambiguous.
            "options": "-c timezone=Asia/Shanghai",
        }


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"environment variable {name} must be an integer, got {raw!r}")


def load_settings() -> Settings:
    return Settings(
        postgres_host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=_int("POSTGRES_PORT", 5432),
        postgres_db=os.getenv("POSTGRES_DB", "fare"),
        postgres_user=os.getenv("POSTGRES_USER", "fare"),
        postgres_password=os.getenv("POSTGRES_PASSWORD", "farepass"),
        daily_cap_cents=_int("DAILY_CAP_CENTS", 2000),
        transfer_gap_minutes=_int("TRANSFER_GAP_MINUTES", 90),
        db_connect_retries=_int("DB_CONNECT_RETRIES", 60),
    )
