import httpx
from pydantic import BaseModel, Field

from . import config, prompts


class DraftOut(BaseModel):
    linkedin_text: str
    x_text: str
    threads_text: str
    notes: str
    thread_items: list[str] = Field(default_factory=list)


class TranslationOut(BaseModel):
    full_text: str
    thread_items: list[str]
    notes: str


class ThreadPlanOut(BaseModel):
    thread_items: list[str]
    notes: str


_anthropic_client = None


def _validate_full_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if not text:
        raise RuntimeError("Модель вернула пустой текст")
    if len(text) > max_chars:
        raise RuntimeError(
            f"Модель вернула {len(text)} символов при лимите {max_chars}; "
            "текст не обрезан, попробуй создать пост ещё раз"
        )
    return text


def _validate_thread_items(items: list[str]) -> list[str]:
    clean = [item.strip() for item in items if item and item.strip()]
    if not clean:
        raise RuntimeError("Модель не вернула Threads-план")
    if len(clean) > config.THREAD_MAX_ITEMS:
        raise RuntimeError(
            f"Модель вернула {len(clean)} Threads-карточек при лимите {config.THREAD_MAX_ITEMS}"
        )
    too_long = [
        index
        for index, item in enumerate(clean, start=1)
        if len(item) > config.THREAD_ITEM_CHARS
    ]
    if too_long:
        raise RuntimeError(
            "Threads-карточки превышают лимит "
            f"{config.THREAD_ITEM_CHARS}: {', '.join(map(str, too_long))}"
        )
    return clean


def _draft(
    text: str,
    notes: str = "",
    thread_items: list[str] | None = None,
    *,
    max_chars: int,
) -> DraftOut:
    text = _validate_full_text(text, max_chars)
    items = _validate_thread_items(thread_items or [])
    return DraftOut(
        linkedin_text=text,
        x_text=text,
        threads_text=text,
        notes=notes.strip(),
        thread_items=items,
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


def _openai_parse(system: str, user: str, max_chars: int) -> TranslationOut:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в .env")
    schema = {
        "type": "object",
        "properties": {
            "full_text": {
                "type": "string",
                "minLength": 1,
                "maxLength": max_chars,
            },
            "notes": {"type": "string"},
            "thread_items": {
                "type": "array",
                "minItems": 1,
                "maxItems": config.THREAD_MAX_ITEMS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": config.THREAD_ITEM_CHARS,
                },
            },
        },
        "required": ["full_text", "thread_items", "notes"],
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


def _parse(system: str, user: str, max_chars: int) -> TranslationOut:
    if config.llm_provider() == "openai":
        return _openai_parse(system, user, max_chars)
    return _anthropic_parse(system, user)


def _anthropic_thread_parse(system: str, user: str) -> ThreadPlanOut:
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic()
    resp = _anthropic_client.messages.parse(
        model=config.ANTHROPIC_MODEL,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=ThreadPlanOut,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Модель отказалась создавать Threads-план (refusal)")
    if resp.parsed_output is None:
        raise RuntimeError("Модель не вернула Threads-план")
    return resp.parsed_output


def _openai_thread_parse(system: str, user: str) -> ThreadPlanOut:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в .env")
    schema = {
        "type": "object",
        "properties": {
            "thread_items": {
                "type": "array",
                "minItems": 1,
                "maxItems": config.THREAD_MAX_ITEMS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": config.THREAD_ITEM_CHARS,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["thread_items", "notes"],
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
                "json_schema": {"name": "threads_plan", "strict": True, "schema": schema},
            },
        },
        timeout=180,
    )
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    if msg.get("refusal"):
        raise RuntimeError(f"Модель отказалась: {msg['refusal']}")
    return ThreadPlanOut.model_validate_json(msg["content"])


def threadify_post(text: str) -> ThreadPlanOut:
    """Create a Threads-native sequence without changing the master post."""
    text = text.strip()
    if not text:
        raise RuntimeError("Пустой текст нельзя разбить для Threads")
    if config.llm_provider() == "openai":
        out = _openai_thread_parse(prompts.THREAD_SYSTEM, prompts.thread_message(text))
    else:
        out = _anthropic_thread_parse(prompts.THREAD_SYSTEM, prompts.thread_message(text))
    return ThreadPlanOut(
        thread_items=_validate_thread_items(out.thread_items),
        notes=out.notes.strip(),
    )


def translate_post(
    source: str,
    date: str,
    text: str,
    max_chars: int = config.PLATFORM_SAFE_CHARS,
) -> DraftOut:
    """Translate/correct the full post; compress only above the requested hard limit."""
    out = _parse(
        prompts.translation_system(max_chars),
        prompts.user_message(source, date, text),
        max_chars,
    )
    return _draft(out.full_text, out.notes, out.thread_items, max_chars=max_chars)


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
        prompts.revise_system(config.PLATFORM_SAFE_CHARS),
        prompts.revise_message(current_text, instruction),
        config.PLATFORM_SAFE_CHARS,
    )
    return _draft(
        out.full_text,
        out.notes,
        out.thread_items,
        max_chars=config.PLATFORM_SAFE_CHARS,
    )


def compress_post(current_text: str, target_chars: int) -> DraftOut:
    """Compress in the current language without translating or changing facts."""
    current_text = current_text.strip()
    if not current_text:
        raise RuntimeError("Пустой текст нельзя сжать")
    if target_chars not in {config.MAX_POST_CHARS, config.PLATFORM_SAFE_CHARS}:
        raise ValueError("Неподдерживаемый лимит сжатия")
    out = _parse(
        prompts.compression_system(target_chars),
        prompts.compression_message(current_text),
        target_chars,
    )
    return _draft(
        out.full_text,
        out.notes,
        out.thread_items,
        max_chars=target_chars,
    )


def adapt(edited_text: str) -> DraftOut:
    """Validate an authoritative owner master; Threads is generated separately."""
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
        thread_items=[],
    )
