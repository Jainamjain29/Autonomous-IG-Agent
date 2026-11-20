# QC Agent Build Log

## Step 0: Stub Setup
- Created package structure, contract documentation, and stub agent.
- Output specification defined for `qc/report.json`.
- Stub returns `status="failed"`, `error="not implemented"`.

## Step 1: Implementation Plan

### 1. Target Architecture & Files
- `agents/qc/settings.py`:
  - Centralized single settings file containing all QC thresholds and critic configuration.
  - Every threshold commented with source specification and date "checked 18 Nov 2026".
  - Video format:
    - Target aspect ratio: 9:16 (0.5625) with ±1% tolerance [0.556875, 0.568125].
    - Minimum resolution: 720x1280 (blocker); standard: 1080x1920 (warn if different).
    - Instagram video codecs: H.264, HEVC / H.265 (blocker).
    - Instagram frame rate: 23.0 to 60.0 fps (blocker).
    - Instagram container: moov atom at front of file (faststart) (blocker).
    - YouTube Shorts resolution: warn if above 1080p (>1080 width or >1920 height).
  - Duration limits:
    - Instagram duration: 3.0 s to 900.0 s (15 min) (blocker).
    - YouTube Shorts duration: max 180.0 s (3 min) (blocker).
    - Target duration range: 15.0 s to 90.0 s (warn if outside).
    - Sync duration drift: max 0.5 s difference between ffprobe duration and sync_merge duration_s (warn).
  - Audio standards:
    - Audio stream: must have at least 1 audio stream (blocker).
    - Audio volume: mean_volume must not be silent (threshold: -60.0 dB) (blocker).
    - Instagram audio codec: AAC (warn if not AAC).
  - File size limits:
    - Instagram file size: max 300 MB (314,572,800 bytes) (blocker).
  - Subtitles standards:
    - SRT file: must exist, be non-empty, and valid SRT format (blocker in normal mode; warn only in existing-video mode).
    - Cue coverage: subtitle cues must cover at least 80% (0.80) of video duration (warn).
  - Scene & Narration coverage:
    - Scene timings: every scene_id in plan.json must exist in scene_timings with start_s/end_s within [0, duration] (blocker; skipped in existing-video mode).
    - Scene continuity: no overlaps and no gaps > 1.0 s between consecutive scenes (warn; skipped in existing-video mode).
    - Narration vs SRT word coverage: normalized words of plan narration found in SRT text: blocker if < 0.60, warn if 0.60 <= coverage < 0.85, pass if >= 0.85 (skipped in existing-video mode).
  - Text & Metadata compliance:
    - Required metadata: caption/title must not be empty for any selected platform (blocker).
    - Instagram caption: max 2,200 characters (blocker).
    - Instagram hashtags: max 5 deduped hashtags (blocker); warn if fewer than 3 hashtags (warn).
    - Instagram hook: first line of caption max 125 characters (warn).
    - YouTube title: non-empty, max 100 characters (blocker).
    - YouTube description: max 5,000 characters (blocker).
    - Strictest limit wins when multiple platforms are selected.
  - Advisory AI Critic:
    - Off by default (`ENABLE_AI_CRITIC = False`).
    - Uses `core.llm.complete(agent="qc", ...)`.
    - Returns `{enabled, model, suggestions[], error}`.
    - Advisory only; never alters `passed` or check results.

- `agents/qc/probe.py`:
  - `probe_media(video_path)`: inspects video file using ffprobe if available, with robust fallback to ffmpeg (`imageio_ffmpeg.get_ffmpeg_exe()`) and direct MP4 box parser for faststart verification.
  - Returns structured `ProbeData` dataclass with width, height, aspect_ratio, fps, vcodec, acodec, duration, size_bytes, has_audio, mean_volume_db, is_faststart.

- `agents/qc/checks/`:
  - `video.py`: Pure check functions for video format, resolution, codecs, duration, audio, and file size.
  - `coverage.py`: Pure check functions for subtitles, scene coverage, and narration word coverage.
  - `text.py`: Pure check functions for platform text and hashtag compliance.

- `agents/qc/critic.py`:
  - Critic prompt builder and invocation via `core.llm.complete`.
  - Schema-validated output formatting for advisory feedback.

- `agents/qc/agent.py`:
  - `QCAgent(BaseAgent)` with `name = "qc"`.
  - Reads inputs (`content/plan.json`, `sync_merge/output.json` or `spec.video_path`, `spec.yaml`).
  - Runs all checks, aggregates results, evaluates `passed = not any(c.severity == "blocker" and not c.passed)`.
  - Writes `qc/report.json` matching contract specification.
  - Returns `AgentResult(status="ok" if passed else "failed", ...)`.

### 2. Test Plan
- Unit tests for pure check functions with boundary values (pass, warn, blocker fail).
- Real media tests with tiny generated MP4s and SRTs.
- Full agent tests with sample run folders:
  - Normal mode pass.
  - Blocker fail stops pipeline run before human review.
  - Warn-only still passes run.
  - Existing-video mode skips scene coverage and treats subtitles as warn-only.
  - Multi-platform selection: strictest limit wins.
  - AI critic: disabled, enabled with FakeLLM, and failure resilience.
  - Report JSON matches exact fields in `agents/qc/README.md`.


## Step 2: Implementation Details

### What Was Built
1. `agents/qc/settings.py`:
   - Single source of truth for all thresholds and critic configurations.
   - Every threshold contains a verification comment with date `"checked 18 Nov 2026"`.
   - All rules defined: 9:16 aspect ratio (±1% tolerance), 720x1280 min resolution, standard 1080x1920, H.264/HEVC codecs, 23-60 fps, faststart moov atom, Instagram duration (3s–15m), YouTube Shorts duration (<=180s), target duration (15–90s), sync drift (0.5s), silence threshold (-60.0 dB), Instagram AAC audio codec, 300 MB max file size, 80% subtitle cue coverage, scene timings bounds & continuity (gap <= 1.0s), narration word coverage (85% warn / 60% blocker), text length and hashtag limits (min 3, max 5, hook <= 125 chars, YouTube title <= 100, desc <= 5000).
   - Advisory AI Critic configuration (disabled by default, gemini-2.5-flash).

2. `agents/qc/probe.py`:
   - Media probe extracting resolution, aspect ratio, frame rate, video/audio codecs, duration, file size, faststart moov placement, and audio volume level via `ffmpeg volumedetect`.
   - Supports system `ffprobe` when present, with seamless fallback to `ffmpeg` via `imageio_ffmpeg` and native binary MP4 box parsing for faststart verification.

3. `agents/qc/checks/`:
   - `video.py`: Pure check functions for readability, aspect ratio, resolution boundaries, video/audio codecs, frame rate, faststart atom, durations, audio presence and silence, and file size.
   - `coverage.py`: Subtitle parser, SRT cue coverage calculation, scene timing bounds/overlaps/gaps, and narration vs SRT normalized word coverage.
   - `text.py`: Platform text presence, caption length, deduped hashtag counts, hook preview length, and YouTube title/description lengths.

4. `agents/qc/critic.py`:
   - Advisory critic executing `core.llm.complete(agent="qc", ...)` using JSON schema.
   - Off by default; returns structured suggestions or error string without impacting hard check results or pass status.

5. `agents/qc/agent.py`:
   - `QCAgent(BaseAgent)` with `name = "qc"`.
   - Evaluates inputs (`content/plan.json`, `sync_merge/output.json`, `task.spec.video_path`, `task.spec.platforms`).
   - Supports normal mode and existing-video mode (where scene timings and narration word coverage are skipped, and subtitles are treated as warn-only).
   - Writes `qc/report.json` with exact fields: `passed`, `checks`, and `ai_review`.
   - Returns `AgentResult(status="ok")` when all blockers pass (warnings do not fail the run); returns `AgentResult(status="failed")` when any blocker fails.

### Test Results
- Total QC unit & integration tests: 31 tests across 5 test modules in `tests/agents/qc/`.
  - `test_video_checks.py`: 6 tests covering aspect ratio boundaries, resolution, codecs, fps, duration, audio silence, and file size.
  - `test_coverage_checks.py`: 4 tests covering SRT parsing, cue coverage, scene timings validity, overlaps, gaps, and word coverage.
  - `test_text_checks.py`: 5 tests covering hashtag extraction/deduping, caption lengths, hook length, YouTube title/desc, and required text.
  - `test_probe_real_media.py`: 5 tests creating and probing real MP4 files (1080x1920 with sine audio, 16:9, no audio, silent audio, faststart vs non-faststart).
  - `test_qc_agent.py`: 10 tests covering full agent execution (normal pass, blocker fail, warn-only pass, existing-video mode, multi-platform strictest-wins, critic disabled, critic enabled with FakeLLM, critic failure resilience, pipeline halting on QC failure).
  - `test_stub.py`: 1 test verifying QC agent instantiation and name registration.
- Result: 31 passed in `tests/agents/qc/` (100% pass rate).

### Decisions & Core Contract Gap Analysis
- Stopping the pipeline before human review:
  - Evaluated `core/pipeline.py` contract:
    - If `result.status == "failed"`, `core/pipeline.py` updates stage status to failed and halts via `_mark_run_failed(...)` before reaching `PENDING_APPROVAL`.
    - In addition, `core/pipeline.py` specifically reads `qc/report.json` and halts if `report.get("passed")` is false.
    - Thus, `QCAgent` returning `status="failed"` and `passed=False` perfectly satisfies the pipeline contract to stop execution before human review.
  - Conclusion: No core contract gaps exist. Core was not modified.

## Prompt 2 — fixes

### What Changed
1. **Critic model selection delegated to core configuration**:
   - Removed `CRITIC_MODEL` from `agents/qc/settings.py`.
   - Updated `agents/qc/critic.py` so `core.llm.complete(agent="qc", messages=..., json_schema=..., run_id=...)` is invoked with NO `model=` argument, letting `config/agents.yaml` resolve primary and backup models.
   - Updated `ai_review.model`: populated from `resp.model` upon successful execution, and set to `""` when the critic is disabled or the call fails.
   - Added unit test `test_ai_critic_complete_called_without_model_kwarg` asserting `complete()` receives no `model` kwarg and `ai_review.model` comes from `resp.model`.

2. **Hashtag threshold comment correction**:
   - Updated the comment for `INSTAGRAM_MAX_HASHTAGS` in `agents/qc/settings.py` to state that 5 hashtags per post/Reel is a platform-enforced limit on Instagram since 18 Dec 2025 (not a creative guideline), retaining the `"checked 18 Nov 2026"` verification date.

3. **README contract specification alignment**:
   - Updated `agents/qc/README.md` to document the exact object shape of `ai_review`: `{"enabled": bool, "model": str, "suggestions": [{"area": str, "suggestion": str}], "error": str|null}`.
   - Clarified that `ai_review` is advisory only (never alters `passed` or check results) and is disabled by default (`settings.ENABLE_AI_CRITIC`).
   - Replaced example checks with real checks emitted by the agent: `aspect_ratio` (blocker) and `resolution_standard` (warn).
   - Added a "Severity & Decision Rules" section explaining blocker failure vs warning handling, and existing-video mode rules (coverage skipped with `"skipped – existing video mode"` detail; subtitles warn only).

4. **Report shape schema test**:
   - Added `test_report_json_exact_contract_shape` asserting `qc/report.json` top-level keys are exactly `{passed, checks, ai_review}`, check keys are `{name, passed, severity, detail}` with `severity in {"blocker", "warn"}`, and `ai_review` has `{enabled, model, suggestions, error}`.

### Test Results
- **Run (a) - Standalone QC Suite**:
  - Command: `PY -m unittest discover -s tests/agents/qc -t . -v`
  - Result: 33 tests passed (Ran 33 tests in ~40s, OK).
- **Run (b) - Full Test Suite**:
  - Command: `PY -m unittest discover -s tests -v`
  - Result: 312 tests passed (Ran 312 tests in ~46s, OK).

### Core & Cross-Agent Gaps
- **`tests/agents/__init__.py` discovery gap**:
  - `tests/agents/` previously lacked `__init__.py` on `main`, causing standard test discovery (`python -m unittest discover -s tests -v`) to skip subagent test directories.
  - Per instructions, this is a core repository item handled by the coordination chat on `main` and was not modified in this branch.

## Prompt 3 — real run

### 1. Sample Used
- Source: `workspace/final_reel.mp4`, `workspace/subs.srt`, and `workspace/current_video_plan.json` under `D:\Insta - AI Agent\IG_Agent`.
- Target copy: `runs/qc-real-20261120-2336/` (isolated, original untouched).
- Video metrics:
  - File name: `final_reel.mp4`
  - File size: 1,080,669 bytes (~1.08 MB)
  - Duration: 29.12 s
  - Video stream: H.264 (High), 1080x1920 (9:16 DAR), 25.0 fps, 224 kb/s
  - Audio stream: AAC (LC), 24,000 Hz, mono, 72 kb/s, mean_volume -20.9 dB
  - Box layout: `ftyp` (offset 0), `free` (offset 32), `mdat` (offset 40, size 1,061,852), `moov` (offset 1,061,892, size 18,777)
  - Faststart: `False` (moov atom placed after mdat atom)
- Accompaniments:
  - Subtitles: `subs.srt` (6 cues, 27.0s duration coverage = 92.7%)
  - Plan metadata: `current_video_plan.json` (Quantum Computing topic, caption with 4 hashtags)

### 2. Execution Results
- **Run A — Existing-video mode, platforms `[instagram, youtube]`, critic OFF**:
  - Script command: `PY agents/qc/scripts/real_run.py runs/qc-real-run-a --critic off`
  - Result: `passed = false`
  - Failing checks:
    - `[BLOCKER] video_faststart_instagram`: Moov atom not at front of file (missing faststart)
    - `[WARN] text_instagram_hook_length`: First line hook length 179 exceeds 125 characters
  - All other 23 checks passed (video readable, 9:16 aspect ratio, 1080x1920 resolution, H.264 codec, 25 fps, 29.12s duration, AAC audio, volume -20.9 dB, 1.08 MB size, 4 hashtags within 3-5 limit, valid YouTube title and description).

- **Run B — Same as A with critic ON**:
  - Script command: `PY agents/qc/scripts/real_run.py runs/qc-real-run-b --critic on`
  - Result: `passed = false`
  - Check results: identical to Run A (`video_faststart_instagram` blocker, `text_instagram_hook_length` warning).
  - Critic execution: live call via `core.llm.complete(agent="qc", ...)`. Primary model `gemini-2.5-flash` hit the free tier daily quota limit (20 req/day), triggering automatic fallback to backup model `gemini-2.5-flash-lite`. Backup model returned 404 (`models/gemini-2.5-flash-lite is no longer available to new users`).
  - Resilience: QC caught the LLM error gracefully (`ai_review.enabled = true`, `model = ""`, `suggestions = []`, `error` captured). `passed` status remained unaffected.
  - Usage log: Recorded in `data/core.db` (row id 18: `agent='qc'`, `provider='gemini'`, `model='gemini-2.5-flash-lite'`, `tokens=0`, `latency_ms=1945`, `success=0`, `backup_used=1`).

- **Run C — Normal mode with real SRT & split scene timings**:
  - Script command: `PY agents/qc/scripts/real_run.py runs/qc-real-run-c --critic off`
  - Inputs: `sync_merge/output.json` (3 scenes split across 29.12s, duration 29.12s, `subs.srt`), `content/plan.json` with matching scene IDs and narration matching SRT cues.
  - Result: `passed = false` (only `video_faststart_instagram` blocker failed).
  - Normal mode checks verified:
    - `subtitles_validity`: blocker, passed (6 cues parsed)
    - `subtitles_cue_coverage`: warn, passed (92.7% >= 80%)
    - `scene_timings_validity`: blocker, passed (all 3 scenes within 0..29.12s)
    - `scene_timings_continuity`: warn, passed (no overlaps or gaps > 1.0s)
    - `narration_word_coverage`: blocker, passed (100.0% >= 85%)
    - `duration_sync_merge_drift`: warn, passed (0.00s drift <= 0.5s limit)

- **Run D — Deliberately bad copy**:
  - Media: re-encoded to 1920x1080 (16:9) with audio removed (`-an`).
  - Text: caption with 7 hashtags and 2,448 characters.
  - Result: `passed = false`
  - Blockers fired (7 blockers):
    1. `video_aspect_ratio`: 1.7778 outside 9:16 ±1% range [0.5569, 0.5681]
    2. `video_resolution_minimum`: 1920x1080 below minimum 720x1280
    3. `video_faststart_instagram`: Moov atom not at front of file
    4. `audio_stream_present`: No audio stream present in media file
    5. `audio_volume_level`: Audio track missing; unable to verify volume level
    6. `text_instagram_caption_length`: Caption length 2448 exceeds 2200 characters
    7. `text_instagram_hashtags_max`: Hashtag count 7 exceeds 5 limit
  - Warnings fired: 6 warnings (`video_resolution_standard`, `video_resolution_youtube_1080p`, `duration_target_range`, `subtitles_validity`, `subtitles_cue_coverage`, `text_instagram_hook_length`).

### 3. Cross-Check (Probe vs FFmpeg Raw)

| Property | Probe Value | FFmpeg `-i` Raw Value | Mismatch? |
|---|---|---|---|
| Width | `1080` | `1080` | None |
| Height | `1920` | `1920` | None |
| Duration | `29.12 s` | `00:00:29.12` (29.12 s) | None |
| Video Codec | `h264` | `h264 (High) (avc1)` | None |
| Frame Rate | `25.0 fps` | `25 fps, 25 tbr` | None |
| Audio Codec | `aac` | `aac (LC) (mp4a)` | None |
| File Size | `1,080,669 bytes` | `1,080,669 bytes` (filesystem) | None |
| Faststart (moov before mdat) | `False` | `False` (`mdat` at 40, `moov` at 1,061,892) | None |
| Mean Volume | `-20.9 dB` | `-20.9 dB` (`ffmpeg volumedetect`) | None |

Total Mismatches: 0.

### 4. Analysis of Blockers on Real Sample
- `video_faststart_instagram` (Fired as Blocker):
  - The legacy pipeline rendered `final_reel.mp4` without `-movflags +faststart`, leaving the `moov` atom at the tail of the container.
  - While Instagram's API allows some direct file uploads that re-encode server-side, Instagram progressive upload and Reels developer guidelines require faststart for streaming startup.
  - As a blocker, this prevents unmodified legacy videos from passing QC without re-muxing. In Prompt 4, consider whether `video_faststart_instagram` should be a warning in existing-video mode or automatically repaired via faststart re-muxing.
- `text_instagram_hook_length` (Fired as Warn):
  - Legacy caption did not contain line breaks, so the first line was 179 characters (exceeding 125 char hook threshold before Instagram's `...more` fold). Correctly emitted as a warning and did not fail the run.

