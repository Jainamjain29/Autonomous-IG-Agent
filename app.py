import sys
if sys.platform == 'win32':
    import codecs
    try:
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
    except Exception:
        pass

import os
import json
import streamlit as st
import database as db
import master_loop
import time

st.set_page_config(page_title="IG Growth Agent", layout="wide")
st.title("🤖 Autonomous IG Channel Agent")

# Get current state
current_state = db.get_setting("WORKFLOW_STATE")
if not current_state:
    current_state = "IDLE"
    db.save_setting("WORKFLOW_STATE", "IDLE")

tab1, tab2, tab3 = st.tabs(["🎬 Dashboard & Review", "📈 Analytics", "⚙️ Settings"])

with tab1:
    st.header("Control Center")
    st.write(f"**System Status:** `{current_state}`")
    
    if current_state == "IDLE":
        st.subheader("Autonomous Generation")
        with st.expander("🛠️ Advanced Options (Hybrid Mode)", expanded=False):
            custom_topic = st.text_input("Custom Topic (Leave blank for autonomous)")
            custom_character = st.text_input("Custom Visual Character/Style (Leave blank for default)")
            
            st.markdown("**Character Consistency**")
            character_image = st.file_uploader("Upload Character Reference Image (Gemini will analyze this to enforce visual consistency)", type=["jpg", "jpeg", "png"])
            
            custom_script = st.text_area("Custom Exact Script (Leave blank for AI generation)")
            
            st.markdown("**Post-Processing**")
            enable_upscale = st.checkbox("Enable AI Upscaling (Warning: Takes significant time without a powerful GPU)")
            
        if st.button("🚀 Trigger AI Content Pipeline", type="primary", use_container_width=True):
            with st.spinner("Agent is researching, writing, and assembling the video... Please wait."):
                
                char_image_bytes = character_image.read() if character_image else None
                
                master_loop.generate_full_pipeline(
                    topic=custom_topic if custom_topic else None,
                    character=custom_character if custom_character else None,
                    script=custom_script if custom_script else None,
                    character_image_bytes=char_image_bytes,
                    enable_upscale=enable_upscale
                )
                st.rerun()
                
        st.divider()
        st.subheader("Direct Video Upload")
        uploaded_video = st.file_uploader("Upload an existing .mp4 video for Auto-Captioning", type=["mp4"])
        if uploaded_video:
            if st.button("🚀 Process Uploaded Video", use_container_width=True):
                with st.spinner("Gemini is watching your video to generate a strategy..."):
                    master_loop.process_uploaded_video(uploaded_video.read())
                    st.rerun()
                
    elif current_state == "PENDING_REVIEW":
        st.success("✅ Content Generated and Ready for Review!")
        
        col1, col2 = st.columns([1, 1])
        
        with col1:
            st.subheader("Video Preview")
            if os.path.exists(master_loop.FINAL_REEL):
                st.video(master_loop.FINAL_REEL)
            else:
                st.error("Video file not found.")
                
        with col2:
            st.subheader("Content Strategy")
            if os.path.exists(master_loop.PLAN_PATH):
                with open(master_loop.PLAN_PATH, "r") as f:
                    plan = json.load(f)
                st.write(f"**Topic:** {plan['topic']}")
                st.write(f"**Caption:** {plan['instagram_caption']}")
                st.write(f"**Hashtags:** {' '.join(plan['hashtags'])}")
                
                with st.expander("View Script & Scene Prompts"):
                    st.write(plan['voiceover_script'])
                    st.json(plan['scenes'])
        
        st.divider()
        col3, col4 = st.columns(2)
        with col3:
            if st.button("✅ APPROVE & PUBLISH TO INSTAGRAM", type="primary", use_container_width=True):
                with st.spinner("Uploading to Instagram... This can take a few minutes."):
                    try:
                        result = master_loop.publish_approved_video()
                        st.success(f"Successfully Published! Media ID: {result}")
                        time.sleep(3)
                    except Exception as e:
                        # Publish failures leave the state at FAILED, which the rerun shows.
                        # Anything else (e.g. a stale tab publishing from the wrong state) is shown here.
                        if db.get_setting("WORKFLOW_STATE") != "FAILED":
                            st.error(f"Could not publish: {e}")
                            st.stop()
                st.rerun()
        with col4:
            if st.button("🗑️ REJECT & RESTART", use_container_width=True):
                db.save_setting("WORKFLOW_STATE", "IDLE")
                st.rerun()

    elif current_state == "PUBLISHING":
        st.info("Publishing in progress... Check terminal for details.")
        st.caption("Only reset if the publish is stuck (e.g. the app was restarted mid-publish). "
                   "Resetting while a publish is still running will be overwritten when it finishes.")
        if st.button("🔄 Reset to IDLE", use_container_width=True):
            db.save_setting("WORKFLOW_STATE", "IDLE")
            st.rerun()

    elif current_state == "FAILED":
        st.error("❌ Publishing failed.")
        if master_loop.publish_may_have_succeeded():
            st.warning("⚠️ Publish may have succeeded — check Instagram before retrying.")
        st.code(db.get_setting("LAST_ERROR") or "No error message was recorded.", language=None)
        st.caption("Full request/response logs are in the terminal.")
        col_back, col_reset = st.columns(2)
        with col_back:
            can_review = master_loop.can_return_to_review()
            if st.button("↩️ Back to review", use_container_width=True, disabled=not can_review):
                master_loop.return_to_review()
                st.rerun()
            if not can_review:
                st.caption("The video or plan file no longer exists, so it can't be reviewed again.")
        with col_reset:
            if st.button("🔄 Reset to IDLE", type="primary", use_container_width=True):
                master_loop.reset_after_failure()
                st.rerun()
        
    else:
        st.info(f"System is currently in state: {current_state}. Please wait...")
        if st.button("🚨 Emergency Reset to IDLE", type="primary", use_container_width=True):
            db.save_setting("WORKFLOW_STATE", "IDLE")
            st.rerun()

with tab2:
    st.header("Channel Analytics")
    if st.button("Fetch Latest Analytics"):
        import ig_service
        data = ig_service.get_recent_analytics()
        if data:
            st.json(data)
        else:
            st.error("Failed to fetch analytics.")

with tab3:
    st.header("System Configuration")
    # Pre-fill from SQLite only, so saving the form never copies .env secrets into the database.
    saved_gemini = db.get_stored_setting("GEMINI_API_KEY")
    saved_meta = db.get_stored_setting("META_GRAPH_API_KEY")
    saved_ig = db.get_stored_setting("IG_ACCOUNT_ID")

    env_overrides = [k for k in ("GEMINI_API_KEY", "META_GRAPH_API_KEY", "IG_ACCOUNT_ID") if db.setting_source(k) == ".env"]
    if env_overrides:
        st.info(f"Set in .env (takes priority over values saved here): {', '.join(env_overrides)}")
    
    with st.form("settings_form"):
        gemini_key = st.text_input("Gemini API Key", value=saved_gemini, type="password")
        meta_key = st.text_input("Meta Graph API Access Token", value=saved_meta, type="password")
        ig_id = st.text_input("IG Account ID", value=saved_ig)
        if st.form_submit_button("Save Configuration"):
            db.save_setting("GEMINI_API_KEY", gemini_key)
            db.save_setting("META_GRAPH_API_KEY", meta_key)
            db.save_setting("IG_ACCOUNT_ID", ig_id)
            st.success("Settings saved locally!")
