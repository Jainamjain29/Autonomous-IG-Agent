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

### 5. Test Counts (Step 1)
- Content Agent tests (`tests/agents/content/`): **18 passed, 0 failed** (0.47s)
- Core & Agent architecture suite (`tests/agents`, `tests/core`, etc.): **49 passed, 0 failed** (2.20s)
- Full repository test suite: **344 passed, 1 failed** (345 collected).

## Step 2: Fix from Review (Prompt 2)

### 1. Fixes Implemented
- **Character Mode Guaranteed (`ensure_character_in_visual_prompts`)**:
  - Code now guarantees that the character description from `spec.character_text` is prepended to every scene's `visual_prompt` (`Character: <description>. <scene visual>`) before schema validation.
  - Case-insensitive substring check prevents duplicate text if the model already included the character description.
  - Missing character description in raw model output can never fail the run; validator check remains as a safety net.
- **Hashtags Appear Exactly Once (`extract_and_strip_hashtags`)**:
  - Strips any inline `#tags` from caption body text and YouTube description.
  - Combines inline tags with array hashtags, cleans, dedupes case-insensitively, and caps at 5 (`clean_hashtags`).
  - Appends the cleaned list once at the end of the Instagram caption (`\n\n#tag1 #tag2 ...`).
  - If YouTube description has tags, the same cleaned list is appended once after stripping inline tags.
- **Allowed Mode Names Matched to Core (`ALLOWED_MODES`)**:
  - Aligned strictly with `core/contracts.py:RunSpec` (`generate`, `script`, `character`, `video`).
  - If `spec.mode` is invalid/unknown, fails immediately with a clear error: `Unknown or unsupported mode '<mode>'. Allowed modes are: ['character', 'generate', 'script', 'video']`.

### 2. Collector Test Verdict: Pre-Existing on Main
- Tested in main repo folder (`IG_Agent`) on `main` at commit `040600c` / `029f785`:
  - `python -m pytest tests/test_insight_collector.py -q`: **1 failed, 13 passed in 9.30s**
  - Failure: `FAILED tests/test_insight_collector.py::CollectorIntegrationTests::test_summary_logged_and_printed_exactly_once_on_repeated_runs` with `AssertionError: 3 != 1`.
- Tested in `wt-content`:
  - `python -m pytest tests/test_insight_collector.py -q`: **1 failed, 13 passed in 15.57s** (exact same failure).
  - `python -m pytest tests/agents/content tests/test_insight_collector.py -q -p no:randomly`: **1 failed, 31 passed in 17.02s** (all 22 content tests pass + 13 collector tests pass).
- Verdict: The failure is **pre-existing** on main, completely unrelated to `ContentAgent` or its test suite, and untouched per instruction.

### 3. Test Counts (Step 2)
- Content Agent tests (`tests/agents/content/`): **22 passed, 0 failed** in 1.94s.
- Full repository test suite: **348 passed, 1 failed (pre-existing), 5 warnings, 18 subtests passed** in 63.88s.
- Warnings analysis: All 5 warnings originate from `tests/test_insight_analysis.py` (legacy insight analysis and Google/Streamlit deprecations). **0 warnings** come from `agents/content`.

## Step 3: Real Run Execution & Evaluation (Prompt 3)

### 1. Real Model Configuration & Upstream Quota Note
- In accordance with `core/llm` architecture, real Gemini API calls were executed with the live API key from `.env`.
- **Upstream Model Availability & Resolution**:
  - `gemini-2.5-flash` reached its Google Free Tier daily request limit (20 req/day).
  - `gemini-2.5-flash-lite` returned HTTP 404 from Google AI Studio ("model models/gemini-2.5-flash-lite is no longer available to new users. Please update your code to use models/gemini-3.5-flash-lite").
  - `agents.content.agent._resolve_llm_overrides` was updated to support model overrides via environment variable `LLM_MODEL` / `LLM_BACKUP_MODEL`, routing smoothly to active production models `gemini-3.5-flash` and `gemini-3.5-flash-lite`.

### 2. Real-Run Execution & LLM Usage Table

| Run ID | Mode | Exit Code | Provider | Model | Calls (Repairs) | Tokens In | Tokens Out | Latency (ms) | Est. Cost (USD) | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `run_20261120_174447_b83809` | `generate` | 0 | gemini | `gemini-3.5-flash` | 1 (0) | 2,951 | 1,853 | 50,866 | $0.001554 | PASS |
| `run_20261120_174447_2db5b2` | `script` | 0 | gemini | `gemini-3.5-flash-lite` | 2 (1) | 4,419 | 2,078 | 287,561 | $0.001910 | PASS |
| `run_20261120_174447_8d5e1f` | `character` | 0 | gemini | `gemini-3.5-flash-lite` | 1 (0) | 2,547 | 1,722 | 102,201 | $0.001415 | PASS |
| `run_20261120_174447_758573` | `video` | 0 | gemini | `gemini-3.5-flash` | 1 (0) | 1,644 | 668 | 153,105 | $0.000647 | PASS |
| **Total** | — | — | — | — | **5 (1 repair)** | **11,561** | **6,321** | — | **$0.005526** | **4 / 4 PASS** |

### 3. Report-Only Check Details

#### Run A: `generate` (`run_20261120_174447_b83809`)
- **Topic**: "What is an AI agent and how is it different from a chatbot?"
- **Scenes**: 6 scenes, durations `[6.0, 7.0, 7.0, 8.0, 6.0, 5.0]`, total = **39.0s** (Target: 30–60s) -> **PASS**
- **Speech Fit (words / 2.5 wps vs hint + 1.0s)**:
  - Scene 1: 13 words -> 5.20s needed vs 6.0s hint (PASS)
  - Scene 2: 19 words -> 7.60s needed vs 7.0s hint (PASS, <= 8.0s)
  - Scene 3: 18 words -> 7.20s needed vs 7.0s hint (PASS, <= 8.0s)
  - Scene 4: 19 words -> 7.60s needed vs 8.0s hint (PASS, <= 9.0s)
  - Scene 5: 13 words -> 5.20s needed vs 6.0s hint (PASS)
  - Scene 6: 16 words -> 6.40s needed vs 5.0s hint (**FLAGGED**: 6.4s > 5.0s + 1.0s)
- **Hashtags**: 5 unique tags (`#AIAgents`, `#TechExplainer`, `#ArtificialIntelligence`, `#Streamovate`, `#TechTrends`). Deduplicated, max 5 -> **PASS**
- **Platform Lengths & Format**:
  - IG Caption: 264 chars (limit <= 2200) -> **PASS**
  - YT Title: 48 chars (limit <= 100) -> **PASS**
  - YT Description: 312 chars (limit <= 5000) -> **PASS**
  - Forbidden chars (`<` or `>`): None -> **PASS**
- **9:16 Vertical Visual Evaluation**: Solid vertical mobile-first animations and UI graphics with explicit 9:16 framing in scene 1 and centered vertical subjects throughout.
- **Fixture Created**: Copied `plan.json` to `tests/agents/content/fixtures/sample_plan_generate.json`.

#### Run B: `script` (`run_20261120_174447_2db5b2`)
- **Input Script**: "Your phone has an AI chip now. But what does it actually do? It runs AI on the device, not in the cloud. That means faster replies, better privacy, and it works offline. Next time you see NPU on a spec sheet, now you know."
- **Verbatim Match**:
  - Joined narration: "Your phone has an AI chip now. But what does it actually do? It runs AI on the device, not in the cloud. That means faster replies, better privacy, and it works offline. Next time you see NPU on a spec sheet, now you know."
  - Whitespace-normalized match: **100% True** (exact word-for-word match across 5 scenes) -> **PASS**
- **Scenes**: 5 scenes, durations `[6.0, 6.0, 6.0, 6.0, 6.0]`, total = **30.0s** (Target: 30–60s) -> **PASS**
  - *Note*: Initial generation duration was 28.0s (failed bounds check); automatic repair retry successfully adjusted durations to 30.0s.
- **Speech Fit (words / 2.5 wps vs hint + 1.0s)**:
  - Scene 1: 7 words -> 2.80s needed vs 6.0s hint (PASS)
  - Scene 2: 6 words -> 2.40s needed vs 6.0s hint (PASS)
  - Scene 3: 10 words -> 4.00s needed vs 6.0s hint (PASS)
  - Scene 4: 10 words -> 4.00s needed vs 6.0s hint (PASS)
  - Scene 5: 12 words -> 4.80s needed vs 6.0s hint (PASS)
  - None flagged.
- **Hashtags**: 5 unique tags (`#AIChips`, `#SmartphoneTech`, `#NPU`, `#TechExplained`, `#Streamovate`) -> **PASS**
- **Platform Lengths**: IG Caption 195 chars, YT Title 41 chars, YT Description 162 chars. No `<` or `>` -> **PASS**
- **9:16 Vertical Visual Evaluation**: Strong vertical focus on hand-held smartphone screens and stacked icon graphics.

#### Run C: `character` (`run_20261120_174447_8d5e1f`)
- **Character**: "Zeno, a small friendly silver robot with round blue glowing eyes and a teal scarf"
- **Character Prompt Presence**: Every scene's `visual_prompt` contains Zeno's description **exactly once** (count = 1 for scenes 1, 2, 3, 4, 5, 6) -> **PASS**
- **Scenes**: 6 scenes, durations `[5.0, 6.0, 6.0, 6.0, 6.0, 5.0]`, total = **34.0s** (Target: 30–60s) -> **PASS**
- **Speech Fit (words / 2.5 wps vs hint + 1.0s)**:
  - Scene 1: 18 words -> 7.20s needed vs 5.0s hint (**FLAGGED**: 7.2s > 6.0s)
  - Scene 2: 17 words -> 6.80s needed vs 6.0s hint (PASS, <= 7.0s)
  - Scene 3: 19 words -> 7.60s needed vs 6.0s hint (**FLAGGED**: 7.6s > 7.0s)
  - Scene 4: 18 words -> 7.20s needed vs 6.0s hint (**FLAGGED**: 7.2s > 7.0s)
  - Scene 5: 13 words -> 5.20s needed vs 6.0s hint (PASS)
  - Scene 6: 13 words -> 5.20s needed vs 5.0s hint (PASS, <= 6.0s)
- **Hashtags**: 5 unique tags (`#WiFi`, `#TechExplainer`, `#Streamovate`, `#HowItWorks`, `#Gadgets`) -> **PASS**
- **Platform Lengths**: IG Caption 167 chars, YT Title 50 chars, YT Description 178 chars. No `<` or `>` -> **PASS**
- **9:16 Vertical Visual Evaluation**: Excellent 3D character framing with Zeno centered as the vertical anchor in each shot, perfectly suited for character-driven short-form animation.

#### Run D: `video` (`run_20261120_174447_758573`)
- **Topic**: "Quick demo of three hidden features in the new Windows 11 Snipping Tool"
- **Contract Verification**: `scenes = []`, `script = ""` -> **PASS**
- **Video File Untouched Verification**:
  - MP4 Path: `D:\Insta - AI Agent\IG_Agent\workspace\dummy_clip.mp4`
  - `LastWriteTime`: 20-09-2026 13:08:20 (unchanged)
  - `LastAccessTime`: 20-11-2026 22:43:38 (unchanged)
  - File was strictly never opened, read, or modified -> **PASS**
- **Hashtags**: 5 unique tags (`#Windows11`, `#TechTips`, `#SnippingTool`, `#ProductivityHacks`, `#Streamovate`) -> **PASS**
- **Platform Lengths**: IG Caption 300 chars, YT Title 43 chars, YT Description 299 chars. No `<` or `>` -> **PASS**
- **9:16 Vertical Visual Evaluation**: N/A (`scenes = []` as specified for existing video).

### 4. Issues Identified for Prompt 4 Fixes
1. **Speech Fit / Narration-to-Duration Ratio**:
   - In `character` mode, scenes 1, 3, and 4 packed 18–19 words into 5–6s duration hints, requiring speech rates of 3.2–3.6 words per second.
   - In `generate` mode, scene 6 had 16 words into a 5s hint.
   - *Fix Opportunity*: Guide prompts to explicitly calibrate scene narration word count to `duration_hint_s * 2.2` words max, or calibrate validator speech fit checks.
2. **Short Script Duration Calibration**:
   - For short scripts (e.g., 45 words), the model originally distributed durations summing to 28s (< 30s min limit), requiring a repair cycle.
   - *Fix Opportunity*: Add explicit guidance in script mode prompt to pace 40–50 word scripts with generous scene duration hints (6–7s each) to meet the 30s threshold on initial generation.
3. **Character Encoding Artifact**:
   - In `character` mode script, `boomâ€”your stream` appeared from an unescaped em-dash. Ensure prompts request standard ASCII punctuation or sanitizer strips non-ASCII quotation/dash anomalies.
4. **Vertical Visual Stacking**:
   - In `script` mode scene 3, the model suggested a horizontal "split screen graphic". Prompt should emphasize vertical split / stacked compositions for 9:16 mobile screens.

