"""Settings read from environment variables (and .env later)."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    host: str = os.environ.get("HOST", "127.0.0.1")
    port: int = int(os.environ.get("PORT", "8000"))
    db_path: Path = Path(os.environ.get("DB_PATH", ROOT / "data" / "tata-ev.db"))
    web_dir: Path = ROOT / "web"
    cors_allowed_origins: tuple[str, ...] = tuple(
        origin.strip().rstrip("/")
        for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    )


settings = Settings()
