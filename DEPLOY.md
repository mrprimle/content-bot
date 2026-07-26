# Деплой и передача заказчику

Приложение — один постоянный процесс (`python -m repost.bot`) плюс файл базы SQLite. Всё остальное — расписания, сбор постов, генерация, публикация — живёт внутри этого процесса.

## Вариант 1. VPS + Docker (рекомендуемый, ~5 €/мес)

Подходит для боевой эксплуатации: работает без твоего мака, переживает перезагрузки, логи ротируются.

### 1. Сервер

Любой VPS с Ubuntu 24.04: Hetzner CX22 (~4,5 €/мес), DigitalOcean, Contabo. Минимума 1 vCPU / 2 GB хватает с большим запасом.

```bash
ssh root@IP_СЕРВЕРА
apt update && apt install -y docker.io docker-compose-v2 git
```

### 2. Код

```bash
git clone <URL_РЕПОЗИТОРИЯ> /opt/repost && cd /opt/repost
```

### 3. Конфиг

```bash
cp .env.example .env
nano .env    # заполнить: BOT_TOKEN, OWNER_CHAT_ID, OPENAI_API_KEY, BUFFER_*
nano sources.txt   # список каналов: @username на строку
```

### 4. Перенос накопленной базы (если нужно сохранить историю)

С мака:

```bash
scp ~/Desktop/tg/repost.db root@IP_СЕРВЕРА:/opt/repost/data/repost.db
```

Если база не нужна — пропусти, соберётся заново первым синком.

### 5. Запуск

```bash
docker compose up -d --build
docker compose logs -f          # проверить, что стартовал
```

Всё. Контейнер поднимается сам после перезагрузки сервера и после падения.

### Обслуживание

```bash
docker compose logs --tail 100        # логи
docker compose restart                # перезапуск
git pull && docker compose up -d --build   # обновление кода
docker compose exec bot python -m repost.webingest --days 90   # разовый сбор истории
docker compose exec bot python -m repost.ingest status         # статистика
docker compose exec bot python -m repost.ingest cleanup        # чистка старых постов
```

Бэкап базы (раз в неделю достаточно):

```bash
cp /opt/repost/data/repost.db /opt/repost/data/backup-$(date +%F).db
```

## Вариант 2. Остаться на маке (только для теста)

Автозапуск через launchd — процесс поднимается при входе в систему и перезапускается при падении:

```bash
cp ~/Desktop/tg/scripts/com.repost.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.repost.bot.plist
```

Минус: мак засыпает — расписание пропускается, публикации не уходят. Для боевой работы не годится.

## Передача заказчику

Код универсален, вся привязка к человеку — в `.env`. Заказчику нужно подготовить:

| Что | Где взять |
|---|---|
| `BOT_TOKEN` | свой бот у @BotFather (`/newbot`, затем `/setprivacy` → Disable) |
| `OWNER_CHAT_ID` | написать своему боту `/id` |
| `OPENAI_API_KEY` | platform.openai.com → API keys |
| `BUFFER_ACCESS_TOKEN` | publish.buffer.com/settings/api |
| `BUFFER_CHANNELS` | `python -m repost.publisher --channels` после подключения соцсетей в Buffer |
| `AUTHOR_FACTS` | имя, компания, чем занимается — для обезличивания |
| `sources.txt` | его список Telegram-каналов |

Порядок: заказчик заводит доступы → вписываются в `.env` на сервере → `docker compose up -d` → первый сбор истории (`webingest --days 90`) → проверка через `/next` в боте.

Дальше система работает сама: 1-го числа собирает новые посты, три раза в день предлагает черновик, по кнопке публикует.

## Что стоит сделать перед боевым запуском

- **Ротировать OpenAI-ключ**, если он где-то засветился (чат, переписка): platform.openai.com → API keys → Revoke, создать новый, обновить `.env`.
- **Лимит трат в OpenAI**: platform.openai.com → Billing → Limits, поставить месячный потолок (реальный расход ~$1–3/мес).
- **Подключить Threads в Buffer** и дописать `threads:ID` в `BUFFER_CHANNELS` — сейчас работают LinkedIn и X.
- **Проверить `GENERATOR_MODE`**: `translate` — близкий перевод с обезличиванием, `rewrite` — пересказ идеи своими словами (безопаснее с точки зрения авторских прав, см. RESEARCH.md).
