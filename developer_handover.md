# 🚀 Autonomous IG Agent: Developer Handover Document

This document provides a deep, technical breakdown of the Autonomous IG Agent's architecture, data flows, and code quirks. It is designed to act as a complete onboarding guide for a developer taking over the project.

---

## 1. System Architecture & Data Flow

The system operates as a state-machine driven pipeline orchestrated by a local Streamlit dashboard. 

### High-Level Data Flow:
1. **User (Streamlit)** triggers the pipeline in `app.py`.
2. **`master_loop.py`** takes control, updating the SQLite state to `GENERATING_PLAN`.
3. **`agent_brain.py`** queries Google Gemini (v3.5 Flash) with strict JSON schema instructions to generate a video plan (Topic, Script, Visual Prompts). The plan is saved to `workspace/current_video_plan.json`.
4. State updates to `GENERATING_VISUALS`. **`flow_automator.py`** drives a headless Playwright Chromium instance to Google Flow, injects the visual prompts, and downloads the raw `.mp4` clips to the `workspace/` dir.
5. State updates to `ASSEMBLING_VIDEO`. **`assembly_line.py`** generates Voiceover via `edge-tts`, transcribes the VO locally using `whisper`, and uses `imageio_ffmpeg` to crop, stitch, and burn the subtitles into `workspace/final_reel.mp4`.
6. State updates to `PENDING_REVIEW`. The pipeline halts. The **Streamlit UI** displays the video and plan for human approval.
7. User clicks Approve. State updates to `PUBLISHING`.
8. **`ig_service.py`** takes over. `master_loop.py` spawns a daemon thread running `http.server` and opens a `pyngrok` tunnel to expose `final_reel.mp4` to the internet. 
9. `ig_service.py` hits the Meta Graph API, points it to the ngrok URL, polls for processing completion, publishes the Reel, and tears down the ngrok tunnel.
10. State returns to `IDLE`.

---

## 2. Environment & Dependency Setup

*   **Python Version:** Python 3.11 (Managed via `uv`).
*   **Virtual Environment:** Located in `IG_Agent/.venv`.
*   **Database:** A local SQLite database (`data/agent_database.db`) acts as a simple Key-Value store for API keys and state.
*   **FFmpeg:** Handled dynamically via the `imageio_ffmpeg` Python package. The `assembly_line.py` script automatically resolves the local binary path and injects it into the system `PATH` so `openai-whisper` can utilize it.

---

## 3. Module Deep-Dive

### `app.py` (The Control Center)
*   **Role:** Streamlit frontend and State Machine interface.
*   **Key Concept:** Relies heavily on the `WORKFLOW_STATE` key in the SQLite DB. Valid states are `IDLE`, `GENERATING_PLAN`, `GENERATING_VISUALS`, `ASSEMBLING_VIDEO`, `PENDING_REVIEW`, and `PUBLISHING`.
*   **Gotcha:** Streamlit is synchronous. The `master_loop.generate_full_pipeline()` call blocks the UI thread while it runs. `st.spinner` handles the visual feedback.

### `master_loop.py` (The Orchestrator)
*   **Role:** Wires all modules together and manages the `workspace/` file IO.
*   **Ngrok Integration:** To bypass Meta's requirement for a public URL when uploading videos, `serve_directory(port)` is spun up in a `threading.Thread(daemon=True)`. Ngrok tunnels to this local server, allowing Meta to download the video directly from the local machine.
*   **Fallback Logic:** If `flow_automator.py` crashes during a scene generation, `master_loop.py` catches the exception and dynamically creates a colored dummy clip via FFmpeg so the pipeline doesn't completely fail.

### `agent_brain.py` (The Strategist)
*   **Role:** Interfaces with `google-generativeai`.
*   **Model:** Hardcoded to `gemini-3.5-flash`.
*   **Robust JSON Parsing:** LLMs are notorious for wrapping JSON in markdown blocks (````json ... ````) or prepending conversational text. The `generate_video_plan()` function includes a robust Regex fallback (`re.search(r'\{.*\}', raw_text, re.DOTALL)`) to strip bad Unicode escapes and extract the raw JSON block safely.
*   **Vision Capabilities:** `analyze_video_for_caption()` allows the agent to ingest a user-uploaded MP4 via the Gemini File API to generate a caption without creating new video content.

### `flow_automator.py` (The Scraper)
*   **Role:** Headless Playwright automation for Google Flow (VideoFX).
*   **Authentication:** Requires a persistent browser context. Since interactive login is blocked, a valid session cookie must be present in `data/browser_profile`.
*   **CSS Selector Fragility:** Relies on hardcoded selectors (`textarea`, `button:has-text('Generate')`, `button[aria-label='Download']`). **If Google updates their UI, this file will break and need immediate patching.**
*   **Timeouts:** Video generation takes time. The `wait_for_selector` for the download button is explicitly set to `180000` ms (3 minutes).

### `assembly_line.py` (The Media Engine)
*   **Role:** Handles all media synthesis and rendering.
*   **Whisper:** Uses `whisper.load_model("base")` for fast, local CPU transcription. Generates an `.srt` file.
*   **FFmpeg Filter Complex:** The core magic is here: `crop=ih*(9/16):ih,scale=1080:1920,subtitles={srt_file}:force_style=...`. This single command crops the raw horizontal flow video to 9:16, scales it to 1080x1920, and burns the `.srt` subtitles with custom styling.
*   **Upscaling Hack:** True AI upscaling (Real-ESRGAN) is too slow for CPU. The `upscale_video()` function uses an advanced FFmpeg filter pipeline (`hqdn3d`, `unsharp`, `lanczos` scaling) to simulate upscaling via noise reduction and sharpening.

### `ig_service.py` (The Publisher)
*   **Role:** Meta Graph API v19.0 Integration.
*   **Two-Step Publish:** 
    1. Sends video URL to `/media` to create a container.
    2. Enters a polling loop (`time.sleep(5)`), hitting the container ID until the `status_code` equals `FINISHED`. 
    3. Pushes the container to `/media_publish`.

---

## 4. Known Quirks, Bugs, and "Gotchas" (CRITICAL)

1. **The `grpcio` AppLocker Issue:** 
   * *The Problem:* Newer versions of Google's `grpcio` library attempt to load a DLL (`cygrpc`) that triggers Windows AppLocker security policies, crashing the script.
   * *The Fix:* The environment currently runs a specific, trusted downgrade of `grpcio`. **Do not blindly update `grpcio` or `google-generativeai` without testing on a Windows environment with AppLocker enabled.**

2. **FFmpeg Path Escaping on Windows:**
   * *The Problem:* When `master_loop.py` creates a `concat_list.txt` to merge clips, standard Windows backslashes (`\`) break FFmpeg's parser.
   * *The Fix:* Paths must explicitly have backslashes replaced with forward slashes (`safe_path = clip.replace("\\", "/")`) before writing to the concat file.

3. **Playwright Authentication Bypass:**
   * *The Problem:* Running a GUI browser to log into Google Flow manually is blocked by the background agent runner.
   * *The Fix:* The system uses a cookie-injection bypass. Session data must be manually placed into the `data/` directory.

---

## 5. Development Roadmap (Taking it Forward)

If you are a developer taking over, here is exactly how you should execute the **Phase 8 Roadmap**:

### 1. Fixing Audio & Video Sync (High Priority)
Right now, `master_loop.py` blindly assigns 10 seconds of video to scenes regardless of script length. 
**How to fix:** 
1. Modify `assembly_line.generate_subtitles()` to return the word-level timestamps provided natively by Whisper.
2. Group the words by sentence. 
3. Calculate the exact duration of each sentence. 
4. Pass these durations back to `master_loop.py`, and use FFmpeg to dynamically `trim` the generated video clips to match the exact duration of the spoken sentence before concatenating them.

### 2. Replacing Google Flow for Character Consistency
Google Flow does not support API access or Image-to-Video character locking.
**How to fix:**
1. Rip out the Playwright logic in `flow_automator.py`.
2. Replace it with direct API calls to **Luma AI** or **Runway Gen-3**.
3. Modify `agent_brain.py` to generate a primary "Master Character Image" prompt. Send this to the Midjourney/Stable Diffusion API.
4. Pass that generated Image URL + the Scene Prompt to the Luma/Runway API to ensure the exact same character is animated in every scene.
