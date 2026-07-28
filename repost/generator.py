import mimetypes
import re
from pathlib import Path

import httpx
from pydantic import BaseModel

from . import config, prompts


class DraftOut(BaseModel):
    linkedin_text: str
    x_text: str
    threads_text: str
    notes: str


class SummaryOut(BaseModel):
    summary: str


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


def _normalize_summary(text: str) -> str:
    """Keep a voice summary within the promised 2–4 sentence envelope."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    if not sentences:
        raise RuntimeError("Модель вернула пустое краткое содержание")
    if len(sentences) > 4:
        sentences = sentences[:4]
    if len(sentences) == 1:
        only = sentences[0].rstrip(".!?")
        split_at = None
        for match in re.finditer(r"[,;:—]\s+", only):
            if len(only) // 3 <= match.start() <= len(only) * 2 // 3:
                split_at = match.end()
                break
        if split_at is None:
            words = only.split()
            if len(words) >= 8:
                split_at = len(" ".join(words[: len(words) // 2])) + 1
        if split_at is not None:
            first = only[:split_at].rstrip(" ,;:—")
            second = only[split_at:].lstrip(" ,;:—")
            if first and second:
                sentences = [first + ".", second[:1].upper() + second[1:] + "."]
    return " ".join(sentences)


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


def transcribe(audio_path: str | Path) -> str:
    """Transcribe a bounded audio file through the OpenAI Audio API."""
    if not config.OPENAI_API_KEY:
        raise RuntimeError("Для расшифровки нужен OPENAI_API_KEY")
    path = Path(audio_path)
    if path.stat().st_size > config.TRANSCRIPTION_MAX_BYTES:
        raise RuntimeError(
            f"Файл больше лимита расшифровки ({config.TRANSCRIPTION_MAX_BYTES // 1_000_000} МБ)"
        )
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    models = [config.TRANSCRIPTION_MODEL]
    if config.TRANSCRIPTION_MODEL == "gpt-transcribe":
        models.append("gpt-4o-mini-transcribe")
    response = None
    for index, model in enumerate(models):
        with path.open("rb") as fh:
            response = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                data={"model": model},
                files={"file": (path.name, fh, mime)},
                timeout=300,
            )
        if response.status_code < 400 or index == len(models) - 1:
            break
        try:
            error = response.json().get("error") or {}
            message = f"{error.get('code', '')} {error.get('message', '')}".lower()
        except ValueError:
            message = ""
        if "model" not in message:
            break
    assert response is not None
    response.raise_for_status()
    text = (response.json().get("text") or "").strip()
    if not text:
        raise RuntimeError("Сервис расшифровки вернул пустой текст")
    return text


def summarize_transcript(transcript: str) -> str:
    """Return a short Russian description so the user can decide whether to listen."""
    system = (
        "Кратко объясни по-русски, о чём эта расшифровка голосового сообщения. "
        "Ровно 2–4 предложения, только факты из расшифровки, без советов и выдумок."
    )
    user = transcript[:30_000]
    if config.llm_provider() == "openai":
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        }
        response = httpx.post(
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
                    "json_schema": {"name": "voice_summary", "strict": True, "schema": schema},
                },
            },
            timeout=180,
        )
        response.raise_for_status()
        msg = response.json()["choices"][0]["message"]
        return _normalize_summary(SummaryOut.model_validate_json(msg["content"]).summary)

    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic()
    response = _anthropic_client.messages.parse(
        model=config.ANTHROPIC_MODEL,
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=SummaryOut,
    )
    if response.parsed_output is None:
        raise RuntimeError("Модель не вернула краткое содержание")
    return _normalize_summary(response.parsed_output.summary)
