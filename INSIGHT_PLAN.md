# Insight Agent — Plan & Source of Truth

> **Read this whole file before starting any step.** It is the only shared memory
> between the people and agents building the Insight Agent. If something here
> conflicts with your assumptions, this file wins; if it is wrong, fix it in the
> same commit and say so in the Step Log.

## Purpose

The Insight Agent collects post- and account-level metrics and comments from
social platforms (Instagram now, YouTube later), stores them cleanly and
reproducibly, and analyzes them to tell the creator what is working, for whom,
and what to try next. It is built as a **self-contained module** (`insight/`) so
it can later move into a separate SaaS repo unchanged.

### The 8 feature groups

1. **Data collection**: platform adapters pull publications, metrics at fixed checkpoints after publish, account metrics daily, and comments. Raw API responses are kept, with tokens removed.
2. **Normalization**: platform metric names are mapped to canonical metrics through a versioned metric dictionary. Snapshots are stored in long format with completeness flags.
3. **Performance analysis**: compare posts at equal ages (same checkpoint), find baselines and outliers, and track growth curves and account trends.
4. **Audience intelligence**: comment themes, questions, sentiment and repeat commenters (by hash only).
5. **Recommendations**: concrete next actions grounded in the computed facts. Wording may come from an LLM; the numbers may not.
6. **Reports & alerts**: periodic summaries, plus alerts for outliers, drops or collection failures.
7. **Hand-offs**: structured outputs other agents consume (e.g. topic and format suggestions for the content pipeline).
8. **Guardrails**: privacy, rate limits, no guessing, facts kept separate from interpretation, and audit trails via raw_responses.

## Locked build order

| # | Step | Status |
|---|------|--------|
| 1 | Foundation: schema, metric dictionary, adapter interface, checkpoints, fixtures, FakeAdapter | **done** |
| 2 | Instagram collector + snapshot scheduler | **done** |
| 3 | Basic metrics view | **done** |
| 4 | Performance analysis | next |
| 5 | Comments collector + audience intelligence | |
| 6 | Recommendations | |
| 7 | Full reports + alerts | |
| 8 | YouTube adapter | |
| 9 | Hand-offs to other agents | |

Do not reorder or merge steps without the owner's explicit approval.

## Module rules

- **Self-contained.** Everything lives in `insight/`. Never import `app`, `master_loop`, `agent_brain`, `flow_automator` or `assembly_line`; a test enforces this. Reading settings via `database.get_setting` is allowed.
- **Numbers are computed in code.** LLMs are used only for language (comment themes, recommendation wording) and never produce or alter a number.
- **Never guess missing data.** If a metric is absent, it is stored as `value = NULL` with a `missing_reason`, and the snapshot is marked `partial` or `unavailable`. Unknown platform metrics are reported as `unmapped`, not mapped by similarity. Missed checkpoints that can't be recovered are recorded as `unavailable`, not filled with later values.
- **Facts are kept separate from interpretation.** Store and present measured values separately from analysis or opinions about them.
- **Use "associated with", not "caused by".** We observe correlations, never causation.
- **No raw usernames.** Comment authors are stored only as `SHA-256(salt + "\0" + normalized_username)` using `INSIGHT_AUTHOR_SALT`. Hashing refuses to run without a salt.
- **Respect API rate limits.** Back off on throttling errors, keep calls to a minimum, and never hammer the API in retry loops.
- **Secrets only in `.env`.** Never write tokens to the DB, logs or raw_responses (`privacy.redact_secrets` strips them).
- **Schema stays Postgres-compatible.** No SQLite-only features. All timestamps are UTC (see `insight/timeutil.py`).

## Workflow rule

1. Read `INSIGHT_PLAN.md` before every step.
2. Each step is **one commit**, with **all tests passing** (old and new).
3. Append a short entry to the Step Log below covering what was built, files, tests and known limits.

## Architecture (as of Step 2)

```
insight/
  __init__.py
  timeutil.py        UTC helpers + UTCDateTime column type (UTC on write, UTC attached on read)
  models.py          SQLAlchemy 2.x models (7 tables)
  checkpoints.py     Checkpoint config + due/missed/unrecoverable logic + period_key rules
  dictionary.py      Load the metric dictionary seed; map platform -> canonical
  seeds/metric_dictionary.v1.json   Versioned metric dictionary
  privacy.py         hash_author(), redact_secrets()
  http_client.py     Reusable GraphClient with retry, backoff, 200-call cap, Meta usage header throttling
  probe.py           Live probe CLI (python -m insight.probe) validating dictionary names
  collect.py         Collector CLI (python -m insight.collect [--dry-run])
  queries.py         Pure read functions returning plain data / DataFrames (no Streamlit import)
  view.py            Streamlit metrics view (streamlit run insight/view.py)
  alembic.ini        Alembic configuration for insight migrations
  migrations/        Alembic migration environment and versioned scripts
    env.py
    versions/0001_baseline_schema.py
  db.py              make_engine(), upgrade_db(), init_db(), session_factory(); DB paths
  storage.py         Idempotent writers: accounts, publications, snapshots, comments, raw responses
  adapters/base.py   PlatformAdapter ABC + record dataclasses
  adapters/fake.py   FakeAdapter over fixture data (IG-shaped payloads)
  adapters/instagram.py  InstagramAdapter implementing PlatformAdapter over live API
  fixtures.py        Sample-data generator + loader (CLI)
scripts/
  install_collector_task.ps1    Registers Windows Task Scheduler task (every 30m)
  uninstall_collector_task.ps1  Unregisters Windows Task Scheduler task
```

### Data model

- `accounts`: (platform, platform_account_id) unique.
- `publications`: (platform, platform_post_id) unique. `content_id` is nullable and links to the content pipeline later; it has no FK because that data lives in another DB.
- `metric_definitions`: the dictionary, unique on (platform, canonical_name, applies_to). Columns include `scale` (unit conversion, e.g. ms to s), `status` (`active` or `todo`) and `seed_version`.
- `metric_snapshots`: one collection for one subject at one checkpoint.
  - `subject_type` ('publication' or 'account') and `subject_id` are NOT NULL. **The unique key is (subject_type, subject_id, checkpoint, period_key).** NULLs are distinct in unique constraints, so the nullable FKs can't serve as the key.
  - The FK columns `publication_id` and `account_id` are kept. Check constraints enforce exactly one of them and require it to match subject_id.
  - `period_key` depends on the checkpoint:
    - post checkpoints: the checkpoint label (`1h`…`28d`)
    - `daily` (account metrics): the UTC date `YYYY-MM-DD`
    - `adhoc`: the collected_at ISO timestamp
  - `completeness` is one of complete, partial, delayed or unavailable.
  - `time_since_publish_seconds` is the real age at collection (NULL for accounts).
- `metric_values`: long format, unique on (snapshot_id, canonical_metric). `value` is NULL and `missing_reason` is set when the metric was expected but absent.
- `raw_responses`: the original payload as JSON, with tokens stripped.
- `comments`: unique on (platform, platform_comment_id). `author_hash` is a 64-character hex string. `parent_comment_id` points to `comments.id`.

### Checkpoints and catch-up

The config lives in `insight/checkpoints.py`:

- **Checkpoints:** `1h, 24h, 48h, 7d, 28d` after publish for posts, `daily` for accounts, and `adhoc`.
- **Grace window:** 10% of the interval, with a minimum of 15 minutes.
- `evaluate_checkpoints(published_at, now, collected)` and `due_checkpoints(publication, now, collected)` return one of these states for each checkpoint:
  - `pending`: not due yet.
  - `due`: inside the grace window. Collect normally.
  - `missed`: past the grace window, but the next checkpoint isn't due yet. Collect now and pass `delayed=True`, which marks the snapshot `delayed`.
  - `unrecoverable`: a later checkpoint is already due, so a collection now would only duplicate the later value under an earlier label. Call `save_snapshot(..., result=None)`, which records an `unavailable` snapshot with no values.
  - `done`: already collected.

### Adapter contract

`PlatformAdapter` has five methods:
- `capabilities()`
- `list_publications(since)`
- `fetch_post_metrics(publication)`
- `fetch_account_metrics()`
- `fetch_comments(publication, since)`

Rules for every adapter:
- Adapters never write to the DB. They return `MetricResult` objects with `raw_payload` (tokens stripped), `values`, `missing` and `unmapped`.
- Adapters must call `build_result()` (or `MetricDictionary.map`) so that all mapping goes through the dictionary.
- Usernames must be hashed inside the adapter, so raw usernames never leave it.

### Known API facts to verify in Step 2

- **Watch-time units:** IG reel watch-time metrics are reported in milliseconds. The seed scales them by 0.001.
- **Followers:** `followers_count` is an IG User *field*, not an insights metric.
- **Profile visits:** `profile_views` needs `metric_type=total_value`.
- **Account metrics in general:** confirm the exact names and periods against the current Graph API version (`GRAPH_VERSION` in `.env`).

## How to run tests

Run from the repo root:

```
python -m unittest discover -s tests -v
```

The Insight tests are `tests/test_insight_storage.py` and `tests/test_insight_logic.py`. They use in-memory or temp SQLite, need no network and no `.env`.

## How to load sample data

```
python -m insight.fixtures                   # writes data/insight_sample.db
python -m insight.fixtures --path other.db   # custom location
python -m insight.fixtures --seed 7          # different (still deterministic) data
```

What the sample data contains:
- 30 reels over 60 days (3 viral, 2 flops) with snapshots at every checkpoint already reached.
- 60 daily account snapshots and about 110 comments, some of them replies.
- One `delayed` 24h snapshot, one `partial` 7d snapshot (avg watch time missing) and one `unavailable` 1h snapshot.

Safety and reuse:
- Loading is idempotent.
- It refuses to write to the real DB (`data/insight.db` or `INSIGHT_DB_URL`).
- From code: `fixtures.generate_dataset()` builds the in-memory dataset, and `FakeAdapter(dataset)` gives you an adapter for tests.

## How to run collector

```bash
# Run one collection pass against Instagram Graph API
python -m insight.collect

# Dry-run: make real API calls, verify auth & responses, write nothing to DB
python -m insight.collect --dry-run

# Custom per-run API call limit (default 200)
python -m insight.collect --call-cap 100
```

## How to install / uninstall Windows Scheduled Task

```powershell
# Register "StreamOvate Insight Collector" task to run every 30 minutes
powershell -ExecutionPolicy Bypass -File scripts/install_collector_task.ps1

# Remove the scheduled task
powershell -ExecutionPolicy Bypass -File scripts/uninstall_collector_task.ps1
```

## How to run the view

```bash
# Start the Streamlit metrics dashboard (read-only)
streamlit run insight/view.py
```

## Configuration

| Variable | Purpose |
|---|---|
| `INSIGHT_AUTHOR_SALT` | Required for hashing real comment authors. Shared secret, the same value for all developers, shared privately. Changing it breaks author matching. |
| `INSIGHT_DB_URL` | Optional. Overrides the real DB (default `sqlite:///data/insight.db`); use a Postgres URL later. |

## Step Log

### Step 1: Foundation (2026-10-02)

**Built**
- The self-contained `insight/` package: 8-table SQLAlchemy 2.x schema, metric dictionary seed v1 and loader, adapter ABC, checkpoint config with catch-up logic, author hashing, token redaction, idempotent storage writers, fixture generator and FakeAdapter. No real API calls.

**Schema decisions**
- Snapshot uniqueness uses NOT NULL `subject_type`/`subject_id` plus `period_key`.
- Account snapshots use the `daily` checkpoint keyed by UTC date.
- Every datetime is normalized to UTC on write and tagged UTC on read. Naive datetimes are rejected.

**Files**
- `insight/` (everything listed under Architecture), `tests/test_insight_storage.py`, `tests/test_insight_logic.py` and `INSIGHT_PLAN.md`.
- `requirements.txt` gains `SQLAlchemy>=2.0.31`.
- `.env.example` gains `INSIGHT_AUTHOR_SALT`.
- `.gitignore` gains an exception so the seeds are versioned.

**Tests**
49 new tests:
- schema creation and seed idempotency
- DB-level rejection of duplicate snapshots (including account snapshots with NULL publication_id), values, publications and comments
- check constraints
- `period_key` rules
- UTC round-trip and naive-datetime rejection
- mapping: scale, missing, null, non-numeric and unmapped metrics, YouTube TODO rows ignored
- checkpoint states, including PC-off catch-up
- hashing: deterministic, salt-dependent, refuses without a salt
- fixtures: shape, determinism, checkpoint coverage, special cases, monotonic growth, no usernames stored, idempotent reload, refusal of the real DB
- the import-isolation guard

The full suite (108 existing + 49 new) passes.

**Known limits**
- Instagram metric names in the seed come from documentation, not live calls. Verify them in Step 2.
- YouTube rows are `todo` placeholders.
- There is no migration tool yet (`create_all` only). Add Alembic before the first schema change on real data.
- Missed checkpoints are not backfilled from platform time series; they are marked `delayed` or `unavailable`.
- Fixture comment texts are templates, so they have no realistic language variety yet.

### Step 2: Live Probe + Instagram Collector & Scheduler (2026-10-02)

**Step 2a: Live API Probe**
- Built `insight/probe.py` (`python -m insight.probe`) to validate metric dictionary names against Instagram Graph API.
- Reusable `GraphClient` with retry on 5xx/timeouts (up to 3 times with exponential backoff 1s, 2s, 4s), 4xx no-retry, hard call cap (40 calls), and token redaction.
- Tested all 13 dictionary metrics against the live API (`@streamovate`):
  - Reel metrics (9): `views`, `reach`, `likes`, `comments`, `shares`, `saved`, `total_interactions`, `ig_reels_avg_watch_time`, `ig_reels_video_view_total_time` -> all 9 OK.
  - Account metrics (4): `followers_count` (User field), `profile_views`, `reach`, `views` (day / total_value) -> all 4 OK.
  - Zero values (`comments=0`, `saves=0`) correctly recognized as OK, not NO_DATA.
  - Verified no undiscovered metrics returned.
  - Raw JSON responses saved with tokens stripped to `data/probe/<ts>/`.

**Step 2b: Instagram Collector + Snapshot Scheduler**
- Alembic database migrations configured via `insight/alembic.ini` (`alembic -c insight/alembic.ini upgrade head`), keeping the module self-contained. Baseline schema (`0001_baseline_schema.py`) covers all 7 tables in `insight/models.py`.
- `upgrade_db()` in `insight/db.py` uses `insight/alembic.ini` and runs migrations automatically before collection and auto-stamps un-versioned databases.
- Shared `GraphClient` in `insight/http_client.py` with 200-call default cap and `X-App-Usage` / `X-Business-Use-Case-Usage` (>80%) throttling check.
- `InstagramAdapter` in `insight/adapters/instagram.py`:
  - `list_publications` with pagination cursor traversal.
  - `fetch_post_metrics` for Reels (`media_product_type == "REELS"`, `media_type == "VIDEO"`).
  - `fetch_account_metrics` with explicit UTC day since/until timestamps (followers excluded).
  - `fetch_account_followers` fetching current User profile followers count for account adhoc snapshot.
  - `fetch_comments` raises `NotImplementedError` (deferred to Step 5).
- Live lookback limit verified: Meta Graph API accepts up to 729 days back (2 years cutoff: 730 days returns `(#100) since param is not valid. Metrics data is available for the last 2 years`). Configured `ACCOUNT_INSIGHTS_MAX_LOOKBACK_DAYS = 729`.
- `insight/collect.py` (`python -m insight.collect [--dry-run]`):
  - Idempotent: re-running immediately collects nothing new.
  - Inter-process file lock (`data/insight.lock`) with 25-minute stale timeout.
  - Syncs publications, collects due/missed checkpoints (`1h`, `24h`, `48h`, `7d`, `28d`), marks unrecoverable checkpoints as `unavailable`, takes adhoc backfill snapshot for newly seen posts older than checkpoints.
  - Daily account catch-up rule: catches up missed daily snapshots from the later of (a) the account's `connected_at` date, (b) the earliest publication date, (c) today minus `ACCOUNT_INSIGHTS_MAX_LOOKBACK_DAYS` (729 days). Never more days per run than fit in the call cap; resumes automatically on the next run.
  - Records followers in account `adhoc` snapshot at most once per UTC day.
  - Logs summary line to `data/logs/collect.log` (rotating 5 MB).
- Windows Scheduled Task scripts:
  - `scripts/install_collector_task.ps1`: Registers 30-minute interactive task "StreamOvate Insight Collector".
  - `scripts/uninstall_collector_task.ps1`: Removes the scheduled task.

**Tests**
- 29 new tests across `tests/test_insight_probe.py` (18 tests) and `tests/test_insight_collector.py` (11 tests).
- Total suite: 186 tests passing cleanly.

### Step 3: Basic Metrics View (2026-10-03)

**Built**
- Pure read query module `insight/queries.py`:
  - No Streamlit import; returns plain data structures and dictionaries.
  - Formats timestamps in Asia/Kolkata (IST) 12-hour format, human-readable ages, and watch times in seconds (`.1f`s).
  - Missing metric values rendered as `"—"`, zero rendered as `"0"`.
  - Parsers for rotating collector log summary lines (last 20 runs) with error handling.
  - Overdue checkpoint detection using the existing `evaluate_checkpoints()` helper.
- Standalone Streamlit dashboard `insight/view.py` (`streamlit run insight/view.py`):
  - Strictly read-only connection via `make_readonly_engine()` (`mode=ro` URI). Never executes Alembic migrations or triggers collections.
  - Pre-flight checks: clean warning if database or publications are missing; warning banner if Alembic migration is not at head.
  - Concurrency safety: SQLite busy timeout set to 10s (`PRAGMA busy_timeout=10000`) across all engines in `insight/db.py`; short-lived read connections ensure background collector writes never fail with "database is locked".
  - Caching & Refresh: query wrappers use `@st.cache_data(ttl=60)`; includes manual "🔄 Refresh" button that invalidates cache and opens a fresh read connection.
  - Three tabs:
    - **Posts**: Summary row per publication with real age, caption, permalink, and per-checkpoint metric values (1h, 24h, 48h, 7d, 28d) with completeness markers (✅ complete, ⏰ delayed, ❌ unavailable, ⏳ not due yet) plus expander showing all metrics × snapshots with real age at collection.
    - **Account**: Daily metrics table and line chart (reach, views, profile visits; followers strictly excluded) + separate followers-over-time table and chart from adhoc snapshots.
    - **Data Health**: Collector run log table (last 20 runs) with warning if last run >90 min ago; snapshot completeness breakdown; overdue checkpoint alerts; raw response count and latest fetch time.

**Tests**
- 39 new tests in `tests/test_insight_view.py`:
  - Query function return shapes and values.
  - Read-only SQLite enforcement (write attempts fail with OperationalError).
  - Empty and missing DB handling.
  - Alembic version head check and mismatch detection.
  - IST 12-hour datetime and duration age formatting.
  - Missing values show `"—"` while zeroes show `"0"`.
  - Log summary parsing on real collector log line fixtures.
  - Overdue checkpoint detection using checkpoint evaluation logic.
  - Concurrency: read-only connection open during concurrent write does not block write.
  - Verification that daily account queries never return followers.
- Total suite: **227 tests passing cleanly** in 14.37s.

