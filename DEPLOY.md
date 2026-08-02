# Production на Vercel + Neon

Production состоит из трёх частей:

- Vercel Python Function с FastAPI принимает Telegram webhook;
- Vercel Cron открывает две итерации в 10:00 и 18:00 `Europe/London`;
- Neon PostgreSQL хранит 49 источников, все материалы, очередь, черновики,
  публикации, idempotency-слоты и `last_message_id` каждого канала.

В `vercel.json` четыре ежедневных UTC-триггера покрывают переход London между
GMT и BST. Endpoint сверяет фактический лондонский час, а уникальный `slot_key`
не даёт одному слоту выполниться дважды.

## Переменные окружения

В production нужны:

```text
DATABASE_URL
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION_STRING
BOT_TOKEN
OWNER_CHAT_ID
OPENAI_API_KEY
OPENAI_MODEL=gpt-5.6-terra
LLM_PROVIDER=openai
BUFFER_ACCESS_TOKEN
BUFFER_CHANNELS
BUFFER_POST_MODE=shareNow
AUTHOR_FACTS
MAX_POST_CHARS=1500
X_PREMIUM=true
POST_TIMES=10:00,18:00
ITEMS_PER_SLOT=1
TIMEZONE=Europe/London
WEBHOOK_SECRET
CRON_SECRET
PUBLIC_BASE_URL
```

`TELEGRAM_SESSION_STRING`, bot token и API-ключи являются секретами. Их нельзя
добавлять в git или показывать в логах. `DATABASE_URL` создаётся интеграцией Neon.

## Создание инфраструктуры

```bash
vercel link --yes
vercel integration add neon --name content-bot-db --plan free_v3 \
  -m region=lhr1 -m auth=false \
  -e production -e preview -e development
```

После принятия Marketplace terms Vercel добавит `DATABASE_URL`. Остальные
переменные добавляются через `vercel env add`. `CRON_SECRET` Vercel автоматически
передаёт cron-запросам как `Authorization: Bearer ...`.

## Миграция локального состояния

Перед финальной миграцией останови локальный polling, чтобы SQLite больше не
менялась:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.repost.bot.plist
```

Затем перенеси и проверь все таблицы:

```bash
DATABASE_URL='postgresql://...' .venv/bin/python \
  scripts/migrate_sqlite_to_postgres.py --sqlite repost.db
```

Скрипт сохраняет первичные ключи, обновляет PostgreSQL sequences и сравнивает
количество строк в каждой таблице. Если миграция или deploy не прошли, сначала
верни локальный процесс; webhook не переключай.

## Deploy и переключение webhook

```bash
vercel --prod
curl -fsS "$PUBLIC_BASE_URL/api/health"
curl -fsS -X POST -H "Authorization: Bearer $CRON_SECRET" \
  "$PUBLIC_BASE_URL/api/setup-webhook"
```

После `setup-webhook` Telegram перестаёт отдавать updates локальному polling.
Проверь `/stats`, один безопасный `/next`, создание draft и Buffer-публикацию.

## Incremental refetch

Первоначальные три месяца уже находятся в PostgreSQL. Когда `status='new'` и
кандидатный пул заканчиваются, бот получает durable lease, блокирует формирование
нового пула и по каждому активному источнику вызывает Telegram с
`min_id=source.last_message_id`. Новые сообщения добавляются в базу, указатель
двигается только вперёд, после чего тот же пустой delivery slot перечитывается.

## Публикация

- один master-text до 1500 Unicode-символов создаётся через `gpt-5.6-terra`;
- LinkedIn получает его одним постом;
- X Premium получает его одним long post без Buffer thread metadata;
- Threads получает тот же текст, разбитый Buffer на сообщения до 500 символов.

## Проверки

```bash
.venv/bin/python scripts/smoke_test.py
.venv/bin/python scripts/workflow_test.py
.venv/bin/python scripts/scheduler_test.py
.venv/bin/python scripts/publisher_test.py
.venv/bin/python -m compileall -q repost api scripts
git diff --check
```
