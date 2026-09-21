# Autonomous Instagram Growth Agent - Detailed Project Overview

## 1. Introduction
This project is an **Autonomous Instagram Growth Agent**, a highly modular AI-driven pipeline that automates the end-to-end creation, assembly, and publishing of Instagram Reels. It utilizes a centralized UI for monitoring and human-in-the-loop review, tying together AI scriptwriting, synthetic voice generation, web-automated video generation, and direct Meta API publishing.

---

## 2. Tech Stack

### Core Environment
*   **Language:** Python 3.11
*   **Package Management:** `uv` with an isolated virtual environment (`.venv`) for maximum stability.
*   **Database:** Embedded SQLite (`database.py`) to safely store system settings and API credentials.

### Frontend & Orchestration
*   **UI Dashboard:** Streamlit (`app.py`), running a local Control Center.
*   **Master Loop:** Python orchestration scripts to manage asynchronous tasks, state machines, and daemon threads.

### AI & Media Generation Engines
*   **Agent Brain:** Google Gemini (`google-generativeai`) using the `gemini-3.5-flash` model for scriptwriting, planning, and vision analysis.
*   **Video Generation Automation:** Playwright headless browser automation (`flow_automator.py`) interacting with Google's VideoFX (Flow) UI to generate synthetic video clips.
*   **Voice Synthesis:** `edge-tts` for high-quality, neural text-to-speech.
*   **Transcription:** `openai-whisper` (backed by local PyTorch CPU) for highly accurate word-level subtitles.
*   **Video Rendering:** `imageio-ffmpeg` utilizing native FFmpeg commands and filters to merge audio/video, crop to 9:16 aspect ratio, and burn SRT subtitles.

### Publishing & Networking
*   **Meta API:** `requests` library utilizing Meta Graph API for Instagram media publishing and analytics retrieval.
*   **Secure Tunneling:** `pyngrok` paired with Python's built-in `http.server` to expose locally rendered video files securely to Meta's servers for downloading and publishing.

---

## 3. Current Features & Capabilities

What the agent can currently do:

1.  **Autonomous AI Strategy Generation (The Brain):**
    *   Generates highly structured JSON video plans containing the video topic, an Instagram caption, optimized hashtags, a voiceover script, and scene-by-scene visual prompts for the video generator.
2.  **Hybrid Generation / Manual Override:**
    *   Allows users to specify custom topics, custom character descriptions, and exact custom scripts.
    *   Accepts image uploads to act as character references for consistent visual generation.
3.  **Automated Playwright Video Sourcing:**
    *   Drives a chromium browser to automatically visit Google's Flow platform, inject visual prompts, wait for rendering, and download the resulting .mp4 clips.
4.  **End-to-End Media Assembly Line:**
    *   Synthesizes the voiceover, transcribes the voiceover to an `.srt` file, merges the video clips, and burns the subtitles onto the final `.mp4` reel.
    *   Automatically falls back to generated FFmpeg colored dummy clips if web automation fails.
5.  **Human-in-the-Loop Review Dashboard:**
    *   Presents a robust Streamlit state-machine UI (`IDLE`, `GENERATING_PLAN`, `ASSEMBLING_VIDEO`, `PENDING_REVIEW`, `PUBLISHING`).
    *   Before posting, the system halts to allow the user to watch the generated video, review the script, and either explicitly approve or reject it.
6.  **Automated Instagram Publishing:**
    *   Creates media containers on Meta's servers, polls for processing readiness, and publishes the reel directly to the Instagram feed.
7.  **Channel Analytics:**
    *   Fetches real-time engagement data (likes, comments) for recent channel posts via the Meta Graph API.
8.  **Direct Video Upload Mode:**
    *   Allows bypassing the generation engine to upload an existing .mp4. The agent will use Gemini Vision to analyze the video and generate an optimized caption before publishing.

---

## 4. Features in Development / Roadmap

The architecture is fully sound, but the system faces constraints inherent to raw text-to-video models. The following features are currently being explored or actively built (Detailed in Phase 8):

*   **Advanced Audio & Video Sync (Lip-Sync Engine):**
    *   Currently, audio is layered over rigid 10-second visual clips.
    *   **In Development:** Integrating a lip-sync model (SadTalker, Wav2Lip, SyncLabs API) or implementing precise scene trimming using Whisper word-level timestamps to dynamically cut clips to exact millisecond sentence bounds.
*   **Character Consistency (Image-to-Video Migration):**
    *   Text-to-Video prompts (like "a boy with glasses") result in slightly different characters across scenes.
    *   **In Development:** Switching to an Image-to-Video pipeline where a single master image (Midjourney/Stable Diffusion) is animated using Runway Gen-3/Luma, or training a custom Stable Diffusion LoRA.
*   **Video Quality Upscaling:**
    *   **In Development:** Implementing an AI upscaler like **Real-ESRGAN** into the assembly line to enhance the 720p AI-generated clips to a crisp 1080p/4K before final stitching. Alternatively, swapping the web automator to interact with higher-end models like Kling AI or Luma Dream Machine.

---

## 5. Broken Features & Known API Limitations

1.  **Fragile Web Automation (Google Flow):**
    *   The `flow_automator.py` script relies on specific CSS selectors (`textarea`, `button:has-text('Generate')`). If Google alters the VideoFX UI, the web automation breaks. The system mitigates this by falling back to FFmpeg dummy colored clips.
2.  **Playwright Interactive Login Blocked:**
    *   The original plan to interactively log into Google Flow via Playwright was blocked by the background agent environment. It is currently bypassed by injecting a `cookies.json` file.
3.  **Windows AppLocker `grpcio` Block:**
    *   The latest Google Gemini `grpcio` dependencies triggered a Windows AppLocker policy block on `cygrpc`. This required a forced downgrade to a trusted version of `grpcio`.
4.  **Deprecated Gemini Models:**
    *   The original codebase targeted `gemini-1.5-flash`, which was deprecated, necessitating an update to `gemini-3.5-flash`.
5.  **Slow Local CPU Rendering:**
    *   AI Upscaling post-processing (if enabled via the UI) takes an exorbitant amount of time due to reliance on CPU processing, lacking dedicated GPU acceleration integration in the current pipeline.
