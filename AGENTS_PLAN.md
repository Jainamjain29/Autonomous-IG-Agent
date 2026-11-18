# Multi-Agent Architecture — Plan & Parallel Development Rules

> **Read this whole document before working on any agent.**
> This file is the shared contract and rulebook governing parallel development across 5 autonomous agents.

---

## 1. System Purpose & Agent Division of Labor

The video generation pipeline is partitioned into 5 independent, single-responsibility agents:

1. **`content`**: Brainstorms topics, hooks, writes voiceover narration scripts, defines scene visual prompts, captions, and hashtags. Outputs `content/plan.json`.
2. **`video_gen`**: Generates individual raw video clips for each planned scene using text-to-video or image-to-video models. Outputs `video_gen/clips.json`.
3. **`sync_merge`**: Synthesizes speech with neural TTS, calculates sentence/word timestamps with Whisper, cuts clips to exact timing, crops to 9:16 vertical, and renders burnt subtitles. Outputs `sync_merge/output.json`.
4. **`qc`**: Automated quality assurance and compliance (aspect ratio, duration, audio levels, subtitle readability, policy review). Outputs `qc/report.json`. Pipeline halts at `PENDING_APPROVAL`.
5. **`posting`**: Dispatches approved video to social platforms (Instagram Reels, YouTube Shorts) via publishing adapters. Runs only after human approval. Outputs `posting/result.json`.

---

## 2. Strict Parallel Development Rules

To allow 5 agents to be developed simultaneously without collisions, every developer / agent must strictly adhere to these rules:

1. **Strict Directory Boundaries:**
   - When building `agents/<name>/`, edit **ONLY** `agents/<name>/` and `tests/agents/<name>/`.
   - **NEVER** edit files in `core/`, `config/`, root `requirements.txt`, legacy files (`app.py`, `master_loop.py`, `agent_brain.py`, `flow_automator.py`, `assembly_line.py`, `ig_service.py`, `database.py`, `tunnel.py`), or `insight/`.
2. **Zero Direct Imports Between Agents:**
   - Agents must **NEVER** import each other (e.g. `import agents.content` inside `agents.video_gen` is strictly forbidden). An automated isolation test enforces this.
3. **Communication Only Via Run Folder Files:**
   - Agents communicate purely through file artifacts in the run folder:
     - Read previous outputs via `self.read_input_json(task, "<earlier_agent>", "<filename>.json")`.
     - Write results via `self.write_output_json(task, "<filename>.json", data)`.
4. **All AI Calls Via `core.llm`:**
   - Never import external LLM SDKs or call third-party AI APIs directly.
   - Use `core.llm.complete(agent="<name>", messages=[...], json_schema={...})`.
5. **Hermetic Offline Testing:**
   - All tests in `tests/agents/<name>/` must pass **offline without network access and without real API keys**, using `core.testing.FakeLLM` or mocks.
6. **Isolated Dependencies:**
   - Add new third-party packages **only** to `agents/<name>/requirements.txt`.
   - Never add dependencies to the root `requirements.txt`.
7. **Protected System Packages (CRITICAL Windows Compatibility):**
   - **NEVER** upgrade `grpcio`, `protobuf`, `google-*`, or `pyarrow`. Pinned versions avoid DLL blocks under Windows AppLocker and Smart App Control.
8. **Git Branch & Commit Discipline:**
   - Work on dedicated feature branches: `agent/<name>`.
   - Exactly **one commit per step/feature**.

---

## 3. Data Contracts & Output Specifications

### 1. `content` Agent
* **Subfolder:** `content/`
* **Output File:** `content/plan.json`
* **JSON Schema:**
```json
{
  "title": "string",
  "topic": "string",
  "hook": "string",
  "script": "string",
  "scenes": [
    {
      "id": 1,
      "narration": "string",
      "visual_prompt": "string",
      "duration_hint_s": 5.0
    }
  ],
  "caption": "string",
  "hashtags": ["#tag1", "#tag2"],
  "platform_text": {
    "instagram": { "caption": "string" },
    "youtube": { "title": "string", "description": "string" }
  }
}
```

### 2. `video_gen` Agent
* **Subfolder:** `video_gen/`
* **Output File:** `video_gen/clips.json`
* **JSON Schema:**
```json
{
  "clips": [
    {
      "scene_id": 1,
      "path": "video_gen/scene_1.mp4",
      "duration_s": 5.0,
      "width": 1080,
      "height": 1920,
      "provider": "luma"
    }
  ]
}
```

### 3. `sync_merge` Agent
* **Subfolder:** `sync_merge/`
* **Output File:** `sync_merge/output.json`
* **JSON Schema:**
```json
{
  "video_path": "sync_merge/final_video.mp4",
  "audio_path": "sync_merge/voiceover.mp3",
  "srt_path": "sync_merge/subtitles.srt",
  "duration_s": 28.5,
  "scene_timings": [
    {
      "scene_id": 1,
      "start_s": 0.0,
      "end_s": 5.2
    }
  ]
}
```

### 4. `qc` Agent
* **Subfolder:** `qc/`
* **Output File:** `qc/report.json`
* **JSON Schema:**
```json
{
  "passed": true,
  "checks": [
    {
      "name": "aspect_ratio",
      "passed": true,
      "severity": "blocker",
      "detail": "1080x1920 (9:16) verified"
    },
    {
      "name": "audio_levels",
      "passed": true,
      "severity": "warn",
      "detail": "Integrated LUFS -14.2"
    }
  ],
  "ai_review": "string"
}
```

### 5. `posting` Agent
* **Subfolder:** `posting/`
* **Output File:** `posting/result.json`
* **JSON Schema:**
```json
{
  "posts": [
    {
      "platform": "instagram",
      "status": "ok",
      "platform_post_id": "string",
      "permalink": "string",
      "error": null
    }
  ]
}
```

---

## 4. Run Lifecycle & Status Flow

```
[create_run] -> RUNNING
       │
       ▼
 1. content     -> run.json stage "content" = "ok"
       │
       ▼
 2. video_gen   -> run.json stage "video_gen" = "ok" (skipped in video mode)
       │
       ▼
 3. sync_merge  -> run.json stage "sync_merge" = "ok" (skipped in video mode)
       │
       ▼
 4. qc          -> run.json stage "qc" = "ok"
       │
       ▼
 [STOP: PENDING_APPROVAL]
       │
       ▼ (Human reviews video & plan)
 [approve()]    -> status = "APPROVED", writes approval.json
       │
       ▼ (User / schedule triggers publish)
 5. posting     -> run.json stage "posting" = "ok"
       │
       ▼
 [COMPLETED]
```

---

## 5. Testing & Verification Requirements

Run all tests from the repository root:
```bash
python -m unittest discover -s tests -v
```

Before committing any agent step:
1. All agent unit tests in `tests/agents/<name>/` must pass.
2. The core platform tests in `tests/core/` must pass.
3. The existing 296 legacy & insight tests must remain unaffected and pass.
