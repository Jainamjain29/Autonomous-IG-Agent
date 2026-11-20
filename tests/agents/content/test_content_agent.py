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
from agents.content.validator import (
    clean_hashtags,
    extract_and_strip_hashtags,
    sanitize_youtube_text,
)
from core.contracts import AgentTask, RunSpec
from core.runfolder import create_run
from core.testing import FakeLLM


def _make_valid_plan(
    topic: str = "AI Video Generation",
    hook: str = "This new AI creates full videos in seconds.",
    script: str = "This new AI creates full videos in seconds. It handles everything automatically. The future of content creation is finally here. You should test it right away.",
    scenes: List[Dict[str, Any]] = None,
    hashtags: List[str] = None,
) -> Dict[str, Any]:
    if scenes is None:
        scenes = [
            {
                "id": 1,
                "narration": "This new AI creates full videos in seconds.",
                "visual_prompt": "Cinematic shot of glowing AI neural network generating video clips.",
                "duration_hint_s": 8.0,
            },
            {
                "id": 2,
                "narration": "It handles everything automatically.",
                "visual_prompt": "Fast paced montage of high tech timeline rendering vertical video.",
                "duration_hint_s": 8.0,
            },
            {
                "id": 3,
                "narration": "The future of content creation is finally here.",
                "visual_prompt": "Futuristic creator studio with holographic screens.",
                "duration_hint_s": 8.0,
            },
            {
                "id": 4,
                "narration": "You should test it right away.",
                "visual_prompt": "Call to action graphic with smartphone screen showing app.",
                "duration_hint_s": 8.0,
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
                "description": "Comprehensive look at autonomous video generation tools.",
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
        self.assertEqual(len(saved["scenes"]), 4)
        self.assertEqual(saved["hook"], saved["scenes"][0]["narration"])
        self.assertTrue(saved["platform_text"]["instagram"]["caption"].endswith("#Innovation"))

    # 2. Happy path: user script mode verbatim pass
    def test_user_script_mode_verbatim_pass(self):
        user_script = (
            "This new AI creates full videos in seconds. "
            "It handles everything automatically. "
            "The future of content creation is finally here. "
            "You should test it right away."
        )
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan(script=user_script)
        fake.set_response("content", plan)

        spec = RunSpec(mode="script", script=user_script, topic="AI Video")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        plan_file = run_dir / "content" / "plan.json"
        with open(plan_file, "r", encoding="utf-8") as f:
            saved = json.load(f)

        joined_narration = " ".join(s["narration"] for s in saved["scenes"])
        self.assertEqual(" ".join(joined_narration.split()), " ".join(user_script.split()))

    # 3. User script mode: mismatch -> retry -> success
    def test_user_script_mode_mismatch_retry_success(self):
        user_script = (
            "This new AI creates full videos in seconds. "
            "It handles everything automatically. "
            "The future of content creation is finally here. "
            "You should test it right away."
        )
        fake = FakeLLM.get_instance()

        bad_plan = _make_valid_plan(script=user_script)
        # Change words in first scene
        bad_plan["scenes"][0]["narration"] = "A brand new AI makes full videos quickly."

        good_plan = _make_valid_plan(script=user_script)

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
        self.assertTrue((run_dir / "content" / "plan.json").exists())

    # 4. User script mode: mismatch -> retry -> fail
    def test_user_script_mode_mismatch_retry_fail(self):
        user_script = "Word one word two word three word four."
        fake = FakeLLM.get_instance()

        bad_plan = _make_valid_plan(script=user_script)
        bad_plan["scenes"][0]["narration"] = "Completely altered text."

        calls = 0

        def scripted_call(agent, model, messages, expect_json=False):
            nonlocal calls
            calls += 1
            return json.dumps(bad_plan), 10, 20

        fake.call = scripted_call

        spec = RunSpec(mode="script", script=user_script)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "failed")
        self.assertEqual(calls, 2)
        self.assertIn("Script mismatch", result.error)
        self.assertFalse((run_dir / "content" / "plan.json").exists())

    # 5. Character mode: character text in every visual prompt
    def test_character_mode_description_in_every_visual_prompt(self):
        char_desc = "A cheerful blue android robot with neon yellow antennas"
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan(topic="Future Robotics")
        for s in plan["scenes"]:
            s["visual_prompt"] = f"A cheerful blue android robot with neon yellow antennas exploring the city."
        fake.set_response("content", plan)

        spec = RunSpec(mode="character", topic="Future Robotics", character_text=char_desc)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        plan_file = run_dir / "content" / "plan.json"
        with open(plan_file, "r", encoding="utf-8") as f:
            saved = json.load(f)

        for s in saved["scenes"]:
            self.assertIn(char_desc.lower(), s["visual_prompt"].lower())

    # 5b. Character mode: guaranteed even if model omits it
    def test_character_mode_guaranteed_when_model_omits_it(self):
        char_desc = "A futuristic cyborg dog with silver paws"
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan(topic="Cyborg Pets")
        # Model omits character description entirely from scene prompts
        for i, s in enumerate(plan["scenes"]):
            s["visual_prompt"] = f"Generic park scene {i+1} with autumn trees."
        fake.set_response("content", plan)

        spec = RunSpec(mode="character", topic="Cyborg Pets", character_text=char_desc)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        plan_file = run_dir / "content" / "plan.json"
        with open(plan_file, "r", encoding="utf-8") as f:
            saved = json.load(f)

        for s in saved["scenes"]:
            self.assertIn(char_desc.lower(), s["visual_prompt"].lower())
            self.assertTrue(s["visual_prompt"].startswith(f"Character: {char_desc}."))

    # 5c. Character mode: does not duplicate text if model already included it
    def test_character_mode_does_not_duplicate_when_already_present(self):
        char_desc = "A futuristic cyborg dog with silver paws"
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan(topic="Cyborg Pets")
        for s in plan["scenes"]:
            s["visual_prompt"] = f"A futuristic cyborg dog with silver paws running through a park."
        fake.set_response("content", plan)

        spec = RunSpec(mode="character", topic="Cyborg Pets", character_text=char_desc)
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        for s in saved["scenes"]:
            count = s["visual_prompt"].lower().count(char_desc.lower())
            self.assertEqual(count, 1)

    # 6. Existing video mode: never reads video file, scenes=[], script=""
    def test_existing_video_mode_never_reads_video_file(self):
        fake = FakeLLM.get_instance()
        video_plan = {
            "title": "Amazing Drone Flight",
            "topic": "FPV Drone Sunset",
            "hook": "Watch this cinematic FPV sunset drift.",
            "script": "",
            "scenes": [],
            "caption": "Incredible sunset captured on FPV drone.",
            "hashtags": ["#FPV", "#Drone", "#Sunset"],
            "platform_text": {
                "instagram": {"caption": "Incredible sunset captured on FPV drone."},
                "youtube": {"title": "Epic FPV Sunset", "description": "Raw 4K FPV flight."},
            },
        }
        fake.set_response("content", video_plan)

        # Non-existent video path: if agent attempts to open it, an error or detection would occur
        spec = RunSpec(
            mode="video",
            topic="FPV Drone Sunset",
            video_path="runs/fake_video_does_not_exist.mp4",
        )
        _, run_dir, task = self._setup_run(spec)

        with mock.patch("builtins.open", wraps=open) as mock_file_open:
            agent = ContentAgent(provider="fake")
            result = agent.run(task)

            # Ensure open() was never called with the video path
            opened_files = [str(call.args[0]) for call in mock_file_open.call_args_list if call.args]
            self.assertFalse(any("fake_video_does_not_exist.mp4" in f for f in opened_files))

        self.assertEqual(result.status, "ok")
        plan_file = run_dir / "content" / "plan.json"
        with open(plan_file, "r", encoding="utf-8") as f:
            saved = json.load(f)

        self.assertEqual(saved["scenes"], [])
        self.assertEqual(saved["script"], "")
        self.assertEqual(saved["title"], "Amazing Drone Flight")

    # 7. Schema / validation failure -> repair call succeeds
    def test_schema_failure_repair_succeeds(self):
        fake = FakeLLM.get_instance()
        # Invalid plan: scene count is only 1 (minimum required is 4)
        bad_plan = _make_valid_plan()
        bad_plan["scenes"] = [bad_plan["scenes"][0]]
        valid_plan = _make_valid_plan()

        calls = 0

        def scripted_call(agent, model, messages, expect_json=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(bad_plan), 10, 20
            return json.dumps(valid_plan), 10, 20

        fake.call = scripted_call

        spec = RunSpec(mode="generate", topic="Repair Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        self.assertEqual(calls, 2)
        self.assertTrue((run_dir / "content" / "plan.json").exists())

    # 8. Repair also fails -> clean error and no plan.json
    def test_repair_also_fails_clean_error_no_plan(self):
        fake = FakeLLM.get_instance()
        # Consistently invalid plan with 1 scene
        bad_plan = _make_valid_plan()
        bad_plan["scenes"] = [bad_plan["scenes"][0]]

        calls = 0

        def scripted_call(agent, model, messages, expect_json=False):
            nonlocal calls
            calls += 1
            return json.dumps(bad_plan), 10, 20

        fake.call = scripted_call

        spec = RunSpec(mode="generate", topic="Double Failure")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "failed")
        self.assertEqual(calls, 2)
        self.assertIn("Validation failed after repair", result.error)
        self.assertFalse((run_dir / "content" / "plan.json").exists())

    # 9. Hashtag cleanup + cap (strip '#', remove spaces/symbols, dedupe, cap 5)
    def test_hashtag_cleanup_and_cap(self):
        raw = ["#AI", "Tech News!", "#ai", "#coding-life", "Python 3", "#Extra1", "#Extra2"]
        cleaned = clean_hashtags(raw, max_count=5)
        # Should be ['#AI', '#TechNews', '#codinglife', '#Python3', '#Extra1']
        self.assertEqual(len(cleaned), 5)
        self.assertEqual(cleaned[0], "#AI")
        self.assertEqual(cleaned[1], "#TechNews")
        self.assertEqual(cleaned[2], "#codinglife")
        self.assertEqual(cleaned[3], "#Python3")
        self.assertEqual(cleaned[4], "#Extra1")

    # 9b. Hashtags appear exactly once: 3 inline in caption + 4 in list -> capped at 5, zero duplicates
    def test_hashtags_appear_exactly_once_inline_plus_list(self):
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        # 3 inline tags in caption
        plan["caption"] = "Check out this amazing AI innovation! #tech #AI #innovation"
        plan["platform_text"]["instagram"]["caption"] = "Check out this amazing AI innovation! #tech #AI #innovation"
        # 4 tags in list (overlapping with inline tags)
        plan["hashtags"] = ["#innovation", "#gadgets", "#future", "#ai"]
        # YouTube description with inline tags
        plan["platform_text"]["youtube"]["description"] = "Full video breakdown. #tech #video"

        fake.set_response("content", plan)

        spec = RunSpec(mode="generate", topic="Hashtag Once Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        ig_caption = saved["platform_text"]["instagram"]["caption"]
        # Verify body text has no inline tags
        body = ig_caption.split("\n\n")[0]
        self.assertNotIn("#", body)
        self.assertEqual(body, "Check out this amazing AI innovation!")

        # Verify hashtags are appended once and <= 5
        tags = saved["hashtags"]
        self.assertLessEqual(len(tags), 5)
        self.assertEqual(len(tags), len(set(t.lower() for t in tags)))  # no duplicates

        # Tags in IG caption must match saved["hashtags"] exactly
        tags_line = ig_caption.split("\n\n")[1]
        self.assertEqual(tags_line, " ".join(tags))

        # Check YouTube description
        yt_desc = saved["platform_text"]["youtube"]["description"]
        yt_body = yt_desc.split("\n\n")[0]
        self.assertNotIn("#", yt_body)

    # 10. Platform length limits
    def test_caption_title_description_length_limits(self):
        fake = FakeLLM.get_instance()
        plan = _make_valid_plan()
        plan["caption"] = "A" * 3000
        plan["platform_text"]["instagram"]["caption"] = "B" * 2500
        plan["platform_text"]["youtube"]["title"] = "C" * 150
        plan["platform_text"]["youtube"]["description"] = "D" * 6000

        fake.set_response("content", plan)

        spec = RunSpec(mode="generate", topic="Limits")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(run_dir / "content" / "plan.json", "r", encoding="utf-8") as f:
            saved = json.load(f)

        self.assertLessEqual(len(saved["platform_text"]["instagram"]["caption"]), 2200)
        self.assertLessEqual(len(saved["platform_text"]["youtube"]["title"]), 100)
        self.assertLessEqual(len(saved["platform_text"]["youtube"]["description"]), 5000)

    # 11. "<" ">" removed from YouTube text
    def test_youtube_angle_brackets_removed(self):
        cleaned = sanitize_youtube_text("<Epic> Video <Tutorial> & Guide >")
        self.assertEqual(cleaned, "Epic Video Tutorial & Guide")
        self.assertNotIn("<", cleaned)
        self.assertNotIn(">", cleaned)

    # 12. Duration sum and per-scene bounds
    def test_duration_sum_and_scene_bounds(self):
        fake = FakeLLM.get_instance()
        bad_plan = _make_valid_plan()
        # Scene 1 is 15s (exceeds 8s max), total is 39s
        bad_plan["scenes"][0]["duration_hint_s"] = 15.0

        good_plan = _make_valid_plan()

        calls = 0

        def scripted_call(agent, model, messages, expect_json=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(bad_plan), 10, 20
            return json.dumps(good_plan), 10, 20

        fake.call = scripted_call

        spec = RunSpec(mode="generate", topic="Duration Test")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        self.assertEqual(calls, 2)

    # 13. Output matches README contract fields exactly
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

    # 14. Only writes inside content/ directory
    def test_only_writes_inside_content_dir(self):
        fake = FakeLLM.get_instance()
        fake.set_response("content", _make_valid_plan())

        spec = RunSpec(mode="generate", topic="Isolation")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        agent.run(task)

        # Inspect all created files in run_dir except standard runfolder files
        ignored_standard = {"spec.yaml", "run.json", "events.jsonl"}
        created = [p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_file()]
        extra_files = [p for p in created if p.parts[0] != "content" and str(p) not in ignored_standard]
        self.assertEqual(extra_files, [], f"Found writes outside content/ directory: {extra_files}")

    # 15. Every LLM call goes through core.llm with agent="content"
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

    # 16. Unknown mode case fails with clear error
    def test_unknown_mode_fails_with_clear_error(self):
        spec = RunSpec(mode="teleportation", topic="Sci-Fi")
        _, run_dir, task = self._setup_run(spec)

        agent = ContentAgent(provider="fake")
        result = agent.run(task)

        self.assertEqual(result.status, "failed")
        self.assertIn("Unknown or unsupported mode 'teleportation'", result.error)
        self.assertIn("Allowed modes are", result.error)
        self.assertFalse((run_dir / "content" / "plan.json").exists())

    # 17. Atomic write: no partial/temp files left on failure
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

    # 18. CLI entrypoint invocation: python -m agents.content <run_dir>
    def test_cli_main_invocation(self):
        fake = FakeLLM.get_instance()
        fake.set_response("content", _make_valid_plan())

        spec = RunSpec(mode="generate", topic="CLI Run")
        _, run_dir, _ = self._setup_run(spec)

        with mock.patch("sys.argv", ["agents.content", str(run_dir)]):
            ret = cli_main()

        self.assertEqual(ret, 0)
        self.assertTrue((run_dir / "content" / "plan.json").exists())


if __name__ == "__main__":
    unittest.main()
