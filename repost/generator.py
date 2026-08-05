import httpx
from pydantic import BaseModel

from . import config, prompts


class DraftOut(BaseModel):
    linkedin_text: str
    x_text: str
    threads_text: str
    notes: str


class TranslationOut(BaseModel):
    full_text: str
    notes: str


_anthropic_client = None


def _validate_full_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise RuntimeError("Модель вернула пустой текст")
    if len(text) > config.MAX_POST_CHARS:
        raise RuntimeError(
            f"Модель вернула {len(text)} символов при лимите {config.MAX_POST_CHARS}; "
            "текст не обрезан, попробуй создать пост ещё раз"
        )
    return text


def _draft(text: str, notes: str = "") -> DraftOut:
    text = _validate_full_text(text)
    return DraftOut(
        linkedin_text=text,
        x_text=text,
        threads_text=text,
        notes=notes.strip(),
    )


def _anthropic_parse(system: str, user: str) -> TranslationOut:
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic()
    resp = _anthropic_client.messages.parse(
        model=config.ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=TranslationOut,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Модель отказалась обрабатывать этот пост (refusal)")
    if resp.parsed_output is None:
        raise RuntimeError("Модель не вернула структурированный ответ")
    return resp.parsed_output


def _openai_parse(system: str, user: str) -> TranslationOut:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в .env")
    schema = {
        "type": "object",
        "properties": {
            "full_text": {
                "type": "string",
                "minLength": 1,
                "maxLength": config.MAX_POST_CHARS,
            },
            "notes": {"type": "string"},
        },
        "required": ["full_text", "notes"],
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
                "json_schema": {"name": "translation", "strict": True, "schema": schema},
            },
        },
        timeout=180,
    )
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    if msg.get("refusal"):
        raise RuntimeError(f"Модель отказалась: {msg['refusal']}")
    return TranslationOut.model_validate_json(msg["content"])


def _parse(system: str, user: str) -> TranslationOut:
    if config.llm_provider() == "openai":
        return _openai_parse(system, user)
    return _anthropic_parse(system, user)


def translate_post(source: str, date: str, text: str) -> DraftOut:
    """Translate the full source once; every platform receives the same text."""
    out = _parse(
        prompts.TRANSLATE_SYSTEM,
        prompts.user_message(source, date, text),
    )
    return _draft(out.full_text, out.notes)


def generate(source: str, date: str, text: str) -> DraftOut:
    """Backward-compatible alias: ordinary posts are always translated."""
    return translate_post(source, date, text)


def revise_post(current_text: str, instruction: str) -> DraftOut:
    """Apply one owner instruction to the current draft through Terra."""
    current_text = current_text.strip()
    instruction = instruction.strip()
    if not current_text:
        raise RuntimeError("Активный черновик пуст")
    if not instruction:
        raise RuntimeError("Инструкция для AI пуста")
    out = _parse(
        prompts.REVISE_SYSTEM,
        prompts.revise_message(current_text, instruction),
    )
    return _draft(out.full_text, out.notes)


def adapt(edited_text: str) -> DraftOut:
    """An owner's English edit is authoritative and needs no second AI call."""
    text = edited_text.strip()
    if not text:
        raise RuntimeError("Пустой текст нельзя опубликовать")
    if len(text) > config.MANUAL_MAX_POST_CHARS:
        raise RuntimeError(
            f"Ручной текст содержит {len(text)} символов при лимите "
            f"{config.MANUAL_MAX_POST_CHARS} для публикации во все площадки"
        )
    return DraftOut(
        linkedin_text=text,
        x_text=text,
        threads_text=text,
        notes="",
    )
