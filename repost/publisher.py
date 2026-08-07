"""Buffer GraphQL API client: https://api.buffer.com, Bearer-token auth.

Публикация идёт через мутацию createPost. Режим задаётся в BUFFER_POST_MODE:
shareNow публикует сразу, addToQueue кладёт пост в очередь канала Buffer.
Каналы задаются в BUFFER_CHANNELS как platform:channelId.
"""
import argparse
import json

import httpx

from . import config

API_URL = "https://api.buffer.com"


def _gql(query: str, variables: dict | None = None) -> dict:
    if not config.BUFFER_TOKEN:
        raise RuntimeError("BUFFER_ACCESS_TOKEN не задан в .env")
    r = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {config.BUFFER_TOKEN}"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], ensure_ascii=False)[:500])
    return data["data"]


def create_post(
    channel_id: str,
    text: str,
    *,
    thread_platform: str | None = None,
    thread: list[str] | None = None,
    image_url: str | None = None,
) -> str:
    metadata = ""
    if thread_platform and thread:
        entries = []
        for index, item in enumerate(thread):
            item_assets = ""
            if index == 0 and image_url:
                item_assets = (
                    f", assets: [{{ image: {{ url: {json.dumps(image_url)} }} }}]"
                )
            entries.append(f"{{ text: {json.dumps(item)}{item_assets} }}")
        items = ", ".join(entries)
        metadata = f", metadata: {{{thread_platform}: {{thread: [{items}]}}}}"
    assets = ""
    if image_url and not (thread_platform and thread):
        assets = f", assets: [{{ image: {{ url: {json.dumps(image_url)} }} }}]"
    query = (
        "mutation { createPost(input: {"
        f"text: {json.dumps(text)}, "
        f"channelId: {json.dumps(channel_id)}, "
        f"schedulingType: automatic, mode: {config.BUFFER_POST_MODE}"
        f"{metadata}{assets}"
        "}) { ... on PostActionSuccess { post { id } } ... on MutationError { message } } }"
    )
    res = _gql(query)["createPost"]
    if res.get("message"):
        raise RuntimeError(res["message"])
    return res["post"]["id"]


def split_for_thread(text: str, limit: int) -> list[str]:
    """Split without dropping words or ideas; prefer paragraph/sentence boundaries."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    separators = ("\n\n", "\n", ". ", "! ", "? ", "; ", ": ", ", ", " ")
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        best_index = -1
        best_separator = ""
        minimum = max(1, limit // 2)
        for separator in separators:
            index = window.rfind(separator, minimum)
            if index > best_index:
                best_index = index
                best_separator = separator
        if best_index < 0:
            cut = limit
        else:
            # Keep punctuation in the previous chunk; whitespace starts the next.
            cut = best_index + len(best_separator.rstrip())
            if cut <= 0:
                cut = best_index
        chunk = remaining[:cut].strip()
        if not chunk:
            chunk = remaining[:limit]
            cut = limit
        chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _publication_payload(
    platform: str,
    text: str | list[str],
) -> tuple[str, str | None, list[str] | None]:
    if isinstance(text, list):
        if platform != "threads":
            raise ValueError(f"Список thread-items нельзя отправить в {platform}")
        chunks = [item.strip() for item in text if item and item.strip()]
        if not chunks:
            raise ValueError("Threads-план пуст")
        if len(chunks) > config.THREAD_MAX_ITEMS:
            raise ValueError(
                f"Threads-план содержит {len(chunks)} частей при лимите "
                f"{config.THREAD_MAX_ITEMS}"
            )
        too_long = [
            index
            for index, item in enumerate(chunks, start=1)
            if len(item) > config.THREAD_ITEM_CHARS
        ]
        if too_long:
            raise ValueError(
                f"Threads-карточки {too_long} длиннее {config.THREAD_ITEM_CHARS} символов"
            )
        return (
            chunks[0],
            "threads" if len(chunks) > 1 else None,
            chunks if len(chunks) > 1 else None,
        )
    text = text.strip()
    if platform == "twitter":
        if len(text) <= config.LIMITS["twitter"]:
            # X Basic/Premium/Premium+ accepts one long post (Show more).
            return text, None, None
        raise ValueError(
            f"Текст для X содержит {len(text)} символов при лимите "
            f"{config.LIMITS['twitter']}; публикация остановлена без обрезания"
        )
    if platform == "threads":
        chunks = split_for_thread(text, config.THREAD_ITEM_CHARS)
        if len(chunks) > config.THREAD_MAX_ITEMS:
            raise ValueError(
                f"Threads требует {len(chunks)} частей при лимите "
                f"{config.THREAD_MAX_ITEMS}; публикация остановлена без обрезания"
            )
        return chunks[0], "threads" if len(chunks) > 1 else None, chunks if len(chunks) > 1 else None
    limit = config.LIMITS.get(platform)
    if limit and len(text) > limit:
        raise ValueError(
            f"Текст для {platform} содержит {len(text)} символов при лимите {limit}; "
            "публикация остановлена без обрезания"
        )
    return text, None, None


def publish_all(
    texts_by_platform: dict[str, str | list[str]],
    image_url: str | None = None,
) -> dict[str, tuple[bool, str]]:
    """Publish master strings plus an optional explicit Threads item list."""
    results: dict[str, tuple[bool, str]] = {}
    channels = config.buffer_channels()
    if not channels:
        return {"buffer": (False, "BUFFER_CHANNELS не настроен в .env")}
    for platform, channel_id in channels.items():
        if platform not in texts_by_platform:
            continue
        text = texts_by_platform[platform]
        if not text or (isinstance(text, str) and not text.strip()):
            results[platform] = (
                False,
                "Пустой текст для настроенной площадки; публикация не выполнена",
            )
            continue
        try:
            first, thread_platform, thread = _publication_payload(platform, text)
            results[platform] = (
                True,
                create_post(
                    channel_id,
                    first,
                    thread_platform=thread_platform,
                    thread=thread,
                    image_url=image_url,
                ),
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            results[platform] = (
                False,
                "UNKNOWN: Buffer мог принять публикацию, но соединение оборвалось. "
                f"Проверь Buffer перед повтором ({str(e)[:180]})",
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                results[platform] = (
                    False,
                    "UNKNOWN: Buffer вернул серверную ошибку; публикация могла быть создана. "
                    f"Проверь Buffer перед повтором ({e.response.status_code})",
                )
            else:
                results[platform] = (False, str(e)[:300])
        except Exception as e:  # noqa: BLE001 — репортим любую ошибку per-platform
            results[platform] = (False, str(e)[:300])
    return results


def list_channels() -> list[dict]:
    account = _gql("query { account { organizations { id name } } }")["account"]
    organizations = account.get("organizations") or []
    if not organizations:
        raise RuntimeError("в Buffer не найдено ни одной организации")

    channels: list[dict] = []
    seen: set[str] = set()
    for organization in organizations:
        organization_id = json.dumps(organization["id"])
        data = _gql(
            "query { channels(input: {"
            f"organizationId: {organization_id}"
            "}) { id name service } }"
        )
        for channel in data.get("channels") or []:
            if channel["id"] not in seen:
                seen.add(channel["id"])
                channels.append(channel)
    return channels


def main() -> None:
    ap = argparse.ArgumentParser(description="Buffer publisher")
    ap.add_argument("--channels", action="store_true", help="показать подключённые каналы и их id")
    ap.add_argument("--test", metavar="TEXT", help="отправить тестовый пост во все настроенные каналы")
    args = ap.parse_args()
    if args.channels:
        for ch in list_channels():
            print(f"{ch.get('service'):<12} {ch.get('id')}  {ch.get('name')}")
    elif args.test:
        for platform, (ok, info) in publish_all(
            {p: args.test for p in config.buffer_channels()}
        ).items():
            print(f"{platform}: {'OK ' + info if ok else 'FAIL ' + info}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
