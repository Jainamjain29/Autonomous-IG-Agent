import os
import time
import sys
import asyncio
from playwright.sync_api import sync_playwright, TimeoutError

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

PROFILE_DIR = os.path.join(os.getcwd(), "data", "browser_profile")

# ==========================================
# ⚠️ CSS SELECTORS (Adjust these if Google changes their UI)
# ==========================================
URL = "https://aitestkitchen.withgoogle.com/tools/video-fx" # Adjust to exact Flow URL if different
PROMPT_TEXTAREA = "textarea" # usually 'textarea' works
GENERATE_BUTTON = "button:has-text('Generate')" # Button containing text Generate
DOWNLOAD_BUTTON = "button[aria-label='Download']" # Adjust based on actual UI
# ==========================================

def initial_setup():
    print("\n🚀 INITIAL SETUP: BROWSER AUTHENTICATION")
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR, headless=False, viewport={"width": 1280, "height": 720}
        )
        page = browser.new_page()
        page.goto("https://accounts.google.com")
        print("Waiting for you to log in and close the browser...")
        try:
            while len(browser.pages) > 0:
                time.sleep(1)
        except Exception:
            pass
        print("✅ Session saved.")

def generate_flow_video(prompt_text, output_filepath):
    """Automates the browser to generate and download a video clip."""
    print(f"🎬 [Playwright] Starting generation for prompt: {prompt_text[:50]}...")
    
    with sync_playwright() as p:
        # headless=False so the user can watch the magic happen (can be changed to True later)
        browser = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR, 
            headless=False, 
            viewport={"width": 1280, "height": 720}
        )
        page = browser.new_page()
        
        try:
            # 1. Navigate to Flow
            print("   -> Navigating to Google Flow...")
            page.goto(URL, timeout=60000)
            page.wait_for_load_state("networkidle")
            
            # 2. Enter the Prompt
            print("   -> Injecting prompt...")
            page.wait_for_selector(PROMPT_TEXTAREA, timeout=15000)
            page.fill(PROMPT_TEXTAREA, prompt_text)
            
            # 3. Click Generate
            print("   -> Clicking Generate...")
            page.click(GENERATE_BUTTON)
            
            # 4. Wait for generation and Download
            print("   -> Waiting for generation to complete (this can take 1-3 minutes)...")
            
            # Wait for the download event to trigger when we click the download button
            # We use a long timeout (180s) because video generation takes time
            page.wait_for_selector(DOWNLOAD_BUTTON, timeout=180000) 
            
            with page.expect_download(timeout=60000) as download_info:
                page.click(DOWNLOAD_BUTTON)
                
            download = download_info.value
            download.save_as(output_filepath)
            print(f"✅ Video successfully downloaded to {output_filepath}")
            
        except TimeoutError as e:
            print(f"❌ Playwright Timeout: {e}")
            page.screenshot(path=os.path.join(os.getcwd(), "workspace", "error_screenshot.png"))
            print("📸 Saved error screenshot to workspace/error_screenshot.png")
            raise e
        finally:
            browser.close()
