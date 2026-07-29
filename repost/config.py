import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = _get(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


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

MIN_POST_CHARS = int(_get("MIN_POST_CHARS") or 1)
MAX_POST_CHARS = max(1, min(int(_get("MAX_POST_CHARS") or 250), 250))
POST_TIMES = [t.strip() for t in (_get("POST_TIMES") or "10:00,18:00").split(",") if t.strip()]
ITEMS_PER_SLOT = max(1, int(_get("ITEMS_PER_SLOT") or 2))
SYNC_TIME = _get("SYNC_TIME") or "08:00"
SYNC_MONTHS = int(_get("SYNC_MONTHS") or 3)
AUTO_SYNC = _bool("AUTO_SYNC", False)
TIMEZONE = _get("TIMEZONE") or "Europe/London"
DB_PATH = str(ROOT / (_get("DB_PATH") or "repost.db"))
AUTHOR_FACTS = _get("AUTHOR_FACTS")
BOT_SEND_DELAY = float(_get("BOT_SEND_DELAY") or 1.1)
BOT_MEDIA_MAX_BYTES = int(_get("BOT_MEDIA_MAX_BYTES") or 49_000_000)
MEDIA_STAGE_TIMEOUT = float(_get("MEDIA_STAGE_TIMEOUT") or 90)
TRANSCRIPTION_MODEL = _get("TRANSCRIPTION_MODEL") or "gpt-transcribe"
TRANSCRIPTION_MAX_BYTES = int(_get("TRANSCRIPTION_MAX_BYTES") or 25_000_000)

SOURCES_FILE = ROOT / "sources.txt"

LIMITS = {"linkedin": MAX_POST_CHARS, "twitter": MAX_POST_CHARS, "threads": MAX_POST_CHARS}


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
    seen: set[str] = set()
    for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "t.me/" in line:
            line = "@" + line.rstrip("/").rsplit("/", 1)[-1]
        elif not line.startswith("@"):
            line = "@" + line
        line = line.casefold()
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out
