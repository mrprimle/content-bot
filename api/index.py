import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
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
            "provider": config.llm_provider(),
            "model": config.llm_model(),
        }
    finally:
        conn.close()


async def _download_bot_file(file_id: str) -> bytes:
    """Fetch a Bot API file with a request-scoped HTTP client.

    Buffer downloads the image while the webhook that asked Buffer to publish is
    still running. On Vercel both requests can share one instance but run on
    different event loops, and the shared python-telegram-bot client is bound to
    the webhook's loop, so reusing it here timed out and Buffer got a 502.
    """
    base = f"https://api.telegram.org/bot{config.BOT_TOKEN}"
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        for attempt in range(3):
            try:
                meta = await client.get(f"{base}/getFile", params={"file_id": file_id})
                meta.raise_for_status()
                file_path = meta.json()["result"]["file_path"]
                response = await client.get(
                    f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{file_path}"
                )
                response.raise_for_status()
                return response.content
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Telegram file download failed: {type(last_error).__name__}")


@app.api_route("/api/media/{access_token}", methods=["GET", "HEAD"])
async def public_media(access_token: str, request: Request) -> Response:
    """Serve a stable public image URL to Buffer without exposing BOT_TOKEN.

    Buffer validates the image with HEAD before sending the post to the
    networks, so HEAD must succeed too (it returned 405 and the scheduled
    posts failed silently on Buffer's side). A ".jpg" suffix is accepted for
    networks that infer the type from the URL.
    """
    access_token = access_token.removesuffix(".jpg")
    conn = db.connect()
    try:
        post = db.post_by_media_token(conn, access_token)
    finally:
        conn.close()
    if post is None or post["media_kind"] not in {"photo", "manual"}:
        raise HTTPException(404, "Media not found")
    try:
        payload = await _download_bot_file(post["bot_media_file_id"])
    except Exception as exc:  # noqa: BLE001
        LOGGER.error(
            "public media fetch failed post_id=%s error=%s",
            post["id"],
            type(exc).__name__,
        )
        raise HTTPException(502, "Media temporarily unavailable") from exc
    headers = {
        "Cache-Control": "public, max-age=86400",
        "Content-Length": str(len(payload)),
    }
    media_type = post["media_mime"] or "image/jpeg"
    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type, headers=headers)
    return Response(content=bytes(payload), media_type=media_type, headers=headers)


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


@app.get("/api/cron/tick/{trigger_utc_hour}")
@app.get("/api/cron/delivery/{trigger_utc_hour}")
async def cron_tick(
    trigger_utc_hour: str,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_bearer(authorization, config.CRON_SECRET, "CRON_SECRET")
    await _ensure_initialized()
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    local_slot = now.strftime("%H:%M")
    publication_slot = next(
        (
            candidate
            for candidate in config.PUBLISH_TIMES
            if int(candidate.split(":", 1)[0]) == now.hour
        ),
        None,
    )
    result: dict[str, object] = {
        "ok": True,
        "trigger": trigger_utc_hour,
        "local_slot": local_slot,
    }
    recovery_conn = db.connect()
    try:
        result["recovered"] = db.recover_planning_publications(
            recovery_conn,
            (now.astimezone(timezone.utc) - timedelta(minutes=15)).isoformat(),
        )
        result["recovered_shelf"] = db.recover_ready_queue_publications(
            recovery_conn,
            (now.astimezone(timezone.utc) - timedelta(minutes=15)).isoformat(),
        )
    finally:
        recovery_conn.close()
    ran = False
    if publication_slot is not None:
        result["publication"] = await repost_bot.publish_scheduled_tick(
            _telegram_app.bot,
            now=now,
        )
        ran = True
    if not ran:
        LOGGER.info("cron no-op trigger=%s local_slot=%s", trigger_utc_hour, local_slot)
        return {
            **result,
            "skipped": "not a configured London publication slot",
        }
    LOGGER.info(
        "cron completed trigger=%s local_slot=%s publication=%s",
        trigger_utc_hour,
        local_slot,
        "publication" in result,
    )
    return result


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
