import os
import time
import requests
import database as db

# Meta Graph API Base URL
GRAPH_URL = "https://graph.facebook.com/v19.0"

def get_credentials():
    access_token = db.get_setting("META_GRAPH_API_KEY")
    ig_user_id = db.get_setting("IG_ACCOUNT_ID")
    if not access_token or not ig_user_id:
        raise ValueError("Missing Meta API credentials. Please set them in the Dashboard.")
    return access_token, ig_user_id

def publish_reel(video_url, caption):
    """Publishes a Reel to Instagram via the Meta Graph API."""
    access_token, ig_user_id = get_credentials()
    
    print("\n🚀 STEP 1: Uploading Video to Meta Servers...")
    # 1. Create Media Container
    container_url = f"{GRAPH_URL}/{ig_user_id}/media"
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": access_token
    }
    
    response = requests.post(container_url, data=payload)
    result = response.json()
    
    if "error" in result:
        print(f"❌ Error creating container: {result['error']['message']}")
        return None
        
    creation_id = result.get("id")
    print(f"✅ Container created. ID: {creation_id}")
    
    print("⏳ STEP 2: Waiting for Meta to process the video (this takes about 30-60 seconds)...")
    # 2. Wait for processing to finish
    status_url = f"{GRAPH_URL}/{creation_id}?fields=status_code&access_token={access_token}"
    
    is_ready = False
    for _ in range(15): # Try for up to ~75 seconds
        time.sleep(5)
        status_res = requests.get(status_url).json()
        status = status_res.get("status_code")
        print(f"   Status: {status}...")
        
        if status == "FINISHED":
            is_ready = True
            break
        elif status == "ERROR":
            print("❌ Meta processing failed.")
            return None
            
    if not is_ready:
        print("❌ Timed out waiting for Meta processing.")
        return None
        
    print("🌟 STEP 3: Publishing the Reel to your Feed...")
    # 3. Publish the Container
    publish_url = f"{GRAPH_URL}/{ig_user_id}/media_publish"
    pub_payload = {
        "creation_id": creation_id,
        "access_token": access_token
    }
    
    pub_response = requests.post(publish_url, data=pub_payload)
    pub_result = pub_response.json()
    
    if "error" in pub_result:
        print(f"❌ Error publishing reel: {pub_result['error']['message']}")
        return None
        
    media_id = pub_result.get("id")
    print(f"🎉 SUCCESS! Reel published. IG Media ID: {media_id}")
    return media_id

def get_recent_analytics():
    """Fetches analytics for recent posts."""
    try:
        access_token, ig_user_id = get_credentials()
        url = f"{GRAPH_URL}/{ig_user_id}/media?fields=id,caption,media_type,like_count,comments_count&access_token={access_token}"
        response = requests.get(url).json()
        
        if "data" in response:
            print("\n📈 RECENT CHANNEL ANALYTICS:")
            for post in response["data"][:3]: # Show top 3 recent
                caption_preview = post.get('caption', 'No caption')[:30].replace('\n', ' ')
                print(f"- [{post['media_type']}] Likes: {post['like_count']} | Comments: {post['comments_count']} | Caption: {caption_preview}...")
            return response["data"]
    except Exception as e:
        print(f"⚠️ Could not fetch analytics: {e}")

if __name__ == "__main__":
    print("Testing Meta Graph API Integration...")
    # We use a safe, public dummy MP4 URL to test the API connection and your credentials
    test_video_url = "https://www.w3schools.com/html/mov_bbb.mp4" 
    test_caption = "Testing my Autonomous IG Agent API connection! 🤖🚀 #tech #ai"
    
    try:
        # Uncomment the line below ONLY if you are absolutely sure you want to post a test video to your live Instagram feed right now.
        # publish_reel(test_video_url, test_caption)
        
        print("\nTesting Analytics Read...")
        get_recent_analytics()
    except Exception as e:
        print(f"❌ Setup error: {e}")
