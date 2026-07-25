"""Buffer GraphQL API client: https://api.buffer.com, Bearer-token auth.

Публикация идёт через мутацию createPost (mode=addToQueue кладёт пост в очередь
канала Buffer; слоты очереди настраиваются в Buffer). Каналы задаются в
BUFFER_CHANNELS как platform:channelId.
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


def publish_all(texts_by_platform: dict[str, str]) -> dict[str, tuple[bool, str]]:
    """{'linkedin': text, ...} -> {'linkedin': (ok, post_id | error), ...}"""
    results: dict[str, tuple[bool, str]] = {}
    channels = config.buffer_channels()
    if not channels:
        return {"buffer": (False, "BUFFER_CHANNELS не настроен в .env")}
    for platform, channel_id in channels.items():
        text = texts_by_platform.get(platform)
        if not text:
            continue
        try:
            results[platform] = (True, create_post(channel_id, text))
        except Exception as e:  # noqa: BLE001 — репортим любую ошибку per-platform
            results[platform] = (False, str(e)[:300])
    return results


def list_channels() -> list[dict]:
    last: Exception | None = None
    for query in (
        "query { channels { id name service } }",
        "query { account { channels { id name service } } }",
    ):
        try:
            data = _gql(query)
            channels = data.get("channels") or (data.get("account") or {}).get("channels")
            if channels:
                return channels
        except Exception as e:  # noqa: BLE001 — пробуем следующий вариант схемы
            last = e
    raise last or RuntimeError("каналы не найдены")


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
