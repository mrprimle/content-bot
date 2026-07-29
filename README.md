# Telegram → social publishing assistant

Пайплайн для отбора идей из Telegram-каналов и публикации коротких английских
постов через Buffer.

```text
34 источника → Telethon (последние 3 месяца) → SQLite
                                                │
                         10:00 и 18:00 Europe/London
                                                │
             пул: по 1 старейшему материалу из каждого источника
                                                │
                         по 2 материала за временной слот
                                                │
              Создать пост → LLM → Опубликовать / Редактировать / Пропустить
              Пропустить    → без вызова LLM → следующий материал из пула
```

Пул сохраняется между запусками и сортируется от старых дат к новым. В 10:00
приходят первые два материала, в 18:00 — следующие два: всего четыре базовых
материала в день. После пропуска бот сразу показывает замену, поэтому фактическое
число сообщений в день может быть больше четырёх. Когда текущий круг закончился,
бот формирует следующий — снова по одному старейшему необработанному материалу
каждого источника.

Для voice/video сохраняются метаданные, а само медиа передаётся в бот, когда
доходит до выдачи. Локальное скачивание используется только как запасной путь
или для расшифровки. Для voice есть отдельная кнопка «Расшифровать»;
распознавание и краткое содержание создаются по запросу, чтобы не тратить токены
на ненужные материалы.

## Установка

```bash
cd ~/Desktop/tg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Для локальной расшифровки Telegram voice также нужен `ffmpeg`
(`brew install ffmpeg` на macOS); Docker-образ уже устанавливает его сам.

Заполни `.env`:

- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` — приложение с
  [my.telegram.org/apps](https://my.telegram.org/apps);
- `BOT_TOKEN`, `OWNER_CHAT_ID` — бот-интерфейс и id владельца;
- `OPENAI_API_KEY` — генерация и опциональная расшифровка;
- `BUFFER_ACCESS_TOKEN`, `BUFFER_CHANNELS` — LinkedIn/X; Threads можно не добавлять;
- `AUTHOR_FACTS` — только подтверждённые сведения об авторе.

Авторизация пользовательской Telethon-сессии:

```bash
.venv/bin/python -m repost.ingest login
```

Файл `repost.session` равнозначен активному входу в аккаунт: он исключён из git,
его нельзя пересылать или публиковать.

## Сбор

Перед первым полным сбором сделай canary на одном источнике. Временно поставь
`AUTO_SYNC=false` в `.env`, чтобы запуск бота через 10 секунд не начал сбор всех
34 источников. Вместо `@channel` укажи один username уже из `sources.txt`:

```bash
.venv/bin/python -m repost.ingest backfill --days 7 --sources @channel --limit 1
.venv/bin/python -m repost.bot
```

В Telegram вызови `/test @channel`. После проверки останови бота, выполни полный
сбор ниже и верни `AUTO_SYNC=true`. Canary уменьшает нагрузку, но Telegram не
обязан заранее предупреждать о `FloodWait` или ограничении аккаунта.

Полный первоначальный сбор последних трёх календарных месяцев:

```bash
.venv/bin/python -m repost.ingest backfill --months 3
```

В production поставь `AUTO_SYNC=true`. Постоянный процесс проверяет сохранённую
дату следующего запуска и повторяет полный трёхмесячный сбор через три
календарных месяца. Повторная выгрузка идемпотентна по
`source + Telegram message id`.

## Бот

```bash
.venv/bin/python -m repost.bot
```

Команды:

- `/test @channel` — показать один материал из указанного источника;
- `/next` — вручную показать следующие два материала из общего пула;
- `/stats` — статистика очереди.

Сырой текст не отправляется в LLM до нажатия «Создать пост». Обычный текстовый
пост переводится на английский с обезличиванием и согласованными заменами — без
извлечения новой «идеи». Идея формируется только из расшифровки voice/audio.
Для voice/video кнопка «Создать пост» без расшифровки просит написать собственный
текст ответом. После генерации доступны только «Опубликовать», «Редактировать»
и «Пропустить»; повторной генерации нет. Любая финальная версия жёстко
ограничивается 250 символами.

## Локальный автозапуск

```bash
cp scripts/com.repost.bot.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.repost.bot.plist
```

Отдельный `com.repost.sync` не запускай: квартальное расписание уже находится
внутри постоянного процесса бота. Если старая версия sync-агента уже была
загружена, одного `Disabled=true` в обновлённом plist недостаточно — выгрузи её:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.repost.sync.plist
```

## Проверки

```bash
.venv/bin/python scripts/smoke_test.py
.venv/bin/python scripts/workflow_test.py
.venv/bin/python scripts/scheduler_test.py
.venv/bin/python scripts/publisher_test.py
.venv/bin/python -m compileall -q repost scripts
plutil -lint scripts/com.repost.bot.plist scripts/com.repost.sync.plist
docker compose config --quiet
```

Инструкция по VPS и переносу сессии: [DEPLOY.md](DEPLOY.md).
