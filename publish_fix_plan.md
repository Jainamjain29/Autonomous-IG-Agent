# Plan: Reliable Reel Publishing

Goal: publish ONE Reel to Instagram reliably.
Out of bounds: `flow_automator.py`, `assembly_line.py`, `agent_brain.py` are not modified.

Status: plan approved. Phase 1 committed; Phase 2 in progress. Each phase stops for review before the next begins.

## Decisions (approved 2026-09-23)

1. **Settings precedence:** `.env` wins over SQLite for `META_ACCESS_TOKEN`, `IG_USER_ID` and `GEMINI_API_KEY`. SQLite wins for every other key, with `.env` as the fallback. The source of each key (`.env`, `sqlite` or `unset`) is printed, never the value.
2. **Both out-of-scope bugs are fixed** in `master_loop.py`: `generate_subtitles` gets `srt_path`, and `generate_full_pipeline` creates `workspace/` with `os.makedirs` at the start. (Note: the `srt_path` change turned out to be cosmetic, because `generate_subtitles` already joins its argument onto `WORKSPACE`.)
3. **Git history is not rewritten** for `vc_redist.x64.exe`. `git rm` plus `*.exe` in `.gitignore` is enough.

## Later (not part of this plan)

- Set up a uv venv on Python 3.11 before running the full pipeline. Phase 1 was only checked with the system Python 3.13.

## What the docs say (checked 2026-09-23)

- **API version:** Graph API **v26.0** was released 2026-07-29. Meta's Instagram publishing examples still show v25.0. Default is `GRAPH_VERSION=v26.0`, which can be changed in `.env`.
- **Resumable upload flow:**
  1. `POST /{ig_user_id}/media` with `media_type=REELS`, `upload_type=resumable` and `caption` (no `video_url`). The response is `{id, uri}`, where `uri` is `https://rupload.facebook.com/ig-api-upload/{ver}/{id}`.
  2. `POST {uri}` with headers `Authorization: OAuth <token>`, `offset: 0` and `file_size: <bytes>`, and the raw file bytes as the body.
  3. `GET /{id}?fields=status_code,status`. Possible values: `IN_PROGRESS`, `FINISHED`, `ERROR`, `EXPIRED`, `PUBLISHED`. On `ERROR`, `status` holds the error subcode.
  4. `POST /{ig_user_id}/media_publish` with `creation_id`.
- **Watch out:** the docs only describe resumable upload for **Facebook Login (graph.facebook.com)**. With `GRAPH_HOST=graph.instagram.com`, the code prints a warning and still tries, using the `uri` Meta returns rather than building one. If Meta rejects it, `PUBLISH_MODE=ngrok` is the fallback.
- **Limits:**
  - Reels must be 3 s to 15 min, at most 300 MB, H.264/HEVC, 23–60 fps.
  - Unpublished containers expire after 24 h.
  - Accounts can publish at most 100 posts per rolling 24 h.
  - The file size is checked before uploading.
- **Publishing permissions:** `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`.

Sources:
- [Instagram content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/)
- [IG User Media reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/)
- [IG Container reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-container/)
- [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog/)

## Phase 1: setup

1. **`requirements.txt`:** list streamlit, requests, pyngrok, google-generativeai, openai-whisper, imageio-ffmpeg, edge-tts, playwright and python-dotenv. Keep `streamlit==1.32.0` pinned and add a `# TODO: pin grpcio / google-generativeai to known-working versions` line.
2. **`database.py`:**
   - Create `data/` with `os.makedirs(..., exist_ok=True)` and call `init_db()` when the module is imported.
   - `get_setting(key)` resolves each key per the precedence decision above, reading `.env` with `load_dotenv()`.
   - The fallback has to live here because `agent_brain.py`, which is out of bounds, reads `GEMINI_API_KEY` through `db.get_setting`.
   - The database and `.env` use different key names (`META_GRAPH_API_KEY` vs `META_ACCESS_TOKEN`, `IG_ACCOUNT_ID` vs `IG_USER_ID`). A small alias map lets either name work.
3. **`.env.example`:** add `META_ACCESS_TOKEN`, `IG_USER_ID`, `GEMINI_API_KEY`, `GRAPH_HOST=graph.facebook.com`, `GRAPH_VERSION=v26.0` and `PUBLISH_MODE=resumable`. `.env` is already in `.gitignore`.
4. **`vc_redist.x64.exe`:** `git rm` it and add `*.exe` to `.gitignore`. This does not remove the file from history: the first commit still holds all 25 MB. Rewriting history is destructive, so it is only done if explicitly requested.

**Decided:** `.env` wins for the three secrets, SQLite wins for everything else (see Decisions above).

## Phase 2: rewrite `ig_service.py`

- **Configuration and errors:**
  - Host and version come from `.env` or settings.
  - A typed exception, `IGPublishError(message, status_code, response_json)`, replaces the current `return None`.
- **Shared `_request()` helper:**
  - Always sets `timeout=30`.
  - On an HTTP error or an `error` field in the response, it logs the status code and full JSON, then raises `IGPublishError`.
  - The access token is removed from anything it logs.
- **`verify_credentials()`:**
  - Calls `GET /{ig_user_id}?fields=id,username`.
  - On graph.facebook.com it also calls `debug_token` and prints the username, token expiry and scopes.
  - Warns if `instagram_content_publish` is missing.
- **`publish_reel_local(path, caption)`:**
  - Creates the container, uploads to the returned `uri`, polls, then publishes.
  - A `publish=False` option stops after polling; the dry run uses it.
- **Polling:**
  - Checks every 10 s, for up to 10 min.
  - Reads `status_code,status`.
  - Logs the full response on `ERROR` or `EXPIRED`.
- **`publish_reel(video_url, caption)`:** the ngrok path, kept on the same helpers. A dispatcher chooses between the two paths based on `PUBLISH_MODE=ngrok|resumable` (default `resumable`).
- **`get_recent_analytics()`:** keeps the same behaviour but moves onto the new helper.

## Phase 3: publish flow

- **`master_loop.publish_approved_video()`:**
  - Raises `RuntimeError` if the state isn't `PENDING_REVIEW`. This is an explicit check rather than `assert`, because Python drops asserts when run with `-O`.
  - Then sets the state to `PUBLISHING`.
  - Starts ngrok and the HTTP server only in ngrok mode. The server is changed to keep its `httpd` handle so it can be shut down cleanly.
  - A `try/finally` always runs `ngrok.kill()` and `httpd.shutdown()`/`server_close()`.
  - On success the state becomes `IDLE`. On failure it becomes `FAILED` and the error message is stored in a `LAST_ERROR` setting in SQLite.
- **`app.py`:**
  - The publish button catches the error and reruns so the FAILED view shows.
  - A new `FAILED` view shows `LAST_ERROR` and has a Reset button, which clears the error and sets `IDLE`.
  - The `PUBLISHING` view gets a Reset button.
  - **Known limitation:** pressing Reset while a publish is really still running will be overwritten when that publish finishes. This is noted in a code comment rather than solved with locking.

## Phase 4: `scripts/publish_test.py`

- **Usage:** `python scripts/publish_test.py path/to/video.mp4 "caption" [--dry-run]`
- **Steps:**
  - Adds the repo root to `sys.path`.
  - Runs `verify_credentials()`, then `publish_reel_local(..., publish=not dry_run)`.
  - `--dry-run` verifies credentials, creates the container, uploads and polls, but does **not** call `media_publish`.
- **Results:**
  - Exits 0 on success.
  - Exits 1 on `IGPublishError` and prints the details.
  - Does not touch the Streamlit workflow state.
- **Dry-run side effect:** a dry run leaves an unpublished container behind. Meta expires it after 24 h, so it is harmless.

## Out of scope, noticed while reading

Both were approved and fixed in Phase 1 (see Decisions above).
