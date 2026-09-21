import os
import json
import time
import google.generativeai as genai
import database as db

WORKSPACE = os.path.join(os.getcwd(), "workspace")
PLAN_PATH = os.path.join(WORKSPACE, "current_video_plan.json")

def get_gemini_client():
    api_key = db.get_setting("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in database. Please add it via the Dashboard Settings tab.")
    genai.configure(api_key=api_key)
    # Using 3.5 Flash: It is incredibly fast, free-tier friendly, and excels at strict JSON formatting.
    return genai.GenerativeModel('gemini-3.5-flash')

def generate_content_with_retry(model, *args, **kwargs):
    """Wrapper to handle Gemini API rate limits automatically."""
    import time
    from google.api_core.exceptions import ResourceExhausted
    
    for _ in range(3):
        try:
            return model.generate_content(*args, **kwargs)
        except ResourceExhausted:
            print("⚠️ Gemini API Rate limit hit! Waiting 20 seconds before retrying...")
            time.sleep(20)
    # Final attempt
    return model.generate_content(*args, **kwargs)

def generate_video_plan(custom_topic=None, custom_script=None, custom_character=None, character_image_path=None):
    print("🧠 Waking up Agent Brain...")
    model = get_gemini_client()
    
    # If a character image is provided, extract its visual description
    if character_image_path and os.path.exists(character_image_path):
        print("👁️ Analyzing Character Reference Image for consistent prompting...")
        img_file = genai.upload_file(path=character_image_path)
        while img_file.state.name == "PROCESSING":
            time.sleep(2)
            img_file = genai.get_file(img_file.name)
            
        desc_prompt = "Describe this character in meticulous detail (clothing, face, hair, age, style, accessories) so that a text-to-video model can recreate them identically. Do NOT describe the background, only the character."
        desc_response = generate_content_with_retry(model, [img_file, desc_prompt])
        custom_character = desc_response.text.strip()
        print(f"   -> Character Profile Extracted: {custom_character}")
        genai.delete_file(img_file.name)
    
    topic_instruction = f"The user has requested the specific topic: '{custom_topic}'." if custom_topic else "You must autonomously research and select a high-interest trending topic in Technology, IT, Cloud, or Cybersecurity."
    character_instruction = f"The visuals MUST feature this specific character/style: '{custom_character}'." if custom_character else "The visuals should be high-quality 3D renders or cinematic tech environments."
    script_instruction = f"The user has provided the EXACT script to use: '{custom_script}'. You MUST use this script word-for-word as the 'voiceover_script'. Do NOT change it." if custom_script else "The video must be under 45 seconds (approx 100-110 words of spoken text)."

    system_prompt = f"""
    You are an autonomous Instagram Growth Agent. Your niche is Technology, IT, Cloud, and Cybersecurity.
    Your goal is to create a viral, highly educational 9:16 Reel. 
    
    {topic_instruction}
    {character_instruction}
    {script_instruction}
    
    IMPORTANT: You must split the visual plan into multiple scenes. Each scene should be approximately 10 seconds long. If the script is 30 seconds, you should output exactly 3 scenes.
    
    Output your response EXCLUSIVELY in valid JSON format matching this exact structure:
    {{
        "topic": "The exact topic chosen",
        "rationale": "Why this is a good topic for growth based on current tech trends",
        "voiceover_script": "The exact spoken text. No stage directions, just the words to be spoken.",
        "scenes": [
            {{
                "scene_number": 1,
                "visual_prompt": "Detailed description of what the AI video generator should create for this 10-second block (e.g. 3D render of a glowing server rack, cinematic lighting)",
                "duration_seconds": 10
            }}
        ],
        "instagram_caption": "The post caption, highly engaging, formatted nicely.",
        "hashtags": ["#tech", "#cloud", "#cybersecurity"]
    }}
    """

    print("💭 Reasoning and generating content strategy...")
    
    response = generate_content_with_retry(
        model,
        system_prompt,
        generation_config={"response_mime_type": "application/json"}
    )
    
    raw_text = response.text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()
    
    # Handle "Extra data" by regex extracting the first JSON object block if necessary
    try:
        video_plan = json.loads(raw_text)
    except json.JSONDecodeError:
        try:
            import re
            print(f"⚠️ JSON Decode failed. Attempting to extract JSON block.")
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if match:
                fixed_text = re.sub(r'\\u[0-9a-fA-F]{4}', '', match.group(0))
                video_plan = json.loads(fixed_text)
            else:
                raise ValueError("No JSON block found.")
        except Exception as e:
            print(f"⚠️ Ultimate JSON failure: {e}. Forcing Gemini to retry the entire generation...")
            return generate_video_plan(custom_topic, custom_script, custom_character, character_image_path)
    
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(PLAN_PATH, "w", encoding="utf-8") as f:
        json.dump(video_plan, f, indent=4)
        
    print(f"✅ Video plan successfully generated and saved to {PLAN_PATH}")
    return video_plan

def analyze_video_for_caption(video_path):
    print("🧠 Uploading video to Gemini Vision for analysis...")
    model = get_gemini_client()
    
    # Upload to Gemini File API
    video_file = genai.upload_file(path=video_path)
    
    # Wait for processing
    while video_file.state.name == "PROCESSING":
        print("⏳ Waiting for Gemini to process the video...")
        time.sleep(2)
        video_file = genai.get_file(video_file.name)
        
    if video_file.state.name == "FAILED":
        raise ValueError("Video processing failed in Gemini.")
        
    print("👁️ Gemini is watching the video...")
    system_prompt = """
    Watch this video and act as an expert Instagram Growth Manager.
    Generate a highly engaging caption and trending hashtags that perfectly match the video's content.
    
    IMPORTANT: Do NOT use unicode escape sequences (like \\uXXXX) for emojis. Output actual emoji characters directly.
    Output EXCLUSIVELY in valid JSON format:
    {
        "instagram_caption": "Your engaging caption here",
        "hashtags": ["#tag1", "#tag2"]
    }
    """
    
    response = model.generate_content(
        [video_file, system_prompt],
        generation_config={"response_mime_type": "application/json"}
    )
    
    raw_text = response.text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()
    
    try:
        plan = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"⚠️ JSON Decode failed. Attempting to fix bad escapes. Raw text: {raw_text}")
        # Sometimes models output invalid \u escapes. We can strip them or fallback.
        import re
        fixed_text = re.sub(r'\\u[0-9a-fA-F]{4}', '', raw_text)
        plan = json.loads(fixed_text)
    plan["topic"] = "User Uploaded Video"
    plan["voiceover_script"] = "N/A"
    plan["scenes"] = []
    
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(PLAN_PATH, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=4)
        
    print(f"✅ Caption generated and saved to {PLAN_PATH}")
    
    # Cleanup file from Google's servers
    genai.delete_file(video_file.name)
    return plan

if __name__ == "__main__":
    try:
        plan = generate_video_plan()
        print("\n📋 GENERATED PLAN:")
        print(f"Topic: {plan['topic']}")
        print(f"Scenes: {len(plan['scenes'])}")
        print(f"Script Length: {len(plan['voiceover_script'].split())} words")
    except Exception as e:
        print(f"❌ Error: {e}")
