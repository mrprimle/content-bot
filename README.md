# TG → EN Repost Pipeline

Пайплайн: посты из Telegram-каналов → SQLite → перевод/обезличивание (Claude) → согласование в Telegram-боте → публикация в LinkedIn / X / Threads через Buffer.

Архитектура и обоснование решений: [RESEARCH.md](RESEARCH.md).

```
sources.txt → ingest (Telethon) → repost.db → bot (3 раза в день предлагает черновик)
                                                 │ кнопки: Опубликовать / Заново / Пропустить
                                                 │ правка: reply на сообщение бота
                                                 └→ publisher (Buffer GraphQL) → LinkedIn / X / Threads
```

## Установка

```bash
cd ~/Desktop/tg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # и заполнить
```

## Что нужно заполнить в .env (по шагам)

1. **Telegram API** — зайди на https://my.telegram.org → «API development tools» → создай приложение → скопируй `api_id` и `api_hash` в `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`. Это делается один раз, под твоим аккаунтом.
2. **Авторизация Telethon** — в терминале:
   ```bash
   .venv/bin/python -m repost.ingest login
   ```
   Спросит телефон и код из Telegram. Создаст файл `repost.session` — это ключ к твоему аккаунту, никому не передавать, в git не коммитить (уже в .gitignore).
3. **Бот** — у @BotFather команда `/newbot` → токен в `BOT_TOKEN`. Потом напиши своему боту `/id` и впиши ответ в `OWNER_CHAT_ID`.
4. **Anthropic** — ключ с https://console.anthropic.com → `ANTHROPIC_API_KEY`.
5. **Buffer** — аккаунт на buffer.com, подключи LinkedIn, X и Threads (бесплатный план = ровно 3 канала). Токен: https://publish.buffer.com/settings/api → `BUFFER_ACCESS_TOKEN`. Потом:
   ```bash
   .venv/bin/python -m repost.publisher --channels
   ```
   и впиши id каналов в `BUFFER_CHANNELS` (формат `linkedin:id,twitter:id,threads:id`).
6. **AUTHOR_FACTS** — пару строк о себе: имя, компания, чем занимаешься. Генератор подставляет это при обезличивании.

## Использование

```bash
# 1) Тестовая выгрузка: 3 дня из пары каналов
.venv/bin/python -m repost.ingest backfill --days 3 --sources @channel1,@channel2

# 2) Если ок — полная выгрузка по sources.txt (заполни его: @username на строку)
.venv/bin/python -m repost.ingest backfill --days 90

# 3) Догрузка новых постов (гонять руками или по cron раз в несколько часов)
.venv/bin/python -m repost.ingest sync

# 4) Бот — постоянный процесс; предлагает черновики по POST_TIMES
.venv/bin/python -m repost.bot

# Статистика базы
.venv/bin/python -m repost.ingest status

# Смоук-тест без внешних сервисов
.venv/bin/python scripts/smoke_test.py
```

В боте: `/next` — предложить черновик прямо сейчас, `/stats` — статистика. Правка черновика — ответь (reply) на сообщение с черновиком своим текстом: он станет LinkedIn-версией, а X/Threads пересоберутся автоматически.

## Режимы генератора

- `GENERATOR_MODE=translate` (по умолчанию) — перевод «как есть» + обезличивание: чужие имена → «a friend of mine», чужие компании → твои из AUTHOR_FACTS или нейтральные. Сомнительные утверждения (чужая выручка, награды) не подменяются, а помечаются в notes.
- `GENERATOR_MODE=rewrite` — рекомендованный в RESEARCH.md безопасный режим: берётся идея поста и пишется новый текст, без переноса чужого личного опыта. Меньше рисков по авторским правам и «выдуманной биографии».

## Сбор из группы ботом (режим коллектора)

Бот, добавленный в группу (privacy mode выключен через @BotFather → /setprivacy → Disable), автоматически сохраняет в базу каждое новое текстовое сообщение: кто написал, когда, текст. Историю ДО добавления бота Bot API не отдаёт — её догружаем через `repost.ingest` (Telethon). Чтобы бот работал всегда, есть launchd-агент:

```bash
cp scripts/com.repost.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.repost.bot.plist   # автозапуск + перезапуск при падении
```

## Передача проекта другому человеку

Вся личная привязка живёт в `.env`. Чтобы систему использовал другой человек, меняются только:

- `BOT_TOKEN` — он создаёт своего бота у @BotFather (или передаёте этого);
- `OWNER_CHAT_ID` — его chat id (команда /id боту);
- `ANTHROPIC_API_KEY` — его ключ;
- `BUFFER_ACCESS_TOKEN` + `BUFFER_CHANNELS` — его Buffer с его LinkedIn/X/Threads;
- `AUTHOR_FACTS` — его имя/компания для обезличивания;
- `sources.txt` / группы, куда добавлен бот — его источники.

Код не меняется вообще.

## Заметки

- Buffer `mode=addToQueue` кладёт пост в очередь канала — время выхода определяется слотами очереди в настройках Buffer. Прямой «опубликовать сейчас» можно получить, поставив в Buffer частые слоты.
- Математика очереди (из RESEARCH.md): 25 источников дают ~7 новых постов в день, публикуется 3 — очередь будет расти. Кнопка «Пропустить» — твой фильтр; жми её чаще, чем «Опубликовать».
- Секреты (`.env`, `repost.session`, `repost.db`) не коммитить и не пересылать.
