import os
import json
import time
import threading
import http.server
import socketserver
from pyngrok import ngrok
import database as db

# Import our modules
import agent_brain
import assembly_line
import ig_service

WORKSPACE = os.path.join(os.getcwd(), "workspace")
PLAN_PATH = os.path.join(WORKSPACE, "current_video_plan.json")
FINAL_REEL = os.path.join(WORKSPACE, "final_reel.mp4")
TEMP_CLIP = os.path.join(WORKSPACE, "dummy_clip.mp4")

def generate_full_pipeline(topic=None, character=None, script=None, character_image_bytes=None, enable_upscale=False):
    """Runs the AI Brain and Video Assembly up to the Human Review stage."""
    print("🚀 STARTING AUTONOMOUS PIPELINE...")
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
    print(f"🎬 [Automator] Launching Browser Automation to generate {len(plan['scenes'])} visual scenes...")
    
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
                print(f"✨ Upscaling scene {i}...")
                upscaled_clip = os.path.join(WORKSPACE, f"scene_{i}_upscaled.mp4")
                assembly_line.upscale_video(clip_path, upscaled_clip)
                # Replace original with upscaled
                os.replace(upscaled_clip, clip_path)
                
        except Exception as e:
            print(f"❌ Automation failed for scene {i}: {e}. Falling back to FFmpeg dummy clip.")
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
    print("✅ PIPELINE COMPLETE. WAITING FOR HUMAN APPROVAL.")
    return True

def process_uploaded_video(video_bytes):
    """Bypasses generation and uses Gemini Vision to analyze an uploaded video."""
    print("🚀 PROCESSING UPLOADED VIDEO...")
    db.save_setting("WORKFLOW_STATE", "GENERATING_PLAN")
    
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(FINAL_REEL, "wb") as f:
        f.write(video_bytes)
        
    print("🎬 Video saved. Sending to Gemini for analysis...")
    agent_brain.analyze_video_for_caption(FINAL_REEL)
    
    db.save_setting("WORKFLOW_STATE", "PENDING_REVIEW")
    print("✅ ANALYSIS COMPLETE. WAITING FOR HUMAN APPROVAL.")
    return True

def serve_directory(port):
    """Starts a simple HTTP server in the workspace directory."""
    import functools
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=WORKSPACE)
    httpd = socketserver.TCPServer(("", port), handler)
    httpd.serve_forever()

def publish_approved_video():
    """Uses ngrok to expose the local video and publishes via Meta API."""
    db.save_setting("WORKFLOW_STATE", "PUBLISHING")
    
    # Start local server in a background thread
    PORT = 8000
    server_thread = threading.Thread(target=serve_directory, args=(PORT,), daemon=True)
    server_thread.start()
    
    # Open ngrok tunnel
    print("🌍 Opening secure tunnel to local workspace...")
    public_url = ngrok.connect(PORT).public_url
    video_url = f"{public_url}/final_reel.mp4"
    
    print(f"🔗 Public URL ready: {video_url}")
    
    # Get plan details for caption
    with open(PLAN_PATH, "r", encoding="utf-8") as f:
        plan = json.load(f)
        
    caption = f"{plan['instagram_caption']}\n\n{' '.join(plan['hashtags'])}"
    
    # Publish to IG
    print("📲 Sending to Instagram...")
    media_id = ig_service.publish_reel(video_url, caption)
    
    # Cleanup
    ngrok.kill()
    db.save_setting("WORKFLOW_STATE", "IDLE")
    
    if media_id:
        print("🎉 PUBLISH SUCCESSFUL!")
        return media_id
    else:
        print("❌ PUBLISH FAILED.")
        return None

# Ensure starting state is IDLE
if not db.get_setting("WORKFLOW_STATE"):
    db.save_setting("WORKFLOW_STATE", "IDLE")
