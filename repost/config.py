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
OPENAI_MODEL = _get("OPENAI_MODEL") or "gpt-5.6-terra"


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
MAX_POST_CHARS = max(1, min(int(_get("MAX_POST_CHARS") or 1500), 1500))
MANUAL_MAX_POST_CHARS = max(
    MAX_POST_CHARS,
    min(int(_get("MANUAL_MAX_POST_CHARS") or 3000), 3000),
)
X_PREMIUM = _bool("X_PREMIUM", False)
PLANNING_TIME = _get("PLANNING_TIME") or "21:00"
PUBLISH_TIMES = [
    t.strip()
    for t in (_get("PUBLISH_TIMES") or "09:00,14:00,19:00").split(",")
    if t.strip()
]
DAILY_POSTS = max(1, int(_get("DAILY_POSTS") or 3))
if len(PUBLISH_TIMES) != DAILY_POSTS:
    raise RuntimeError(
        f"PUBLISH_TIMES содержит {len(PUBLISH_TIMES)} слотов, "
        f"но DAILY_POSTS={DAILY_POSTS}"
    )
# Kept for manual/test delivery helpers; scheduled planning always offers one
# candidate at a time and creates DAILY_POSTS durable slots.
ITEMS_PER_SLOT = max(1, int(_get("ITEMS_PER_SLOT") or 1))
SYNC_TIME = _get("SYNC_TIME") or "08:00"
SYNC_MONTHS = int(_get("SYNC_MONTHS") or 3)
AUTO_SYNC = _bool("AUTO_SYNC", False)
TIMEZONE = _get("TIMEZONE") or "Europe/London"
DB_PATH = str(ROOT / (_get("DB_PATH") or "repost.db"))
DATABASE_URL = _get("DATABASE_URL")
TELEGRAM_SESSION_STRING = _get("TELEGRAM_SESSION_STRING")
WEBHOOK_SECRET = _get("WEBHOOK_SECRET")
CRON_SECRET = _get("CRON_SECRET")
PUBLIC_BASE_URL = _get("PUBLIC_BASE_URL")
AUTHOR_FACTS = _get("AUTHOR_FACTS")
BOT_SEND_DELAY = float(_get("BOT_SEND_DELAY") or 1.1)
BOT_MEDIA_MAX_BYTES = int(_get("BOT_MEDIA_MAX_BYTES") or 49_000_000)
MEDIA_STAGE_TIMEOUT = float(_get("MEDIA_STAGE_TIMEOUT") or 90)
SOURCES_FILE = ROOT / "sources.txt"

LIMITS = {
    "linkedin": MANUAL_MAX_POST_CHARS,
    "twitter": 25_000 if X_PREMIUM else 280,
    "threads": 500,
}


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
