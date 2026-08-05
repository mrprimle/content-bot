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
                           21:00 Europe/London
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
                             Редактировать   Готово на завтра   Закончить на сегодня
                                                      │
                                            repeat until 3/3
                                                      │
                                next day 09:00 · 14:00 · 19:00
                                                      ▼
                                                   Buffer
                                      LinkedIn · X Premium · Threads
```

## What the production bot does

### Bot voice

Operational messages use a deliberately warm, caring co-author voice: gentle
encouragement, hearts, clear reassurance that durable state is safe, and occasional
playful references to Mike as Neo who has made the Matrix work for him. The tone
never modifies source material, generated drafts, publication text, error details,
or database state. The completed 3/3 planning session gets the strongest affirmation
and explicitly invites the owner to rest while the next-day automation takes over.

1. A dedicated Telegram user account reads the 49 public channels listed in
   [`sources.txt`](sources.txt). The list is the current snapshot of the Founders
   Pack: <https://t.me/addlist/efNe-fsXVp1lYWZi>.
2. The initial import stores the last three **calendar** months of messages in the
   database. Text and media metadata are stored; media itself is fetched only when
   it is about to be shown.
3. At 21:00 `Europe/London`, Vercel Cron starts one evening planning session for
   three posts that will publish the next day.
4. The bot starts iteration 1/3 and sends one original Russian material with two buttons:
   `✨ Создать пост` and `⏭ Пропустить`.
5. `Пропустить` permanently closes that candidate and immediately sends the next
   one. The owner can keep skipping inside the same iteration without an AI call.
6. `Создать пост` calls Terra once. It translates the post into natural English,
   corrects only author/company facts using `AUTHOR_FACTS`, and compresses only when
   necessary to stay within 1500 Unicode characters.
7. The ready planning draft has five actions: `✅ Готово на завтра`,
   `✏️ Редактировать руками`, `🤖 Редактировать с AI`, `⏭ Другой материал`,
   and `⏹ Закончить на сегодня`.
8. Editing means replying with the complete replacement master text. LinkedIn/X
   keep that text exactly and do not AI-compress it; manual edits may use up to
   3000 characters. Terra only rebuilds the separate Threads sequence.
9. `Готово на завтра` saves the draft durably in PostgreSQL and immediately starts
   the next iteration. If the selected Telegram source has a photo, this last step
   asks `С картинкой` or `Без картинки`; no media question appears for text-only
   sources. After 3/3, the evening session closes without publishing yet and sends
   a warm completion/Neo affirmation.
10. On the next day the three drafts publish automatically through Buffer at
    09:00, 14:00, and 19:00 London time. Successful platforms are recorded
    individually, so a retry targets only failed platforms. Every slot sends one
    concise Telegram result: either `✅ posted` with a short excerpt or a visible
    failure/unknown-state warning.
11. `Закончить на сегодня` stops the remaining evening iterations. Drafts already
    marked ready remain scheduled; unfinished slots are cancelled.

The persistent `📊 Статус` button shows an auditable content-pool partition
(`total = remaining + already sent to the owner`) and today's three-slot plan. Each
planned post is represented by its London publication time, state, and first
sentence. Missing drafts, failed/unknown slots, overdue ready posts, invalid times,
and inconsistent plan sizes are surfaced in a dedicated error section; the status
handler is read-only and reports database failures without changing state.

The persistent `✍️ Создать пост` action remains independent from the evening batch.
It opens two immediate modes:

- `📚 Накидывать из базы` starts one candidate iteration; the final action publishes
  immediately instead of scheduling for tomorrow.
- `✍️ Написать свой текст` stores the submitted text unchanged and without an AI
  call. The owner can publish it as-is, run `✨ Standard Transform`, edit manually,
  rebuild Threads with AI, edit with AI, or cancel.

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

Every planning iteration and manual delivery has a unique `slot_key`. Repeating the
same cron request cannot reserve a second candidate. A `planning_session` owns three
ordered `planning_slot` rows; replacements stay inside the same slot, so raw skips
cannot accidentally create a fourth scheduled post.

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
21:00 London
   │ create planning_session + slots 1/3, 2/3, 3/3
   ▼
planning_slot:selecting ──► post:offered
                                │
                    ┌───────────┴───────────┐
                    │                       │
                  drop                    make
                    │                       │
          same slot, next candidate   Terra pipeline
                                            │
                                   draft:awaiting_review
                                            │
                              edit ─────────┤
                                            │ готово на завтра
                                            ▼
                                  planning_slot:ready
                                            │
                                  start next iteration
                                            │
                              after 3/3: session:scheduled
                                            │
                         next day 09:00 · 14:00 · 19:00
                                            ▼
                                  Buffer per-platform publish
                                            │
                        published / retry-known-failure / unknown
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
  factual corrections that need review;
- return a separate ordered `thread_items` sequence: a truthful hook or question,
  one complete story/value point per card, and a final payoff/optional discussion
  question. Every card is at most 250 characters and never cuts a sentence merely
  to fill the limit.

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
manually. The LinkedIn/X master remains exactly owner-written; Terra only builds
the separate Threads sequence.

For photo sources, the bot stores Telegram's reusable Bot API `file_id` plus an
unguessable media capability token. On the final approval step the owner chooses
with or without the photo. A selected photo is exposed to Buffer through
`GET /api/media/{token}` on the Vercel origin; that endpoint streams the Telegram
file without revealing `BOT_TOKEN`. Buffer receives the stable public URL in
`assets: [{ image: { url } }]`, so LinkedIn, X, and Threads all get the same image.
For a multi-card Threads post the asset belongs to the first `thread` item because
Buffer treats that ordered array as the publication source of truth.
Buffer requires a direct HTTPS image smaller than 10 MB. The media token and choice
are durable, including across a restart between evening review and next-day publish.

## Publishing behavior

[`repost/publisher.py`](repost/publisher.py) uses Buffer's GraphQL `createPost`
mutation with Bearer authentication. Image posts add one `assets.image.url` to the
same per-platform mutation.

| Platform | Result |
| --- | --- |
| LinkedIn | One post containing the full master text. |
| X | One long post when `X_PREMIUM=true`; no Buffer thread metadata. |
| Threads | AI-authored ordered cards of at most 250 characters, sent exactly as previewed through `metadata.threads.thread`. The first card is repeated as top-level `text`, as Buffer requires. |

The bot shows `📄 LinkedIn / X` and a numbered `🧵 Threads preview` separately.
`🧵 Пересобрать Threads с AI` changes only the Threads sequence; it never changes
the LinkedIn/X master. Legacy/raw drafts without an AI plan fall back to conservative
sentence/paragraph splitting at 250 characters.

`BUFFER_POST_MODE=shareNow` publishes immediately. `addToQueue` delegates timing to
the Buffer channel queue. The current production environment uses `shareNow`.

## Scheduling on Vercel

The product schedule is always:

```text
21:00 Europe/London — prepare three drafts sequentially
next day 09:00 Europe/London — publish draft 1
next day 14:00 Europe/London — publish draft 2
next day 19:00 Europe/London — publish draft 3
```

Vercel Cron schedules are UTC and do not automatically follow British daylight
saving time. [`vercel.json`](vercel.json) therefore contains the summer and winter
UTC variants for each of the four London events. The FastAPI tick endpoint checks
the actual London hour; the matching trigger runs and its paired trigger returns a
safe no-op. The planning session and every publication slot are durable and
idempotent, so repeated cron delivery cannot create duplicate posts.

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
| [`api/index.py`](api/index.py) | FastAPI service: health, Telegram webhook, public tokenized media, cron delivery, webhook setup. |
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
| `post` | Original message, media metadata, reusable Telegram file id, media token, queue status, Telegram UI ids. |
| `draft` | Model, platform texts, owner edit, notes, optional-image choice, review/publication state. |
| `publication` | Per-platform success/error and Buffer external id. |
| `delivery_batch` | Unique scheduled/manual/replacement slot. |
| `planning_session` | One 21:00 review batch, target date, required count, and lifecycle. |
| `planning_slot` | Ordered draft, exact next-day UTC publish time, retry/unknown state. |
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
| `OPENAI_API_KEY` | yes | OpenAI API credential used on source transformation, Standard Transform, and AI edit. |
| `OPENAI_MODEL` | yes | `gpt-5.6-terra` in production. |
| `LLM_PROVIDER` | yes | `openai`; Anthropic remains an optional fallback implementation. |
| `AUTHOR_FACTS` | yes | Verified Mike/Vahue facts available to the correction prompt. |
| `BUFFER_ACCESS_TOKEN` | yes | Buffer API token. |
| `BUFFER_CHANNELS` | yes | `linkedin:id,twitter:id,threads:id`. |
| `BUFFER_POST_MODE` | yes | `shareNow` or `addToQueue`. |
| `MAX_POST_CHARS` | yes | Master draft limit; clamped to a maximum of 1500. |
| `THREAD_ITEM_CHARS` | yes | Target and hard validation limit for each Threads card; `250`. |
| `THREAD_MAX_ITEMS` | yes | Maximum ordered cards in one Threads thread; `10`. |
| `MANUAL_MAX_POST_CHARS` | yes | Owner-edited final text limit; 3000 for LinkedIn compatibility. |
| `X_PREMIUM` | yes | Allows one X post up to 25,000 characters; master limit still applies. |
| `PLANNING_TIME` | yes | Single London review trigger, currently `21:00`. |
| `PUBLISH_TIMES` | yes | Next-day London publication slots: `09:00,14:00,19:00`. |
| `DAILY_POSTS` | yes | Drafts required per evening session; must match the number of publish slots. |
| `ITEMS_PER_SLOT` | yes | Candidate cards shown at once, currently `1`; skips request replacements in the same planning slot. |
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

`/start` installs a persistent `✍️ Создать пост` button. `Накидывать из базы`
starts one immediate candidate flow. `Написать свой текст` opens a durable manual
input session independently of the 21:00 planning session. Russian, English, or
mixed input is first saved byte-for-byte with zero LLM calls. `Standard Transform`
then explicitly runs the three-stage pipeline: translate to English, correct only
Mike/Vahue facts, and semantically compress only when needed to fit 1500 characters.
`✏️ Редактировать руками` accepts a complete owner-written master up to 3000
characters without rewriting or compressing that master; Terra then rebuilds only
the Threads sequence. `🤖 Редактировать с AI` opens a durable Terra
instruction loop: each reply is applied to the current version and preserves its
current language unless the instruction requests translation.

Telegram's Bot API cannot prefill arbitrary text into the user's composer. The
manual-edit action therefore sends only `waiting for edited text:` as a ForceReply
prompt. The owner replies with the complete replacement text; the bot does not
repeat the current draft before input.

## Production deployment

Production is:

- FastAPI on Vercel Functions in `lhr1`;
- Telegram webhook instead of polling;
- Vercel Cron for one London review event and three next-day publication slots;
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
- one idempotent evening session with three sequential draft slots;
- three durable next-day publication slots plus unlimited in-slot replacements;
- no LLM or Buffer calls before explicit owner actions;
- on-demand database and raw custom-text flows independent from evening planning;
- explicit Standard Transform with zero AI calls before that button;
- draft finish behavior without replacement;
- media staging without transcription;
- optional photo publication through the stable tokenized Vercel media endpoint;
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

- Telegram cannot prefill arbitrary long text into the native composer; manual edit
  therefore uses a short ForceReply prompt and waits for the complete replacement.
- Media is not transcribed or summarized.
- Source membership is controlled by `sources.txt`; the Telegram chat-folder link is
  documentation, not dynamically parsed on every run.
- There is no destructive retention job. Durable tombstones prioritize idempotency
  and safety over database compaction.
- Preview deployments are not full bot environments because production secrets are
  intentionally unavailable there.
