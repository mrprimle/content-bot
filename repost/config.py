import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


API_ID = _get("TELEGRAM_API_ID")
API_HASH = _get("TELEGRAM_API_HASH")
SESSION = str(ROOT / (_get("TELEGRAM_SESSION") or "repost.session"))

BOT_TOKEN = _get("BOT_TOKEN")
OWNER_CHAT_ID = int(_get("OWNER_CHAT_ID") or 0)

ANTHROPIC_MODEL = _get("ANTHROPIC_MODEL") or "claude-opus-5"
OPENAI_API_KEY = _get("OPENAI_API_KEY")
OPENAI_MODEL = _get("OPENAI_MODEL") or "gpt-5-mini"


def llm_provider() -> str:
    """openai | anthropic: явный LLM_PROVIDER, иначе по наличию ключей."""
    explicit = _get("LLM_PROVIDER").lower()
    if explicit:
        return explicit
    return "openai" if OPENAI_API_KEY else "anthropic"


def llm_model() -> str:
    return OPENAI_MODEL if llm_provider() == "openai" else ANTHROPIC_MODEL

BUFFER_TOKEN = _get("BUFFER_ACCESS_TOKEN")
BUFFER_POST_MODE = _get("BUFFER_POST_MODE") or "addToQueue"

GENERATOR_MODE = _get("GENERATOR_MODE") or "translate"
MIN_POST_CHARS = int(_get("MIN_POST_CHARS") or 150)
POST_TIMES = [t.strip() for t in (_get("POST_TIMES") or "09:00,14:00,19:00").split(",") if t.strip()]
SYNC_DAY = int(_get("SYNC_DAY") or 1)
SYNC_TIME = _get("SYNC_TIME") or "08:00"
SYNC_DAYS = int(_get("SYNC_DAYS") or 35)
TIMEZONE = _get("TIMEZONE") or "Europe/Kyiv"
DB_PATH = str(ROOT / (_get("DB_PATH") or "repost.db"))
AUTHOR_FACTS = _get("AUTHOR_FACTS")

SOURCES_FILE = ROOT / "sources.txt"

LIMITS = {"linkedin": 3000, "twitter": 280, "threads": 500}


def buffer_channels() -> dict[str, str]:
    """BUFFER_CHANNELS='linkedin:id1,twitter:id2' -> {'linkedin': 'id1', ...}"""
    out: dict[str, str] = {}
    for item in _get("BUFFER_CHANNELS").split(","):
        item = item.strip()
        if not item or ":" not in item or item.endswith("CHANNEL_ID"):
            continue
        platform, cid = item.split(":", 1)
        out[platform.strip().lower()] = cid.strip()
    return out


def read_sources() -> list[str]:
    if not SOURCES_FILE.exists():
        return []
    out = []
    for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "t.me/" in line:
            line = "@" + line.rstrip("/").rsplit("/", 1)[-1]
        elif not line.startswith("@"):
            line = "@" + line
        out.append(line)
    return out
