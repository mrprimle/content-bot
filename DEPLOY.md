# Деплой на VPS

В production работает один постоянный процесс `python -m repost.bot`. Он:

- формирует общий пул: по одному старейшему необработанному материалу каждого
  источника, с сортировкой от старых дат к новым;
- в 10:00 и 18:00 `Europe/London` выдаёт по два материала — четыре базовых в
  день; пропуск сразу выдаёт замену сверх этой четвёрки;
- хранит очередь и состояние кнопок в SQLite;
- проверяет дату квартального Telethon-сбора;
- повторяет полный сбор последних трёх календарных месяцев раз в три месяца.

Отдельный системный cron не нужен.

## Сервер

Подойдёт Ubuntu 24.04, 1 vCPU / 2 GB RAM:

```bash
apt update
apt install -y docker.io docker-compose-v2 git sqlite3
systemctl enable --now docker
git clone <URL_РЕПОЗИТОРИЯ> /opt/repost
cd /opt/repost
install -d -m 700 data
cp .env.example .env
nano .env
chmod 600 .env
```

Для сервера нужны:

- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`;
- `BOT_TOKEN`, `OWNER_CHAT_ID`;
- `OPENAI_API_KEY`;
- `BUFFER_ACCESS_TOKEN`, `BUFFER_CHANNELS`;
- `AUTHOR_FACTS`;
- `AUTO_SYNC=true` после успешного canary.

Compose уже задаёт:

```text
DB_PATH=/data/repost.db
TELEGRAM_SESSION=/data/repost.session
```

Поэтому база и пользовательская Telegram-сессия переживают пересборку
контейнера.

## Telethon-сессия

Безопаснее авторизовать рабочий Telegram-аккаунт прямо на сервере:

```bash
docker compose run --rm --build bot python -m repost.ingest login
```

Либо перенести уже созданную локальную сессию:

```bash
scp ~/Desktop/tg/repost.session root@IP_СЕРВЕРА:/opt/repost/data/repost.session
```

`repost.session` даёт доступ к Telegram-аккаунту. Права на сервере:

```bash
chmod 700 /opt/repost/data
chmod 600 /opt/repost/data/repost.session
```

## Остановка старого локального запуска

Если бот переносится с Mac на VPS, сначала останови на Mac оба старых
launchd-задания. Иначе два процесса с одним `BOT_TOKEN` будут конфликтовать, а
старый sync продолжит обращаться к Telegram:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.repost.bot.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.repost.sync.plist
```

Изменение plist на `Disabled=true` не останавливает уже загруженное задание.

## Canary и первый запуск

Сначала поставь `AUTO_SYNC=false` в `.env` и загрузи один материал из одного
источника в рабочую базу. Вместо `@channel` выбери один username из
`sources.txt`, чтобы canary не добавил лишний источник:

```bash
docker compose run --rm --build bot \
  python -m repost.ingest backfill --days 7 --sources @channel --limit 1
docker compose up -d --build
```

В Telegram вызови `/test @channel`. Команда показывает сырой материал без LLM;
LLM вызывается только после кнопки «Создать пост».

Если canary прошёл, останови бота, собери полное окно, верни `AUTO_SYNC=true` в
`.env` и запусти production:

```bash
docker compose down
docker compose run --rm --build bot python -m repost.ingest backfill --months 3
sed -i 's/^AUTO_SYNC=.*/AUTO_SYNC=true/' .env
docker compose up -d --build
docker compose logs --tail 100
```

Telegram может выдать `FloodWait` или ограничить аккаунт без отдельного
предварительного предупреждения. Canary и отдельный рабочий аккаунт снижают
последствия ошибки конфигурации, но не гарантируют отсутствие ограничений.

После успешного полного сбора бот сохранит `next_full_sync_at` в базе.
При перезапуске дата не теряется, и один и тот же квартальный запуск не
планируется заново.

## Проверка интерфейса

В Telegram:

```text
/test @channel
/stats
```

`/test` только показывает один материал и не обращается к LLM. LLM вызывается
после кнопки «Создать пост», а Buffer — только после «Опубликовать».

Перед передачей проверь offline workflow:

```bash
docker compose run --rm bot python scripts/smoke_test.py
docker compose run --rm bot python scripts/workflow_test.py
docker compose run --rm bot python scripts/scheduler_test.py
docker compose run --rm bot python scripts/publisher_test.py
```

## Обслуживание

```bash
docker compose logs --tail 100
docker compose restart
git pull && docker compose up -d --build
docker compose exec bot python -m repost.ingest status
sqlite3 data/repost.db ".backup 'data/backup.db'"
```

Не запускай `repost.ingest` одновременно в двух контейнерах: одна Telethon
session не рассчитана на параллельную запись из нескольких процессов.
