# Content Agent Build Log

## Step 0: Stub Setup
- Created package structure, contract documentation, and stub agent.
- Output specification defined for `content/plan.json`.
- Stub returns `status="failed"`, `error="not implemented"`.

## Step 1: Implementation Plan

### 1. Target Architecture & Files
- `agents/content/settings.py`:
  - Duration bounds: target total 30–60 s (default 45 s), 4–8 scenes, 3–8 s per scene.
  - Limits: IG caption <= 2200 chars, max 5 hashtags; YT title <= 100 chars, description <= 5000 chars.
  - Brand voice: "Clear, friendly tech explainers on the latest products (what it is, how to use it, why it matters)" for @streamovate.
  - Language: English.
- `agents/content/prompts.py`:
  - `PROMPT_VERSION = "1.0.0"`.
  - System prompts and task prompt templates per mode (`generate`, `script`, `character`, `video`).
  - Repair prompt template to pass exact validation failures for 1 retry call.
- `agents/content/schemas.py`:
  - JSON schemas for structured output via `core.llm.complete(..., json_schema=...)`.
  - Schema for standard 4-8 scene generation/script/character modes.
  - Schema for existing video mode (empty scenes and script, metadata only).
- `agents/content/validator.py`:
  - Validate schema types and required fields against README contract.
  - Validate scene count, duration hints per scene, and total duration sum (30–60 s).
  - Verbatim script check: whitespace-normalized join of scene narrations equals input script.
  - Character consistency check: character description present in every scene's `visual_prompt`.
  - Hashtags sanitizer: strip `#`, strip whitespace/symbols, dedupe case-insensitively, cap at 5.
  - YouTube text sanitizer: strip `<` and `>` characters, enforce length limits.
  - Instagram text builder: append formatted `#tag` list to IG caption without exceeding 2200 chars.
- `agents/content/agent.py`:
  - `ContentAgent(BaseAgent)` with `name="content"`.
  - Resolves input mode (`generate`, `script`, `character`, `video`).
  - Reads `spec.yaml` from `task.run_dir / "spec.yaml"`.
  - Executes primary LLM call via `core.llm.complete(agent="content", ...)`.
  - Validates output. On failure, makes 1 repair LLM call with exact error details.
  - For `script` mode, checks verbatim word-for-word match; retries once on mismatch with error feedback, then fails cleanly.
  - For `video` mode, never accesses `spec.video_path`; outputs metadata with `scenes=[]` and `script=""`.
  - Atomically writes `runs/<run_id>/content/plan.json` via temp file + replace.
- `agents/content/__main__.py`:
  - CLI runner `python -m agents.content <run_dir>` for executing a single stage.

### 2. LLM Calls per Mode
- `generate`: 1 structured call (`json_schema`) generating title, topic, hook, script, 4–8 scenes, caption, hashtags. Hook = first spoken line.
- `script`: 1 structured call splitting user script into 4–8 scenes verbatim, writing visual prompts, title, caption, hashtags. Verified word-for-word against user script.
- `character`: Same as generate, but character description injected into system/user prompt and verified across every scene's `visual_prompt`.
- `video`: 1 structured call generating title, hook, caption, hashtags, and platform text based solely on spec topic/description. No video file access.

### 3. Validation and Retry Logic
- Validation checks:
  1. JSON schema structure matching README contract.
  2. Scene count (4–8 scenes for video-creation modes; 0 for video mode).
  3. Per-scene duration (3–8 s each) and duration sum (30–60 s).
  4. Character prompt presence in every scene (character mode).
  5. Script verbatim match (script mode).
  6. Platform length limits (IG caption <= 2200, YT title <= 100, YT desc <= 5000).
- Retry policy:
  - Schema/validation failure: 1 repair call detailing exact errors. If repair fails, return `AgentResult(status="failed", error=...)` and write no `plan.json`.
  - Script mismatch: 1 targeted retry call instructing model to maintain exact verbatim script. If retry fails, return `AgentResult(status="failed")`.

### 4. Implementation Summary & Decisions
- Built `ContentAgent` adhering strictly to contract defined in `agents/content/README.md`.
- Key Architectural Decisions:
  1. **Hashtag Normalization & Placement**: Input hashtags are stripped of `#`, symbols, and whitespace, deduped case-insensitively, capped at 5, and formatted as `#tag`. Code appends them to the Instagram caption (`platform_text.instagram.caption`) rather than relying on the LLM to place them in the body text.
  2. **Verbatim Script Validation**: In `script` mode, scene narrations are joined and whitespace-normalized to compare word-for-word with `spec.script`. Any mismatch triggers a repair call passing the mismatch diff. If the retry still mismatches, execution fails cleanly without writing `plan.json`.
  3. **Character Continuity**: In `character` mode, the character description from `spec.character` or `spec.character_text` is verified in every scene's `visual_prompt`, ensuring persistent visual style across clips.
  4. **Existing Video Safety**: In `video` mode, `scenes` is set to `[]` and `script` to `""`. The agent never opens, inspects, or touches `spec.video_path`.
  5. **YouTube Sanitization**: Strips forbidden `<` and `>` characters from YouTube title (<= 100 chars) and description (<= 5000 chars).
  6. **Atomic File Write**: `plan.json` is written via a unique temporary file (`plan.json.tmp_<hex>`) with disk sync (`os.fsync`) before atomic rename. On failure, temporary files are removed and no partial `plan.json` is left on disk.
  7. **Hermetic Testing**: All tests run completely offline using `core.testing.FakeLLM` with zero network and no API keys.

### 5. Test Counts
- Content Agent tests (`tests/agents/content/`): **18 passed, 0 failed** (0.47s)
- Core & Agent architecture suite (`tests/agents`, `tests/core`, etc.): **49 passed, 0 failed** (2.20s)
- Full repository test suite: **344 passed, 1 failed** (345 collected; single failure is pre-existing legacy `tests/test_insight_collector.py::CollectorIntegrationTests::test_summary_logged_and_printed_exactly_once_on_repeated_runs` from prior step).
