# Posting Agent Build Log

## Plan (Build Prompt 1 of 5)
1. **Architecture & Contract Compliance**:
   - Implement `PostingAgent(BaseAgent)` with name `"posting"`.
   - Implement machine-wide publish lock (`agents/posting/lock.py`) in OS temp dir (`ig_agent_posting.lock`) holding `run_id`, `pid`, `host`, and 12-hour IST start time. Detect and fail fast on live PID, take over and log stale locks (dead PID), release in `finally`. Dry runs do not take the lock.
   - Enforce pre-publish gates: check human approval recorded for run (`approval.json` with `approved=True`) and QC report passed (`qc/report.json` with `passed=True`). If failed/missing, refuse and publish nothing.
   - Video resolution: `task.spec.video_path` in video mode; `sync_merge/output.json`'s `video_path` otherwise. Verify non-empty file.
   - Platform selection from `task.spec.platforms` (default `["instagram"]`).
   - Idempotency: skip platforms already marked `"published"` in existing `posting/result.json`. Retried/failed/unattempted platforms run. `dry_run` is never treated as published.
   - Instagram caption formatting: `platform_text.instagram.caption` with fallback to `caption`; case-insensitive hashtag appending without duplicates; pre-network validation of <=2,200 chars and <=30 hashtags.
   - Event logging: record gate checks, lock acquisition/release, and platform outcomes via `core.runfolder.append_event`.
2. **Platform Adapters**:
   - `InstagramAdapter(PublishAdapter)` (`agents/posting/platforms/instagram.py`): Graph API only, Instagram Login check (token starts with `IGAA...`), reuse `tunnel.py` and `ig_service.py` (import only), fetch permalink after publish, map typed errors to readable strings, always stop tunnel in `finally`.
   - `YouTubeAdapter(PublishAdapter)` (`agents/posting/platforms/youtube.py`): check Google OAuth configuration; if missing, return status `"skipped"` with `"not configured: Google OAuth not set up"` (non-failing stage); structure upload path with default `"private"` privacy.
3. **Dry Run & CLI**:
   - Support `dry_run` flag: runs gates, video checks, caption building, credential presence (names only), adapter checks without network/tunnel/lock. Record status `"dry_run"` with would-be caption/title noted.
   - CLI entrypoint (`agents/posting/__main__.py`): `python -m agents.posting --run <run_id> [--dry-run] [--platforms ...]`.
4. **Testing Suite** (`tests/agents/posting/`):
   - Comprehensive unit tests mocking `ig_service`, `tunnel`, and filesystem.
   - Cover approval missing/refused, QC missing/failed, caption fallbacks, hashtag rules & limits, existing-video mode, IG success & permalink retrieval/failure, IG error mapping, tunnel cleanup on failure, YouTube not configured -> skipped, partial failure -> stage failed, idempotency skipping published, dry-run safety & no lock/network, lock contention & stale takeover & exception release, ngrok session-limit error mapped, README schema conformity.
5. **Documentation & Verification**:
   - Update `agents/posting/README.md` with documented status values (`published`, `failed`, `skipped`, `dry_run`).
   - Run full test suite with `.venv/Scripts/python -m unittest discover -s tests -v`.
   - Document build outcomes, decisions, and core gaps in `BUILD_LOG.md`.

## What Was Built
- `agents/posting/agent.py`: `PostingAgent(BaseAgent)` with pre-publish gates (approval + QC), video resolution, platform dispatch, lock management, idempotency, partial failure tracking, exact `result.json` writing, and structured event logging.
- `agents/posting/lock.py`: Machine-wide publish lock file in OS temp dir (`ig_agent_posting.lock`) holding `run_id`, PID, host, and 12-hour IST start time. Detection of active vs stale locks with takeover, cleanup in `finally`, and mapping of cross-machine ngrok session-limit errors.
- `agents/posting/platforms/instagram.py`: `InstagramAdapter` implementing `core.platforms.base.PublishAdapter`, validating `META_ACCESS_TOKEN` starts with `IGAA`, verifying <= 2,200 chars and <= 30 hashtags, serving video over `tunnel.py` ngrok tunnel, publishing via `ig_service.py`, fetching permalink, mapping errors, and cleaning up tunnel in `finally`.
- `agents/posting/platforms/youtube.py`: `YouTubeAdapter` implementing `core.platforms.base.PublishAdapter`, checking Google OAuth configuration and returning non-failing `skipped` status when unconfigured, structuring the upload path with default `"private"` privacy.
- `agents/posting/platforms/__init__.py`: Package exports for Instagram and YouTube adapters.
- `agents/posting/__main__.py`: CLI runner (`python -m agents.posting --run <run_id> [--dry-run] [--platforms instagram,youtube]`) providing human-readable per-platform summary output.
- `tests/agents/posting/test_posting.py` & `tests/agents/posting/test_stub.py`: Comprehensive test suite containing 25 tests covering all 20 required scenarios, dry-run safety, and CLI execution.

## Decisions
1. **Publish Lock Design**:
   - Location: OS temp folder (`tempfile.gettempdir() / "ig_agent_posting.lock"`).
   - Contents: JSON containing `run_id`, `pid`, `host`, and `start_time` in 12-hour IST format (`%I:%M:%S %p IST, %d %b %Y`).
   - Active PID detection: Uses Windows ctypes `OpenProcess` with `PROCESS_QUERY_LIMITED_INFORMATION` / `GetExitCodeProcess` (and fallback to POSIX `os.kill(pid, 0)`).
   - Contention: Live PID fails fast with `"another publish is running (run <id>)"`.
   - Stale lock takeover: If PID is dead, takes over lock file, overwrites with current run info, and records event `posting_lock_stale_takeover`.
   - Cleanup: Guaranteed release in `finally`.
   - Cross-machine ngrok error mapping: Maps `ERR_NGROK_108`, agent session limits, or concurrency limits to `"shared ngrok account is in use — someone else is publishing"`.
   - Dry runs: Do not acquire or release the lock.
2. **Idempotency**:
   - Reads existing `posting/result.json` if present.
   - Any platform with status `"published"` (or legacy `"ok"`) is skipped and retained as-is in `posts[]`, never posted twice.
   - Only platforms with status `"failed"`, `"skipped"`, or unattempted are executed on re-run.
   - `dry_run` status is never treated as published.
3. **Caption Rules (Instagram)**:
   - Primary: `platform_text.instagram.caption`; fallback: `caption`.
   - Hashtag appending: Appends hashtags from `hashtags[]` that are not already present in the caption text (case-insensitive deduplication, normalizing leading `#`).
   - Limits: If final text > 2,200 characters or > 30 hashtags, platform fails immediately prior to any network or tunnel operations with clear error messages. Never silently trimmed.
4. **Dry Run**:
   - Runs full gates (approval + QC), video file verification, caption building and limit checks, credential presence checks (names only, no secrets logged), and adapter config checks.
   - Makes zero network calls, creates no tunnels, and does not take the publish lock.
   - Writes `posting/result.json` with status `"dry_run"`, noting would-be caption/title in `permalink` and `summary` while keeping `error` field `null`.
5. **YouTube Privacy Default**:
   - Defaults to `"private"`.
   - Overridable per run via `spec.extra.get("privacy")` or `spec.extra.get("youtube", {}).get("privacy")`.

## Test Count
- Total posting test suite: 25 unit tests in `tests/agents/posting/`.
- Full suite: 336 tests passing across `tests/` with 0 failures and 0 errors.

## Core Gaps
1. **RunSpec Schema for YouTube/Privacy Options**:
   - `RunSpec` in `core.contracts` lacks top-level fields for `privacy` (public/unlisted/private) or per-platform configuration dicts. Currently read via `task.spec.extra.get("privacy")`.
2. **PublishAdapter Method Signature**:
   - `core.platforms.base.PublishAdapter.publish(video_path: str, text: str, dry_run: bool = False)` only passes a single `text` parameter. Platforms like YouTube require separate `title` and `description` parameters. Handled via optional keyword arguments (`title`, `description`, `privacy`) in `YouTubeAdapter.publish()`.

## Step 0: Stub Setup
- Created package structure, contract documentation, and stub agent.
- Output specification defined for `posting/result.json`.
- Stub returns `status="failed"`, `error="not implemented"`.
