import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    headless: bool
    default_proxy: Optional[str]
    user_agent: Optional[str]
    referer: Optional[str]
    cookie: Optional[str]
    screenshot_dir: str


def _bool_env(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def get_config() -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return Config(
        db_host=os.getenv("DB_HOST", "127.0.0.1"),
        db_port=int(os.getenv("DB_PORT", "5432")),
        db_user=os.getenv("DB_USER", "postgres"),
        db_password=os.getenv("DB_PASSWORD", "1313575799"),
        db_name=os.getenv("DB_NAME", "domainmonitor"),
        headless=_bool_env("HEADLESS", True),
        default_proxy=os.getenv("DEFAULT_PROXY"),
        user_agent=os.getenv("USER_AGENT"),
        referer=os.getenv("REFERER"),
        cookie=os.getenv("COOKIE"),
        screenshot_dir=os.getenv("SCREENSHOT_DIR", os.path.join(".", "screenshots")),
    )

