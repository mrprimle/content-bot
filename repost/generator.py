import httpx
from pydantic import BaseModel

from . import config, prompts


class DraftOut(BaseModel):
    linkedin_text: str
    x_text: str
    threads_text: str
    notes: str


_anthropic_client = None


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _finalize(out: DraftOut) -> DraftOut:
    out.linkedin_text = _clip(out.linkedin_text, config.LIMITS["linkedin"])
    out.x_text = _clip(out.x_text, config.LIMITS["twitter"])
    out.threads_text = _clip(out.threads_text, config.LIMITS["threads"])
    out.notes = out.notes.strip()
    return out


def _anthropic_parse(system: str, user: str) -> DraftOut:
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic()
    resp = _anthropic_client.messages.parse(
        model=config.ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=DraftOut,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Модель отказалась обрабатывать этот пост (refusal)")
    if resp.parsed_output is None:
        raise RuntimeError("Модель не вернула структурированный ответ")
    return resp.parsed_output


def _openai_parse(system: str, user: str) -> DraftOut:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в .env")
    schema = {
        "type": "object",
        "properties": {
            "linkedin_text": {"type": "string"},
            "x_text": {"type": "string"},
            "threads_text": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["linkedin_text", "x_text", "threads_text", "notes"],
        "additionalProperties": False,
    }
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": config.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "draft", "strict": True, "schema": schema},
            },
        },
        timeout=180,
    )
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    if msg.get("refusal"):
        raise RuntimeError(f"Модель отказалась: {msg['refusal']}")
    return DraftOut.model_validate_json(msg["content"])


def _parse(system: str, user: str) -> DraftOut:
    if config.llm_provider() == "openai":
        return _finalize(_openai_parse(system, user))
    return _finalize(_anthropic_parse(system, user))


def generate(source: str, date: str, text: str) -> DraftOut:
    """Original Telegram post -> English draft in 3 platform variants."""
    return _parse(prompts.system_prompt(), prompts.user_message(source, date, text))


def adapt(edited_text: str) -> DraftOut:
    """User-edited English text -> refreshed x/threads condensed variants."""
    out = _parse(prompts.ADAPT_SYSTEM, edited_text)
    out.linkedin_text = _clip(edited_text, config.LIMITS["linkedin"])
    return out
