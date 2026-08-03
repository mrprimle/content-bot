# Vahue Content Bot

Telegram-first editorial pipeline for turning a curated archive of Russian-language
founder content into reviewed English posts for LinkedIn, X, and Threads.

The important product decision is that **AI is not used while browsing ideas**.
The owner first sees the original Telegram material and can reject as many items as
needed for free. `gpt-5.6-terra` is called only after the owner chooses an idea.

```text
Founders Pack channels
        │
        │ one-time 3-month backfill + durable per-channel pointer
        ▼
Telethon user session ───────────────┐
                                     ▼
                              PostgreSQL / Neon
                         sources, posts, queue, drafts,
                         publications, idempotency state
                                     │
                      10:00 and 18:00 Europe/London
                                     ▼
                           Telegram bot review UI
                   ┌─────────────────┴─────────────────┐
             Пропустить                         Создать пост
                   │                                  │
          next raw candidate                 gpt-5.6-terra
          in the same iteration        translate + factual correction
                                           + compress to ≤1500 chars
                                                      │
                                   ┌──────────────────┼──────────────────┐
                             Редактировать      Опубликовать    Закончить итерацию
                                                      │
                                                      ▼
                                                   Buffer
                                      LinkedIn · X Premium · Threads
```

## What the production bot does

1. A dedicated Telegram user account reads the 49 public channels listed in
   [`sources.txt`](sources.txt). The list is the current snapshot of the Founders
   Pack: <https://t.me/addlist/efNe-fsXVp1lYWZi>.
2. The initial import stores the last three **calendar** months of messages in the
   database. Text and media metadata are stored; media itself is fetched only when
   it is about to be shown.
3. At 10:00 and 18:00 `Europe/London`, Vercel Cron starts one review iteration.
4. The bot sends one original Russian material with two buttons:
   `✨ Создать пост` and `⏭ Пропустить`.
5. `Пропустить` permanently closes that candidate and immediately sends the next
   one. The owner can keep skipping inside the same iteration without an AI call.
6. `Создать пост` calls Terra once. It translates the post into natural English,
   corrects only author/company facts using `AUTHOR_FACTS`, and compresses only when
   necessary to stay within 1500 Unicode characters.
7. The ready draft has three actions:
   `✅ Опубликовать`, `✏️ Редактировать`, and `⏹ Закончить итерацию`.
8. Editing means replying with the complete replacement text. It is authoritative
   and is **not** sent through AI again. The 1500-character AI target no longer
   applies; manual edits may use up to 3000 characters so LinkedIn remains valid.
9. Publishing sends the draft through Buffer to LinkedIn, X, and Threads. Successful
   platforms are recorded individually, so a retry targets only failed platforms.
10. Publishing or `Закончить итерацию` ends the current chain. The next automatic
    candidate arrives at the next scheduled slot.

## Queue semantics

The queue is not a simple global chronological scan. It implements a persistent,
fair round-robin across sources:

1. From every active source, select its oldest `new` material.
2. Put those candidates into a durable pool.
3. Order the pool globally by `posted_at`, Telegram message id, and database id.
4. Deliver from that pool until it is exhausted.
5. Build the next pool from the next-oldest material of every source.

This prevents a prolific channel from consuming the entire queue while preserving
oldest-to-newest ordering inside every source and as much global chronology as the
fairness rule permits. The pool survives process restarts and Vercel invocations.

Every scheduled or manual delivery has a unique `slot_key`. Repeating the same cron
request cannot reserve a second candidate. A skipped raw post gets its own
`replacement:<post_id>` slot, so double-clicking an old Telegram button cannot emit
another replacement.

### Deduplication

- `(source_id, tg_message_id)` is unique, making backfill and incremental sync
  idempotent.
- Non-empty text is normalized and hashed. The same text reposted across several
  channels is stored once.
- Empty media posts use a source/message-specific hash, so unrelated audio or video
  messages are never collapsed together.
- Terminal rows are kept as tombstones. Physical cleanup is deliberately disabled
  because old Telegram callback ids and delivery idempotency depend on stable ids.

## Telegram interaction state machine

```text
post:new
   │ candidate-pool claim
   ▼
post:queued ── delivery lease ──► post:offered
                                      │
                 ┌────────────────────┴────────────────────┐
                 │                                         │
              drop                                      make
                 │                                         │
         post:skipped                           post:generating
         + replacement                                  │
                                                       draft
                                                         │
                                              post:drafted
                                              draft:awaiting_review
                                                         │
                     ┌───────────────────────────────────┼─────────────────┐
                     │                                   │                 │
                  edit                               publish       finish iteration
                     │                                   │                 │
          same draft, new text              per-platform result    post/draft:skipped
                                                         │
                                   ┌─────────────────────┴──────────────────┐
                                   │                                        │
                              all success                       partial/unknown result
                                   │                                        │
                       post/draft:published        retry failed only / manual Buffer check
```

Database transitions use compare-and-set updates. Concurrent callbacks cannot run
the same generation or publication twice. A Buffer timeout is marked
`publish_unknown` and is never retried automatically because Buffer may already have
accepted the post.

## AI contract

The prompt lives in [`repost/prompts.py`](repost/prompts.py), and the API client is
in [`repost/generator.py`](repost/generator.py).

Production configuration:

```text
LLM_PROVIDER=openai
OPENAI_MODEL=gpt-5.6-terra
MAX_POST_CHARS=1500
```

Terra must:

- translate the complete post rather than summarize it;
- preserve first-person voice, structure, reasoning, examples, numbers, jokes,
  irony, profanity, and the ending;
- keep a natural translation unchanged when it already fits 1500 characters;
- when necessary, compress by removing repetition and secondary explanation before
  removing concrete examples or punchlines;
- treat the input as Mike Doroshenko's own post whose personal/company facts may be
  wrong or outdated;
- use `AUTHOR_FACTS` as the only source of truth for Mike/Vahue corrections;
- preserve book titles, public people, third-party companies, products,
  methodologies, quotations, and third-party biographies or transactions;
- return strict JSON with `full_text` and a short Russian `notes` field describing
  factual corrections that need review.

The response schema also enforces the 1500-character limit. If the model still
returns a longer string, generation fails visibly instead of silently truncating it.

Current author facts:

```text
Name: Mike Doroshenko
Gender: man
Company: Vahue
Lives in London
Currently building SMM automation
Current projects deploy AI agents for people and businesses
Previously worked at Meta in Applied AI
Vahue has more than 7 companies and 30 employees
Trained more than 50 people in AI
```

## Media behavior

Voice, audio, video, video notes, photos, and documents are supported as review
materials. They are **never transcribed and never sent to AI**.

For delivery, the Telethon user forwards the original media to the bot through a
short-lived correlation marker. The bot then shows it to the owner and removes the
staging messages. If forwarding fails, it tries a bounded just-in-time download;
if that also fails, the original Telegram link remains available.

Choosing `Создать пост` on voice/audio/video asks the owner to write the full post
manually. That text then enters the normal draft review screen without an AI call.

## Publishing behavior

[`repost/publisher.py`](repost/publisher.py) uses Buffer's GraphQL `createPost`
mutation with Bearer authentication.

| Platform | Result |
| --- | --- |
| LinkedIn | One post containing the full master text. |
| X | One long post when `X_PREMIUM=true`; no Buffer thread metadata. |
| Threads | The same text split at paragraph/sentence/word boundaries into chunks of at most 500 characters. |

`BUFFER_POST_MODE=shareNow` publishes immediately. `addToQueue` delegates timing to
the Buffer channel queue. The current production environment uses `shareNow`.

## Scheduling on Vercel

The product schedule is always:

```text
10:00 Europe/London
18:00 Europe/London
```

Vercel Cron schedules are UTC and do not automatically follow British daylight
saving time. [`vercel.json`](vercel.json) therefore contains four UTC triggers:
09:00, 10:00, 17:00, and 18:00. The FastAPI endpoint checks the actual London hour
and accepts only the matching two. In summer, 09:00/17:00 UTC run; in winter,
10:00/18:00 UTC run. The other two return a safe no-op.

## Initial backfill and incremental refetch

The first import is intentionally manual:

```bash
.venv/bin/python -m repost.ingest backfill --months 3
```

`source.last_message_id` stores the highest Telegram id observed for each channel.
When there are no queued or `new` posts left, the next delivery request acquires a
cross-process refetch lease, queries every active channel with
`min_id=last_message_id`, advances the pointers, and retries the same empty slot.
Thus posts created since the original import enter the queue only when the archive
is exhausted, exactly once.

`AUTO_SYNC=false` is the expected production setting for this workflow. The legacy
quarterly-sync job remains available for a polling deployment, but the Vercel
production path relies on refetch-on-exhaustion.

## Architecture and module map

| File | Responsibility |
| --- | --- |
| [`api/index.py`](api/index.py) | FastAPI service: health, Telegram webhook, cron delivery, webhook setup. |
| [`repost/bot.py`](repost/bot.py) | Telegram UI, callbacks, generation/edit/publish flow, recovery, local polling scheduler. |
| [`repost/db.py`](repost/db.py) | SQLite/PostgreSQL compatibility layer, schema, queue claims, states, locks, idempotency. |
| [`repost/ingest.py`](repost/ingest.py) | Telethon login, backfill, pointer-based sync, lazy media transfer. |
| [`repost/generator.py`](repost/generator.py) | OpenAI/Anthropic structured-output client and hard length validation. |
| [`repost/prompts.py`](repost/prompts.py) | Translation, factual-correction, and compression contract. |
| [`repost/publisher.py`](repost/publisher.py) | Buffer channel discovery, platform payloads, publication result classification. |
| [`scripts/migrate_sqlite_to_postgres.py`](scripts/migrate_sqlite_to_postgres.py) | Lossless SQLite → PostgreSQL migration and row-count verification. |
| [`scripts/*_test.py`](scripts) | Offline regression coverage for database, workflow, scheduler, and publishing. |

### Database tables

| Table | Durable data |
| --- | --- |
| `source` | Telegram username, active flag, title, `last_message_id`, last sync time. |
| `post` | Original message, media metadata, queue status, Telegram UI ids. |
| `draft` | Model, platform texts, owner edit, notes, review/publication state. |
| `publication` | Per-platform success/error and Buffer external id. |
| `delivery_batch` | Unique scheduled/manual/replacement slot. |
| `delivery_item` | Reserved post, send lease token, bot message id, delivery timestamps. |
| `app_meta` | Refetch leases, recovery notices, and optional sync metadata. |

PostgreSQL advisory locks serialize Telethon session access and queue formation
across concurrent serverless invocations. SQLite uses file/process locks for local
operation.

## Configuration reference

Copy [`.env.example`](.env.example) to `.env` for local use. Never commit `.env`,
`.session`, database files, bot tokens, Buffer tokens, or API keys.

| Variable | Required | Meaning |
| --- | --- | --- |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | yes | Telethon application from `my.telegram.org/apps`. |
| `TELEGRAM_SESSION_STRING` | Vercel | Serialized dedicated user session. Local mode uses `repost.session`. |
| `BOT_TOKEN` | yes | Telegram Bot API token from BotFather. |
| `OWNER_CHAT_ID` | yes | Only this private chat may control the bot. |
| `DATABASE_URL` | Vercel | Neon/PostgreSQL connection. Without it, local SQLite is used. |
| `OPENAI_API_KEY` | yes | OpenAI API credential used only on `Создать пост`. |
| `OPENAI_MODEL` | yes | `gpt-5.6-terra` in production. |
| `LLM_PROVIDER` | yes | `openai`; Anthropic remains an optional fallback implementation. |
| `AUTHOR_FACTS` | yes | Verified Mike/Vahue facts available to the correction prompt. |
| `BUFFER_ACCESS_TOKEN` | yes | Buffer API token. |
| `BUFFER_CHANNELS` | yes | `linkedin:id,twitter:id,threads:id`. |
| `BUFFER_POST_MODE` | yes | `shareNow` or `addToQueue`. |
| `MAX_POST_CHARS` | yes | Master draft limit; clamped to a maximum of 1500. |
| `MANUAL_MAX_POST_CHARS` | yes | Owner-edited final text limit; 3000 for LinkedIn compatibility. |
| `X_PREMIUM` | yes | Allows one X post up to 25,000 characters; master limit still applies. |
| `POST_TIMES` | yes | London product slots, currently `10:00,18:00`. |
| `ITEMS_PER_SLOT` | yes | Initial cards per slot, currently `1`; skips can request replacements. |
| `TIMEZONE` | yes | `Europe/London`. |
| `WEBHOOK_SECRET` | Vercel | Validates Telegram's webhook secret header. |
| `CRON_SECRET` | Vercel | Bearer token for cron and webhook setup endpoints. |
| `PUBLIC_BASE_URL` | Vercel | Production origin, currently the Vercel project URL. |
| `AUTO_SYNC` | no | Keep `false` for archive-then-refetch production behavior. |
| `BOT_SEND_DELAY` | no | Per-chat pacing between Telegram sends. |
| `BOT_MEDIA_MAX_BYTES` | no | Maximum fallback Bot API upload size. |
| `MEDIA_STAGE_TIMEOUT` | no | Timeout for the server-side Telethon-to-bot handoff. |

Secrets are project-level **Production** environment variables in Vercel. They are
not copied into GitHub and are not exposed to client-side code. Preview deployments
intentionally do not receive Telegram/OpenAI/Buffer secrets, preventing a PR from
starting a second bot against production accounts.

## Local installation

Python 3.13 is used in production.

```bash
git clone https://github.com/mrprimle/content-bot.git
cd content-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Authorize the dedicated Telegram reader account once:

```bash
.venv/bin/python -m repost.ingest login
```

`repost.session` is equivalent to an active login and must remain private.

Run a safe canary before the full import:

```bash
.venv/bin/python -m repost.ingest backfill --days 7 --sources @channel --limit 1
.venv/bin/python -m repost.bot
```

In Telegram, run `/test @channel`. If delivery works, stop the bot, perform the
full import, and start it again:

```bash
.venv/bin/python -m repost.ingest backfill --months 3
.venv/bin/python -m repost.bot
```

Telegram may raise `FloodWait` during a large import. One source failure is reported
without discarding successful sources, and a failed full backfill exits non-zero.

### Bot commands

| Command | Effect |
| --- | --- |
| `/id` | Show the current private chat id and whether it matches `OWNER_CHAT_ID`. |
| `/test @channel` | Deliver one candidate from a specific source without calling AI. |
| `/next` | Start an additional manual iteration from the global pool. |
| `/stats` | Show queue/database counters. |
| `/resend <draft_id>` | Re-deliver a saved, unpublished draft after a Telegram/UI delivery failure. |

`/start` installs a persistent `✍️ Создать свой пост` button. It opens a durable
manual input session at any time, independently of the 10:00/18:00 source queue.
The submitted Russian, English, or mixed text goes through the same three-stage
Terra pipeline and returns as a normal reviewable draft. The draft's
`✏️ Редактировать без AI-лимита` button accepts a complete owner-written replacement
up to 3000 characters without calling or compressing through AI again.

## Production deployment

Production is:

- FastAPI on Vercel Functions in `lhr1`;
- Telegram webhook instead of polling;
- Vercel Cron for the two London review slots;
- Neon PostgreSQL for durable state;
- GitHub repository `mrprimle/content-bot`, with pushes to `main` automatically
  creating production deployments.

The detailed bootstrap and migration procedure is in [`DEPLOY.md`](DEPLOY.md).
After infrastructure already exists, the normal release flow is:

```bash
git status
git diff --check
git push origin main
vercel inspect https://content-bot-teal.vercel.app
curl -fsS https://content-bot-teal.vercel.app/api/health
```

Do not run local polling after the Telegram webhook is active. Telegram must have a
single update consumer for this bot token.

## Verification

All tests are offline: they use temporary SQLite databases and fake Telegram,
OpenAI, Telethon, and Buffer boundaries. They do not publish or consume tokens.

```bash
.venv/bin/python scripts/smoke_test.py
.venv/bin/python scripts/workflow_test.py
.venv/bin/python scripts/scheduler_test.py
.venv/bin/python scripts/publisher_test.py
.venv/bin/python -m compileall -q repost api scripts
plutil -lint scripts/com.repost.bot.plist scripts/com.repost.sync.plist
docker compose config --quiet
git diff --check
```

Coverage includes:

- fair candidate pools and round boundaries;
- two daily base iterations plus unlimited skip replacements;
- no LLM or Buffer calls before explicit owner actions;
- draft finish behavior without replacement;
- media staging without transcription;
- pointer-based refetch after queue exhaustion;
- duplicate suppression and authoritative source reconciliation;
- process-crash recovery and unknown-publication safety;
- X long-post and Threads splitting rules;
- disabled destructive cleanup.

## Operational checks

- `GET /api/health` confirms service, database type, queue counters, source count,
  oldest/newest material, and configured model.
- Vercel → Project → Logs shows webhook and cron execution.
- Vercel → Project → Settings → Environment Variables shows encrypted production
  configuration.
- OpenAI Platform → Usage → Responses and Chat Completions → Models shows
  `gpt-5_6-terra` token usage.
- Buffer should be checked manually before retrying a `publish_unknown` draft.

### User-visible progress and runtime logs

Long-running owner actions acknowledge themselves before work starts. Translation
shows that Terra is translating/checking/compressing, accepted edits show their
character count, and publishing lists the Buffer destinations. Validation and
handler failures always return a Telegram message; an invalid reply is never
silently ignored. If an edit exceeds the limit, the validation message becomes the
new ForceReply prompt, so the owner can shorten the text and continue the same
draft repeatedly.

Production logs contain only high-level ids, states, lengths, durations, and
per-platform results—never post bodies or credentials. Inspect them with:

```bash
vercel logs --environment production --since 1h --expand --no-branch
```

## Non-goals and intentional limitations

- Editing is full-text replacement, not an AI chat such as “remove paragraph two”.
- Media is not transcribed or summarized.
- Source membership is controlled by `sources.txt`; the Telegram chat-folder link is
  documentation, not dynamically parsed on every run.
- There is no destructive retention job. Durable tombstones prioritize idempotency
  and safety over database compaction.
- Preview deployments are not full bot environments because production secrets are
  intentionally unavailable there.
