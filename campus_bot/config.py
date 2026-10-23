from dataclasses import dataclass
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Small .env reader; the process environment always has priority."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().isidentifier():
            raise ValueError("Неверная строка .env")
        os.environ.setdefault(key.strip(), value.strip().strip("\"").strip("'"))


@dataclass(frozen=True)
class Config:
    token: str
    owner_id: int
    timezone: ZoneInfo
    data_dir: Path
    lessons_path: Path
    mail_user: str = ""
    mail_password: str = ""
    mail_host: str = "imap.gmail.com"

    @classmethod
    def load(cls, require_token: bool = True):
        load_env(ROOT / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if require_token and (not token or ":" not in token):
            raise ValueError("Заполните BOT_TOKEN в .env (см. .env.example)")
        return cls(
            token, int(os.getenv("OWNER_ID", "0") or 0),
            ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
            Path(os.getenv("DATA_DIR", str(ROOT / "runtime"))).resolve(),
            ROOT / "data" / "personal_schedule.json",
            os.getenv("MAIL_USER", "").strip(),
            # Google shows app passwords in groups of four; IMAP expects them without spaces.
            os.getenv("MAIL_PASSWORD", "").replace(" ", ""),
            os.getenv("MAIL_IMAP_HOST", "imap.gmail.com").strip(),
        )
