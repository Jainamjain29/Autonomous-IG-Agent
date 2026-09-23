import os
import json
import time
import database as db

# Import our modules
import agent_brain
import assembly_line
import ig_service
import tunnel

WORKSPACE = os.path.join(os.getcwd(), "workspace")
PLAN_PATH = os.path.join(WORKSPACE, "current_video_plan.json")
FINAL_REEL = os.path.join(WORKSPACE, "final_reel.mp4")
TEMP_CLIP = os.path.join(WORKSPACE, "dummy_clip.mp4")

def generate_full_pipeline(topic=None, character=None, script=None, character_image_bytes=None, enable_upscale=False):
    """Runs the AI Brain and Video Assembly up to the Human Review stage."""
    print("[pipeline] STARTING AUTONOMOUS PIPELINE...")
    os.makedirs(WORKSPACE, exist_ok=True)
    
    char_img_path = None
    if character_image_bytes:
        char_img_path = os.path.join(WORKSPACE, "character_ref.jpg")
        with open(char_img_path, "wb") as f:
            f.write(character_image_bytes)
            
    # 1. AI Reasoning
    db.save_setting("WORKFLOW_STATE", "GENERATING_PLAN")
    plan = agent_brain.generate_video_plan(custom_topic=topic, custom_character=character, custom_script=script, character_image_path=char_img_path)
    script_text = plan['voiceover_script']
    
    # 2. Flow Generation via Playwright
    db.save_setting("WORKFLOW_STATE", "GENERATING_VISUALS")
    print(f"[automator] Launching Browser Automation to generate {len(plan['scenes'])} visual scenes...")
    
    import subprocess
    import flow_automator
    
    clip_paths = []
    # Create a real AI clip for each scene
    for i, scene in enumerate(plan['scenes']):
        clip_path = os.path.join(WORKSPACE, f"scene_{i}.mp4")
        clip_paths.append(clip_path)
        
        visual_prompt = scene['visual_prompt']
        style_modifier = "9:16 vertical video format, highly detailed, photorealistic. "
        full_prompt = style_modifier + visual_prompt
        
        try:
            if os.path.exists(clip_path):
                os.remove(clip_path)
                
            # Attempt to generate the video with the UI automation
            flow_automator.generate_flow_video(full_prompt, clip_path)
            
            # Optional: Upscale individual scene right after generation to save peak memory
            if enable_upscale:
                print(f"[upscale] Upscaling scene {i}...")
                upscaled_clip = os.path.join(WORKSPACE, f"scene_{i}_upscaled.mp4")
                assembly_line.upscale_video(clip_path, upscaled_clip)
                # Replace original with upscaled
                os.replace(upscaled_clip, clip_path)
                
        except Exception as e:
            print(f"[automator] ERROR: Automation failed for scene {i}: {e}. Falling back to FFmpeg dummy clip.")
            color = "blue" if i % 2 == 0 else "red"
            subprocess.run([assembly_line.FFMPEG_EXE, "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1080x1920:d=10", clip_path], check=True)
    
    # Merge the scenes into one continuous visual track
    merged_visuals = os.path.join(WORKSPACE, "merged_visuals.mp4")
    assembly_line.merge_video_clips(clip_paths, merged_visuals)
    
    # 3. Audio & Assembly
    db.save_setting("WORKFLOW_STATE", "ASSEMBLING_VIDEO")
    audio_path = os.path.join(WORKSPACE, "voiceover.mp3")
    srt_path = os.path.join(WORKSPACE, "subs.srt")
    
    assembly_line.generate_voiceover(script_text, audio_path)
    assembly_line.generate_subtitles(audio_path, srt_path)
    assembly_line.assemble_final_video(merged_visuals, audio_path, srt_path, FINAL_REEL)
    
    # Cleanup individual scene clips
    for clip in clip_paths:
        try: os.remove(clip)
        except: pass
    
    # 4. Ready for Human Review
    db.save_setting("WORKFLOW_STATE", "PENDING_REVIEW")
    print("[pipeline] PIPELINE COMPLETE. WAITING FOR HUMAN APPROVAL.")
    return True

def process_uploaded_video(video_bytes):
    """Bypasses generation and uses Gemini Vision to analyze an uploaded video."""
    print("[upload] PROCESSING UPLOADED VIDEO...")
    db.save_setting("WORKFLOW_STATE", "GENERATING_PLAN")
    
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(FINAL_REEL, "wb") as f:
        f.write(video_bytes)
        
    print("[upload] Video saved. Sending to Gemini for analysis...")
    agent_brain.analyze_video_for_caption(FINAL_REEL)
    
    db.save_setting("WORKFLOW_STATE", "PENDING_REVIEW")
    print("[upload] ANALYSIS COMPLETE. WAITING FOR HUMAN APPROVAL.")
    return True

def publish_approved_video():
    """Publishes the approved Reel to Instagram. Only valid from PENDING_REVIEW.

    PUBLISH_MODE=resumable uploads the local file directly; PUBLISH_MODE=ngrok exposes
    only the final video via an ngrok tunnel and lets Meta fetch it. Whatever happens, ngrok and the
    file server are shut down, and the state ends as IDLE (success, media ID stored in
    LAST_MEDIA_ID) or FAILED (error stored in LAST_ERROR, failing step in LAST_ERROR_STEP).
    Errors are re-raised to the caller.
    """
    state = db.get_setting("WORKFLOW_STATE")
    if state != "PENDING_REVIEW":
        raise RuntimeError(f"Publishing is only allowed from PENDING_REVIEW (current state: {state or 'unset'})")

    db.save_setting("WORKFLOW_STATE", "PUBLISHING")
    db.save_setting("LAST_ERROR", "")
    db.save_setting("LAST_ERROR_STEP", "")
    httpd = None
    succeeded = False
    try:
        if not os.path.isfile(FINAL_REEL):
            raise FileNotFoundError(f"Video not found: {FINAL_REEL}")

        # Get plan details for caption
        with open(PLAN_PATH, "r", encoding="utf-8") as f:
            plan = json.load(f)
        caption = f"{plan['instagram_caption']}\n\n{' '.join(plan['hashtags'])}"

        mode = ig_service.get_publish_mode()
        print(f"[publish] Sending to Instagram (PUBLISH_MODE={mode})...")
        if mode == "resumable":
            media_id = ig_service.publish_reel_local(FINAL_REEL, caption)
        else:
            httpd = tunnel.start_file_server(FINAL_REEL)
            print("[publish] Opening ngrok tunnel to the final video...")
            public_url = tunnel.open_tunnel(httpd.server_address[1])
            video_url = f"{public_url}/{tunnel.url_name(FINAL_REEL)}"
            print(f"[publish] Public URL: {video_url}")
            tunnel.check_public_url(video_url)
            media_id = ig_service.publish_reel(video_url, caption)

        succeeded = True
        db.save_setting("LAST_MEDIA_ID", media_id)
        print(f"[publish] PUBLISH SUCCESSFUL! IG Media ID: {media_id}")
        return media_id
    except BaseException as e:
        # "setup" = failed before the first Graph API step (missing file, bad plan, ...);
        # "tunnel" = the ngrok file server, tunnel or public URL check failed.
        step = getattr(e, "step", None) or "setup"
        message = f"Failed at step: {step}\n{type(e).__name__}: {e}"
        response = getattr(e, "response_json", None)
        if response:
            message += "\n\nAPI response:\n" + json.dumps(response, indent=2)
        db.save_setting("LAST_ERROR", message)
        db.save_setting("LAST_ERROR_STEP", step)
        print(f"[publish] FAILED at step {step}: {type(e).__name__}: {e}")
        raise
    finally:
        tunnel.stop(httpd)
        # Note: if the dashboard was reset while this publish was running, this overwrites
        # that reset. Acceptable for a single-user tool; the final state reflects the outcome.
        db.save_setting("WORKFLOW_STATE", "IDLE" if succeeded else "FAILED")

def publish_may_have_succeeded():
    """True if the last publish failed at media_publish, so the Reel may be live anyway
    (e.g. the request reached Meta but the response was lost)."""
    return db.get_setting("LAST_ERROR_STEP") == "publish"

def can_return_to_review():
    return os.path.isfile(FINAL_REEL) and os.path.isfile(PLAN_PATH)

def return_to_review():
    """FAILED -> PENDING_REVIEW, so the same video can be published again."""
    state = db.get_setting("WORKFLOW_STATE")
    if state != "FAILED":
        raise RuntimeError(f"Can only return to review from FAILED (current state: {state or 'unset'})")
    if not can_return_to_review():
        raise FileNotFoundError("final_reel.mp4 or the video plan no longer exists")
    db.save_setting("WORKFLOW_STATE", "PENDING_REVIEW")

def reset_after_failure():
    db.save_setting("LAST_ERROR", "")
    db.save_setting("LAST_ERROR_STEP", "")
    db.save_setting("WORKFLOW_STATE", "IDLE")

# Ensure starting state is IDLE
if not db.get_setting("WORKFLOW_STATE"):
    db.save_setting("WORKFLOW_STATE", "IDLE")
