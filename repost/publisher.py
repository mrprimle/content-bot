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


def create_post(channel_id: str, text: str) -> str:
    query = (
        "mutation { createPost(input: {"
        f"text: {json.dumps(text)}, "
        f"channelId: {json.dumps(channel_id)}, "
        f"schedulingType: automatic, mode: {config.BUFFER_POST_MODE}"
        "}) { ... on PostActionSuccess { post { id } } ... on MutationError { message } } }"
    )
    res = _gql(query)["createPost"]
    if res.get("message"):
        raise RuntimeError(res["message"])
    return res["post"]["id"]


def _clip_for_platform(platform: str, text: str) -> str:
    text = text.strip()
    limit = config.LIMITS.get(platform)
    if not limit or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def publish_all(texts_by_platform: dict[str, str]) -> dict[str, tuple[bool, str]]:
    """{'linkedin': text, ...} -> {'linkedin': (ok, post_id | error), ...}"""
    results: dict[str, tuple[bool, str]] = {}
    channels = config.buffer_channels()
    if not channels:
        return {"buffer": (False, "BUFFER_CHANNELS не настроен в .env")}
    for platform, channel_id in channels.items():
        if platform not in texts_by_platform:
            continue
        text = texts_by_platform[platform]
        if not text or not text.strip():
            results[platform] = (
                False,
                "Пустой текст для настроенной площадки; публикация не выполнена",
            )
            continue
        text = _clip_for_platform(platform, text)
        try:
            results[platform] = (True, create_post(channel_id, text))
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
