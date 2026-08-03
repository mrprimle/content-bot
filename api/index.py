import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from repost import bot as repost_bot
from repost import config, db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs request URLs at INFO. Telegram embeds the bot token in that URL,
# so production must never emit those records.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
LOGGER = logging.getLogger("repost.api")

app = FastAPI(title="Vahue Content Bot", docs_url=None, redoc_url=None)
_telegram_app = repost_bot.create_application()
_init_lock = asyncio.Lock()
_initialized = False


async def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    async with _init_lock:
        if _initialized:
            return
        await _telegram_app.initialize()
        conn = db.connect()
        try:
            db.reconcile_active_sources(conn, config.read_sources())
        finally:
            conn.close()
        _initialized = True


def _require_bearer(authorization: str | None, expected: str, label: str) -> None:
    if not expected:
        raise HTTPException(503, f"{label} is not configured")
    if authorization != f"Bearer {expected}":
        raise HTTPException(401, "Unauthorized")


@app.get("/")
async def root() -> dict:
    return {"service": "vahue-content-bot", "status": "ok"}


@app.get("/api/health")
async def health() -> dict:
    conn = db.connect()
    try:
        return {
            "status": "ok",
            "database": "postgres" if db.is_postgres(conn) else "sqlite",
            "queue": db.stats(conn),
            "model": config.OPENAI_MODEL,
        }
    finally:
        conn.close()


@app.post("/api/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if not config.WEBHOOK_SECRET or x_telegram_bot_api_secret_token != config.WEBHOOK_SECRET:
        raise HTTPException(401, "Invalid Telegram webhook secret")
    await _ensure_initialized()
    update = Update.de_json(await request.json(), _telegram_app.bot)
    if update is None:
        raise HTTPException(400, "Invalid Telegram update")
    LOGGER.info(
        "webhook update received update_id=%s kind=%s",
        update.update_id,
        "callback" if update.callback_query else "message" if update.effective_message else "other",
    )
    await _telegram_app.process_update(update)
    LOGGER.info("webhook update completed update_id=%s", update.update_id)
    return {"ok": True}


@app.get("/api/cron/delivery/{trigger_utc_hour}")
async def cron_delivery(
    trigger_utc_hour: str,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_bearer(authorization, config.CRON_SECRET, "CRON_SECRET")
    await _ensure_initialized()
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    slot = next(
        (
            candidate
            for candidate in config.POST_TIMES
            if int(candidate.split(":", 1)[0]) == now.hour
        ),
        None,
    )
    if slot is None:
        LOGGER.info("cron no-op trigger=%s london_hour=%s", trigger_utc_hour, now.hour)
        return {
            "ok": True,
            "trigger": trigger_utc_hour,
            "skipped": "not a configured London delivery hour",
        }
    sent = await repost_bot.propose_batch(
        _telegram_app.bot,
        slot_key=f"schedule:{now.date().isoformat()}:{slot}",
        max_items=config.ITEMS_PER_SLOT,
    )
    LOGGER.info("cron completed trigger=%s slot=%s sent=%s", trigger_utc_hour, slot, sent)
    return {"ok": True, "trigger": trigger_utc_hour, "slot": slot, "sent": sent}


@app.post("/api/setup-webhook")
async def setup_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_bearer(authorization, config.CRON_SECRET, "CRON_SECRET")
    await _ensure_initialized()
    base_url = config.PUBLIC_BASE_URL.rstrip("/") if config.PUBLIC_BASE_URL else str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/api/telegram"
    ok = await _telegram_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        secret_token=config.WEBHOOK_SECRET,
        drop_pending_updates=False,
    )
    return {"ok": bool(ok), "webhook_url": webhook_url}
