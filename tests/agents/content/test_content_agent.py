"""Comprehensive tests for ContentAgent covering all contracts, modes, and edge cases."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from agents.content.agent import ContentAgent
from agents.content.__main__ import main as cli_main
from agents.content.settings import (
    BRAND_HANDLE,
    BRAND_NAME,
    VISUAL_PROMPT_STYLE_SUFFIX,
)
from agents.content.validator import (
    clean_hashtags,
    compute_scene_duration,
    correct_brand_name,
    extract_and_strip_hashtags,
    narration_needs_more_than_8s,
    sanitize_youtube_text,
)
from core.contracts import AgentTask, RunSpec
from core.runfolder import create_run
from core.testing import FakeLLM


def _make_valid_plan(
    topic: str = "AI Video Generation",
    hook: str = "This revolutionary new artificial intelligence tool creates complete high production quality vertical videos in just seconds.",
    script: str = (
        "This revolutionary new artificial intelligence tool creates complete high production quality vertical videos in just seconds. "
        "It handles every step of the creative workflow from narrative drafting to final visual rendering. "
        "Content creators can now produce engaging explainers at scale without complex editing timelines or expensive equipment. "
        "Every single scene is precisely paced to deliver clear and accessible insights for modern viewers. "
        "Follow StreamOvate for daily breakdowns of the most exciting tools shaping the future of media."
    ),
    scenes: List[Dict[str, Any]] = None,
    hashtags: List[str] = None,
) -> Dict[str, Any]:
    if scenes is None:
        scenes = [
            {
                "id": 1,
                "narration": "This revolutionary new artificial intelligence tool creates complete high production quality vertical videos in just seconds.",
                "visual_prompt": "Cinematic shot of glowing AI neural network generating video clips.",
                "duration_hint_s": 7.0,
            },
            {
                "id": 2,
                "narration": "It handles every step of the creative workflow from narrative drafting to final visual rendering.",
                "visual_prompt": "Fast paced montage of high tech timeline rendering vertical video.",
                "duration_hint_s": 6.5,
            },
            {
                "id": 3,
                "narration": "Content creators can now produce engaging explainers at scale without complex editing timelines or expensive equipment.",
                "visual_prompt": "Futuristic creator studio with holographic screens.",
                "duration_hint_s": 7.0,
            },
            {
                "id": 4,
                "narration": "Every single scene is precisely paced to deliver clear and accessible insights for modern viewers.",
                "visual_prompt": "Dynamic graphic showing audio wave visualization and scene timeline.",
                "duration_hint_s": 6.5,
            },
            {
                "id": 5,
                "narration": "Follow StreamOvate for daily breakdowns of the most exciting tools shaping the future of media.",
                "visual_prompt": "Call to action graphic with smartphone screen showing app.",
                "duration_hint_s": 7.0,
            },
        ]
    return {
        "title": "The Future of AI Video",
        "topic": topic,
        "hook": hook,
        "script": script,
        "scenes": scenes,
        "caption": "Explore the next generation of AI video creation with @streamovate.",
        "hashtags": hashtags or ["#AIVideo", "#TechNews", "#Innovation"],
        "platform_text": {
            "instagram": {
                "caption": "Explore the next generation of AI video creation with @streamovate."
            },
            "youtube": {
                "title": "The Future of AI Video in 2026",
                "description": "Comprehensive look at autonomous video generation tools from StreamOvate.",
            },
        },
    }


class TestContentAgent(unittest.TestCase):
    def setUp(self):
        FakeLLM.reset()
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_dir = Path(self.tmp_dir.name)
        os.environ["LLM_PROVIDER"] = "fake"

    def tearDown(self):
        if "LLM_PROVIDER" in os.environ:
            del os.environ["LLM_PROVIDER"]
        self.tmp_dir.cleanup()

    def _setup_run(self, spec: RunSpec) -> Tuple[str, Path, AgentTask]:
        run_id = create_run(spec, base_dir=self.base_dir)
        run_dir = self.base_dir / run_id
        task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
        return run_id, run_dir, task

    # 1. Happy path: generate mode
    def test_generate_mode_happy_path(self):
        fake = FakeLLM.get_instance()
        valid_plan = _make_valid_plan(topic="Smart Home 2026")
        fake.set_response("content", valid_plan)

        spec = RunSpec(mode="generate", topic="Smart Home 2026")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        plan_file = run_dir / "content" / "plan.json"
        self.assertTrue(plan_file.exists())

        with open(plan_file, "r", encoding="utf-8") as f:
            saved = json.load(f)

        self.assertEqual(saved["topic"], "Smart Home 2026")
        self.assertEqual(len(saved["scenes"]), 5)
        self.assertEqual(saved["hook"], saved["scenes"][0]["narration"])
        self.assertTrue(saved["platform_text"]["instagram"]["caption"].endswith("#Innovation"))

    # 2. Happy path: user script mode verbatim pass
    def test_user_script_mode_verbatim_pass(self):
        user_script = (
            "Your smartphone now carries a dedicated AI accelerator. "
            "It runs machine learning models directly on the physical device. "
            "That delivers near instant responsiveness while keeping your sensitive personal data completely private. "
            "Next time you check mobile specifications, look for the NPU."
        )
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan(topic="AI Chips", script=user_script)
        plan["hook"] = "Your smartphone now carries a dedicated AI accelerator."
        plan["scenes"] = [
            {
                "id": 1,
                "narration": "Your smartphone now carries a dedicated AI accelerator.",
                "visual_prompt": "Macro zoom on smartphone processor glowing softly.",
                "duration_hint_s": 4.0,
            },
            {
                "id": 2,
                "narration": "It runs machine learning models directly on the physical device.",
                "visual_prompt": "Data packets swirling inside device chassis rather than reaching into cloud.",
                "duration_hint_s": 4.5,
            },
            {
                "id": 3,
                "narration": "That delivers near instant responsiveness while keeping your sensitive personal data completely private.",
                "visual_prompt": "Visual shield locking around the phone with high speed checkmarks appearing.",
                "duration_hint_s": 6.5,
            },
            {
                "id": 4,
                "narration": "Next time you check mobile specifications, look for the NPU.",
                "visual_prompt": "Tech specs graphic highlighting the NPU block.",
                "duration_hint_s": 4.5,
            },
        ]
        fake.set_response("content", plan)

        spec = RunSpec(mode="script", script=user_script, topic="AI Chips")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)
        joined = " ".join(s["narration"] for s in saved["scenes"])
        self.assertEqual(joined.split(), user_script.split())

    # 3. User script mode: mismatch triggers retry, which succeeds
    def test_user_script_mode_mismatch_retry_success(self):
        user_script = (
            "Smart glasses are evolving rapidly into lightweight everyday accessories. "
            "They feature discrete optical displays and multi-directional speakers. "
            "Now you can navigate streets and capture memories completely hands free."
        )
        bad_plan = _make_valid_plan(script=user_script)
        bad_plan["hook"] = "Smart glasses are evolving fast."
        bad_plan["scenes"] = [
            {"id": 1, "narration": "Smart glasses are evolving fast.", "visual_prompt": "Glasses visual.", "duration_hint_s": 4.0},
            {"id": 2, "narration": "They feature discrete optical displays and multi-directional speakers.", "visual_prompt": "Audio visual.", "duration_hint_s": 4.5},
            {"id": 3, "narration": "Now you can navigate streets and capture memories completely hands free.", "visual_prompt": "Street visual.", "duration_hint_s": 5.0},
        ]

        good_plan = _make_valid_plan(script=user_script)
        good_plan["hook"] = "Smart glasses are evolving rapidly into lightweight everyday accessories."
        good_plan["scenes"] = [
            {"id": 1, "narration": "Smart glasses are evolving rapidly into lightweight everyday accessories.", "visual_prompt": "Glasses visual.", "duration_hint_s": 4.5},
            {"id": 2, "narration": "They feature discrete optical displays and multi-directional speakers.", "visual_prompt": "Audio visual.", "duration_hint_s": 4.5},
            {"id": 3, "narration": "Now you can navigate streets and capture memories completely hands free.", "visual_prompt": "Street visual.", "duration_hint_s": 5.0},
        ]

        fake = FakeLLM.get_instance()
        calls = 0

        def scripted_call(agent, model, messages, expect_json=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(bad_plan), 10, 20
            return json.dumps(good_plan), 10, 20

        fake.call = scripted_call

        spec = RunSpec(mode="script", script=user_script)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        self.assertEqual(calls, 2)
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["scenes"][0]["narration"], "Smart glasses are evolving rapidly into lightweight everyday accessories.")

    # 4. User script mode: mismatch retry fails -> clean error and no plan.json
    def test_user_script_mode_mismatch_retry_fail(self):
        user_script = "Robots will assist with household chores soon."
        bad_plan = _make_valid_plan(script=user_script)
        bad_plan["hook"] = "Robots will help soon."
        bad_plan["scenes"] = [{"id": 1, "narration": "Robots will help soon.", "visual_prompt": "Robot visual.", "duration_hint_s": 4.0}]

        fake = FakeLLM.get_instance()
        fake.set_response("content", bad_plan)

        spec = RunSpec(mode="script", script=user_script)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "failed")
        self.assertIn("Script mismatch", result.error)
        self.assertFalse((run_dir / "content" / "plan.json").exists())

    # 5. Character mode: guaranteed in every scene's visual_prompt
    def test_character_mode_guaranteed_when_model_omits_it(self):
        char_desc = "Zeno, a small friendly silver robot with round blue glowing eyes and a teal scarf"
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        for s in plan["scenes"]:
            s["visual_prompt"] = "A futuristic lab with clean glass tables."
        fake.set_response("content", plan)

        spec = RunSpec(mode="character", topic="Wi-Fi", character_text=char_desc)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        for s in saved["scenes"]:
            self.assertIn(char_desc, s["visual_prompt"])

    # 6. Character mode: does not duplicate when model already included it
    def test_character_mode_does_not_duplicate_when_already_present(self):
        char_desc = "Zeno, a small friendly silver robot with round blue glowing eyes and a teal scarf"
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        for s in plan["scenes"]:
            s["visual_prompt"] = f"{char_desc} pointing at an internet router."
        fake.set_response("content", plan)

        spec = RunSpec(mode="character", topic="Wi-Fi", character_text=char_desc)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        for s in saved["scenes"]:
            self.assertEqual(s["visual_prompt"].count(char_desc), 1)

    # 7. Existing video mode: never reads video file, scenes=[], script=""
    def test_existing_video_mode_never_reads_video_file(self):
        fake_video = self.base_dir / "source.mp4"
        fake_video.write_bytes(b"dummy mp4 data 123456")
        initial_mtime = fake_video.stat().st_mtime

        fake = FakeLLM.get_instance()
        video_plan = {
            "title": "Snipping Tool Secrets",
            "topic": "Windows 11 Snipping Tool Features",
            "hook": "Did you know the Snipping Tool has secret superpowers?",
            "script": "",
            "scenes": [],
            "caption": "Three quick hidden features in Windows 11 Snipping Tool that boost productivity.",
            "hashtags": ["#Windows11", "#Productivity", "#TechTips"],
            "platform_text": {
                "instagram": {"caption": "Check out these 3 Windows 11 hacks."},
                "youtube": {
                    "title": "Windows 11 Snipping Tool Tricks",
                    "description": "Unlock hidden screen capture features.",
                },
            },
        }
        fake.set_response("content", video_plan)

        spec = RunSpec(mode="video", topic="Windows 11 Snipping Tool Features", video_path=str(fake_video))
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        self.assertEqual(fake_video.stat().st_mtime, initial_mtime)

        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        self.assertEqual(saved["scenes"], [])
        self.assertEqual(saved["script"], "")

    # 8. Hashtags: appear exactly once, deduplicated, capped at 5
    def test_hashtags_appear_exactly_once_inline_plus_list(self):
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        plan["caption"] = "Check out this amazing AI innovation! #tech #AI #innovation"
        plan["hashtags"] = ["#innovation", "#gadgets", "#future", "#ai"]
        plan["platform_text"]["youtube"]["description"] = "Full video breakdown. #tech #video"

        fake.set_response("content", plan)

        spec = RunSpec(mode="generate", topic="Hashtag Once Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        self.assertLessEqual(len(saved["hashtags"]), 5)
        self.assertEqual(len(saved["hashtags"]), len(set(h.lower() for h in saved["hashtags"])))

        ig_caption = saved["platform_text"]["instagram"]["caption"]
        body_part, tags_part = ig_caption.split("\n\n")
        self.assertNotIn("#", body_part)
        self.assertEqual(tags_part.split(), saved["hashtags"])

    # 9. Timing & Speech fit: hints come from word count even if model sends wrong numbers
    def test_duration_hints_computed_from_word_count_ignoring_model(self):
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        for s in plan["scenes"]:
            s["duration_hint_s"] = 15.0  # model wrong number
        fake.set_response("content", plan)

        spec = RunSpec(mode="generate", topic="Timing Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        for s in saved["scenes"]:
            expected_dur = compute_scene_duration(s["narration"])
            self.assertEqual(s["duration_hint_s"], expected_dur)
            self.assertNotEqual(s["duration_hint_s"], 15.0)

    # 10. Timing: 25-word scene triggers repair in generate mode
    def test_25_word_scene_triggers_repair_in_generate_mode(self):
        fake = FakeLLM.get_instance()
        bad_plan = _make_valid_plan()
        long_narration = "This is a ridiculously long scene narration containing exactly twenty five individual words designed specifically to exceed the maximum allowed eight second speech threshold for content generation."
        self.assertTrue(narration_needs_more_than_8s(long_narration))
        bad_plan["hook"] = long_narration
        bad_plan["scenes"][0]["narration"] = long_narration

        good_plan = _make_valid_plan()

        calls = 0

        def scripted_call(agent, model, messages, expect_json=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(bad_plan), 10, 20
            return json.dumps(good_plan), 10, 20

        fake.call = scripted_call

        spec = RunSpec(mode="generate", topic="Word Count Repair Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        self.assertEqual(calls, 2)

    # 11. Timing: 25-word scene in script mode is automatically split by code at boundary
    def test_25_word_scene_in_script_mode_splits_automatically(self):
        user_script = (
            "Your phone has an intelligent chip inside it now, but what does it actually do on your device instead of the cloud? "
            "It runs AI locally, which gives you faster replies, better privacy, and works completely offline. "
            "Next time you see NPU on a spec sheet, now you understand."
        )
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan(script=user_script)
        plan["hook"] = "Your phone has an intelligent chip inside it now, but what does it actually do on your device instead of the cloud?"
        plan["scenes"] = [
            {
                "id": 1,
                "narration": "Your phone has an intelligent chip inside it now, but what does it actually do on your device instead of the cloud?",
                "visual_prompt": "Smartphone on a desk.",
                "duration_hint_s": 6.0,
            },
            {
                "id": 2,
                "narration": "It runs AI locally, which gives you faster replies, better privacy, and works completely offline.",
                "visual_prompt": "Speed and privacy icons.",
                "duration_hint_s": 6.0,
            },
            {
                "id": 3,
                "narration": "Next time you see NPU on a spec sheet, now you understand.",
                "visual_prompt": "Spec sheet graphic.",
                "duration_hint_s": 5.0,
            },
        ]
        fake.set_response("content", plan)

        spec = RunSpec(mode="script", script=user_script, topic="NPU Splitting")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        self.assertGreater(len(saved["scenes"]), 3)
        for s in saved["scenes"]:
            self.assertFalse(narration_needs_more_than_8s(s["narration"]))

        joined = " ".join(s["narration"] for s in saved["scenes"])
        self.assertEqual(joined.split(), user_script.split())

    # 12. Timing: short script (~45 words) passes with no repair and total duration below 30s
    def test_short_script_passes_no_repair_and_total_under_30s(self):
        short_script = (
            "Your phone has an AI chip now. "
            "It runs machine learning on the device, not in the cloud. "
            "That means faster responses, enhanced privacy, and full offline functionality. "
            "Now you know what NPU stands for."
        )
        self.assertLessEqual(len(short_script.split()), 45)

        fake = FakeLLM.get_instance()
        plan = _make_valid_plan(script=short_script)
        plan["hook"] = "Your phone has an AI chip now."
        plan["scenes"] = [
            {"id": 1, "narration": "Your phone has an AI chip now.", "visual_prompt": "Phone on desk.", "duration_hint_s": 3.5},
            {"id": 2, "narration": "It runs machine learning on the device, not in the cloud.", "visual_prompt": "Local processing.", "duration_hint_s": 5.0},
            {"id": 3, "narration": "That means faster responses, enhanced privacy, and full offline functionality.", "visual_prompt": "Privacy shield.", "duration_hint_s": 5.0},
            {"id": 4, "narration": "Now you know what NPU stands for.", "visual_prompt": "NPU logo.", "duration_hint_s": 3.5},
        ]
        fake.set_response("content", plan)

        spec = RunSpec(mode="script", script=short_script)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        total_dur = sum(s["duration_hint_s"] for s in saved["scenes"])
        self.assertLess(total_dur, 30.0)

    # 13. Video mode: no made-up facts rule in prompt
    def test_video_mode_prompt_includes_only_facts_given_rule(self):
        fake = FakeLLM.get_instance()
        video_plan = {
            "title": "Snipping Tool Demo",
            "topic": "Snipping tool screen recording feature",
            "hook": "Learn screen recording fast.",
            "script": "",
            "scenes": [],
            "caption": "A quick look at the screen recording feature.",
            "hashtags": ["#SnippingTool"],
            "platform_text": {
                "instagram": {"caption": "A quick look at the screen recording feature."},
                "youtube": {"title": "Snipping Tool", "description": "Quick look."},
            },
        }
        fake.set_response("content", video_plan)

        spec = RunSpec(mode="video", topic="Snipping tool screen recording feature")
        _, _, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        agent.run(task)

        self.assertGreaterEqual(len(fake.recorded_calls), 1)
        prompt_content = " ".join(m["content"] for m in fake.recorded_calls[0]["messages"])
        self.assertIn("ONLY facts", prompt_content)
        self.assertIn("Never invent features", prompt_content)

    # 14. Instagram caption body == caption in all 4 modes
    def test_ig_caption_body_equals_caption_in_all_modes(self):
        modes = ["generate", "script", "character", "video"]
        for m in modes:
            FakeLLM.reset()
            fake = FakeLLM.get_instance()
            if m == "video":
                plan = {
                    "title": "Title",
                    "topic": "Topic",
                    "hook": "Hook",
                    "script": "",
                    "scenes": [],
                    "caption": f"Clean caption for mode {m}.",
                    "hashtags": ["#test"],
                    "platform_text": {
                        "instagram": {"caption": f"Different caption for mode {m}."},
                        "youtube": {"title": "YT Title", "description": "YT Desc"},
                    },
                }
                spec = RunSpec(mode="video", topic="Video Topic")
            else:
                plan = _make_valid_plan(topic=f"Topic {m}")
                plan["caption"] = f"Clean caption for mode {m}."
                plan["platform_text"]["instagram"]["caption"] = f"Mismatched caption for mode {m}."
                spec = RunSpec(mode=m, topic=f"Topic {m}", script=plan["script"], character_text="A friendly robot")

            fake.set_response("content", plan)
            _, run_dir, task = self._setup_run(spec)

            agent = ContentAgent(provider="fake")
            res = agent.run(task)
            self.assertEqual(res.status, "ok")

            with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
                saved = json.load(f)

            ig_body = saved["platform_text"]["instagram"]["caption"].split("\n\n")[0].strip()
            self.assertEqual(ig_body, saved["caption"].strip())

    # 15. Visual prompts fit AI video: suffix present exactly once
    def test_visual_prompt_suffix_present_exactly_once(self):
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        plan["scenes"][0]["visual_prompt"] = f"A high tech robot on stage. {VISUAL_PROMPT_STYLE_SUFFIX}"
        plan["scenes"][1]["visual_prompt"] = "A futuristic city in the clouds"

        fake.set_response("content", plan)

        spec = RunSpec(mode="generate", topic="Visual Suffix Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        for s in saved["scenes"]:
            self.assertTrue(s["visual_prompt"].endswith(VISUAL_PROMPT_STYLE_SUFFIX))
            self.assertEqual(s["visual_prompt"].count(VISUAL_PROMPT_STYLE_SUFFIX), 1)

    # 16. Prompt text contains no-text/no-logo/no-split-screen rule
    def test_prompt_text_contains_no_text_no_logo_rule(self):
        fake = FakeLLM.get_instance()
        fake.set_response("content", _make_valid_plan())

        spec = RunSpec(mode="generate", topic="Visual Rules Test")
        _, _, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        agent.run(task)

        prompt_content = " ".join(m["content"] for m in fake.recorded_calls[0]["messages"])
        self.assertIn("no readable on-screen text", prompt_content.lower())
        self.assertIn("logos", prompt_content.lower())
        self.assertIn("split screens", prompt_content.lower())

    # 17. Brand name correction: case-insensitive streamovate -> StreamOvate except @handle and hashtags
    def test_brand_name_corrected_to_streamovate_except_handle_and_tags(self):
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        plan["title"] = "streamovate presents the latest in tech"
        plan["caption"] = "Learn with streamovate and follow @streamovate for updates."
        plan["scenes"][0]["narration"] = "Welcome to streamovate today where we explore new tech and innovations."
        plan["hook"] = plan["scenes"][0]["narration"]
        plan["platform_text"]["youtube"]["title"] = "streamovate tech breakdown"
        plan["platform_text"]["youtube"]["description"] = "Subscribe to streamovate and follow @streamovate."
        plan["hashtags"] = ["#streamovate", "#tech"]

        fake.set_response("content", plan)

        spec = RunSpec(mode="generate", topic="Brand Casing Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        self.assertIn("StreamOvate presents", saved["title"])
        self.assertIn("Learn with StreamOvate and follow @streamovate", saved["caption"])
        self.assertIn("Welcome to StreamOvate today", saved["scenes"][0]["narration"])
        self.assertIn("StreamOvate tech breakdown", saved["platform_text"]["youtube"]["title"])
        self.assertIn("Subscribe to StreamOvate and follow @streamovate", saved["platform_text"]["youtube"]["description"])
        self.assertIn("#streamovate", saved["hashtags"])

    # 18. Output matches README contract fields exactly
    def test_output_matches_readme_fields_exactly(self):
        fake = FakeLLM.get_instance()
        fake.set_response("content", _make_valid_plan())

        spec = RunSpec(mode="generate", topic="README check")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)
        self.assertEqual(result.status, "ok")

        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            plan = json.load(f)

        expected_root_keys = {
            "title", "topic", "hook", "script", "scenes",
            "caption", "hashtags", "platform_text"
        }
        self.assertEqual(set(plan.keys()), expected_root_keys)

        for s in plan["scenes"]:
            self.assertEqual(set(s.keys()), {"id", "narration", "visual_prompt", "duration_hint_s"})

        self.assertEqual(set(plan["platform_text"].keys()), {"instagram", "youtube"})
        self.assertEqual(set(plan["platform_text"]["instagram"].keys()), {"caption"})
        self.assertEqual(set(plan["platform_text"]["youtube"].keys()), {"title", "description"})

    # 19. Only writes inside content/ directory
    def test_only_writes_inside_content_dir(self):
        fake = FakeLLM.get_instance()
        fake.set_response("content", _make_valid_plan())

        spec = RunSpec(mode="generate", topic="Isolation")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        agent.run(task)

        ignored_standard = {"spec.yaml", "run.json", "events.jsonl"}
        created = [p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_file()]
        extra_files = [p for p in created if p.parts[0] != "content" and str(p) not in ignored_standard]
        self.assertEqual(extra_files, [], f"Found writes outside content/ directory: {extra_files}")

    # 20. Every LLM call goes through core.llm with agent="content"
    def test_all_llm_calls_go_through_core_llm(self):
        fake = FakeLLM.get_instance()
        fake.set_response("content", _make_valid_plan())

        spec = RunSpec(mode="generate", topic="Gateway agent check")
        _, _, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        agent.run(task)

        self.assertGreaterEqual(len(fake.recorded_calls), 1)
        for call in fake.recorded_calls:
            self.assertEqual(call["agent"], "content")

    # 21. Unknown mode case fails with clear error
    def test_unknown_mode_fails_with_clear_error(self):
        spec = RunSpec(mode="teleportation", topic="Sci-Fi")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "failed")
        self.assertIn("Unknown or unsupported mode 'teleportation'", result.error)
        self.assertIn("Allowed modes are", result.error)
        self.assertFalse((run_dir / "content" / "plan.json").exists())

    # 22. Atomic write: no partial/temp files left on failure
    def test_atomic_write_no_partial_file_on_failure(self):
        fake = FakeLLM.get_instance()
        fake.set_response("content", {"corrupt": "data"})

        spec = RunSpec(mode="generate", topic="Atomic Failure")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "failed")
        content_dir = run_dir / "content"
        if content_dir.exists():
            files = list(content_dir.glob("*"))
            self.assertEqual(files, [], f"Expected clean content dir on failure, found: {files}")

    # 23. CLI entrypoint invocation: python -m agents.content <run_dir>
    def test_cli_main_invocation(self):
        fake = FakeLLM.get_instance()
        fake.set_response("content", _make_valid_plan())

        spec = RunSpec(mode="generate", topic="CLI Run")
        _, run_dir, _ = self._setup_run(spec)

        with mock.patch("sys.argv", ["agents.content", str(run_dir)]):
            ret = cli_main()

        self.assertEqual(ret, 0)
        self.assertTrue((run_dir / "content" / "plan.json").exists())

    # 24. YouTube angle brackets sanitization
    def test_youtube_angle_brackets_removed(self):
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        plan["platform_text"]["youtube"]["title"] = "<The Best Tech> Explainer"
        plan["platform_text"]["youtube"]["description"] = "Watch <here> for >>> awesome << updates."
        fake.set_response("content", plan)

        spec = RunSpec(mode="generate", topic="Angle Brackets Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        yt = saved["platform_text"]["youtube"]
        self.assertNotIn("<", yt["title"])
        self.assertNotIn(">", yt["title"])
        self.assertNotIn("<", yt["description"])
        self.assertNotIn(">", yt["description"])


if __name__ == "__main__":
    unittest.main()
