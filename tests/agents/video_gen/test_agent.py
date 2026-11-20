"""Unit tests for VideoGenAgent execution, resume, retry, and contracts."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from core.contracts import AgentTask, RunSpec
from core.runfolder import create_run
from core.testing import make_tiny_mp4
from agents.video_gen.agent import VideoGenAgent
from agents.video_gen.probe import probe_video
from agents.video_gen.providers.base import (
    Capabilities,
    CharacterModel,
    ClipInfo,
    VideoProvider,
    register_provider,
)


class TestVideoGenAgent(unittest.TestCase):
    def setUp(self):
        self.agent_tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.agent_tmp_dir.name)

    def tearDown(self):
        self.agent_tmp_dir.cleanup()

    def _setup_run(
        self,
        scenes: list,
        provider: str = "placeholder",
        character_text: str = "A friendly cyberpunk programmer",
    ) -> AgentTask:
        spec = RunSpec(
            mode="generate",
            topic="Coding tutorial",
            character_text=character_text,
            extra={"video": {"provider": provider}},
        )
        run_id = create_run(spec, base_dir=self.base_dir)
        run_dir = self.base_dir / run_id

        # Write content/plan.json
        content_dir = run_dir / "content"
        content_dir.mkdir(parents=True, exist_ok=True)
        plan_data = {
            "title": "Python Tips",
            "scenes": scenes,
        }
        with open(content_dir / "plan.json", "w", encoding="utf-8") as f:
            json.dump(plan_data, f)

        return AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)

    def test_full_run_placeholder_matches_readme_fields_exactly(self):
        """Full run with placeholder engine writes clips.json with exact README schema."""
        scenes = [
            {"id": 1, "narration": "Hello world", "visual_prompt": "Terminal glowing", "duration_hint_s": 2.0},
            {"id": 2, "narration": "Write clean code", "visual_prompt": "Code IDE editor", "duration_hint_s": 3.0},
        ]
        task = self._setup_run(scenes=scenes, provider="placeholder")
        agent = VideoGenAgent()
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        clips_json_path = Path(result.outputs["clips"])
        self.assertTrue(clips_json_path.exists())

        with open(clips_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("clips", data)
        self.assertEqual(len(data["clips"]), 2)

        for clip in data["clips"]:
            # Exact keys required by README.md
            self.assertIn("scene_id", clip)
            self.assertIn("path", clip)
            self.assertIn("duration_s", clip)
            self.assertIn("width", clip)
            self.assertIn("height", clip)
            self.assertIn("provider", clip)

            self.assertEqual(clip["provider"], "placeholder")
            self.assertEqual(clip["width"], 1080)
            self.assertEqual(clip["height"], 1920)
            self.assertGreaterEqual(clip["duration_s"], 1.9)

            # Verify file exists on disk at relative path
            clip_file = Path(task.run_dir) / clip["path"]
            self.assertTrue(clip_file.exists(), f"Clip file not found: {clip_file}")

    def test_resume_skips_existing_valid_clips(self):
        """Resume checks if clip exists + probes OK and skips generation."""
        scenes = [
            {"id": 1, "visual_prompt": "Scene 1", "duration_hint_s": 2.0},
            {"id": 2, "visual_prompt": "Scene 2", "duration_hint_s": 2.0},
        ]
        task = self._setup_run(scenes=scenes, provider="placeholder")
        run_dir = Path(task.run_dir)

        # Pre-create scene 1 clip
        clips_dir = run_dir / "video_gen" / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        scene1_path = clips_dir / "scene_1.mp4"
        make_tiny_mp4(scene1_path, w=1080, h=1920, seconds=2.0)
        mtime_before = scene1_path.stat().st_mtime

        agent = VideoGenAgent()
        result = agent.run(task)
        self.assertEqual(result.status, "ok")

        # Scene 1 was reused: stat mtime should be unchanged
        mtime_after = scene1_path.stat().st_mtime
        self.assertEqual(mtime_before, mtime_after)

        # Both clips in clips.json
        with open(result.outputs["clips"], "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["clips"]), 2)
        self.assertEqual(data["clips"][0]["scene_id"], 1)
        self.assertEqual(data["clips"][1]["scene_id"], 2)

    def test_retry_then_stage_failure_names_the_scene(self):
        """When an engine repeatedly fails on a scene, stage fails naming the scene."""
        class FlakyProvider(VideoProvider):
            def __init__(self):
                super().__init__(name="flaky_test")
                self.calls = 0

            def prepare(self, run_ctx, character):
                pass

            def generate(self, scene, character, out_path, target=None):
                scene_id = scene.get("id", 1)
                if scene_id == 2:
                    self.calls += 1
                    raise RuntimeError("GPU rendering simulated crash")
                make_tiny_mp4(out_path, w=1080, h=1920, seconds=2.0)
                return ClipInfo(scene_id, str(out_path), 2.0, 1080, 1920, self.name)

        register_provider("flaky_test", FlakyProvider)

        scenes = [
            {"id": 1, "visual_prompt": "Scene 1", "duration_hint_s": 2.0},
            {"id": 2, "visual_prompt": "Scene 2", "duration_hint_s": 2.0},
        ]
        task = self._setup_run(scenes=scenes, provider="flaky_test")
        agent = VideoGenAgent()
        result = agent.run(task)

        # Stage failed
        self.assertEqual(result.status, "failed")
        # Names the failed scene specifically
        self.assertIn("scene 2", result.error.lower())
        self.assertIn("attempts", result.error.lower())

        # Scene 1 completed clip remains on disk
        scene1_clip = Path(task.run_dir) / "video_gen" / "clips" / "scene_1.mp4"
        self.assertTrue(scene1_clip.exists(), "Completed scene 1 clip must remain on disk")

    def test_unknown_engine_error_lists_available_engines(self):
        """Unknown engine in spec produces a clear error listing available engines."""
        scenes = [{"id": 1, "visual_prompt": "Prompt"}]
        task = self._setup_run(scenes=scenes, provider="quantum_ai_engine")
        agent = VideoGenAgent()
        result = agent.run(task)

        self.assertEqual(result.status, "failed")
        self.assertIn("Unknown video provider 'quantum_ai_engine'", result.error)
        self.assertIn("placeholder", result.error)
        self.assertIn("claude_code", result.error)

    def test_probe_values_used_not_engine_claims(self):
        """Agent probes actual file with ffprobe/ffmpeg rather than trusting engine metadata."""
        class LyingProvider(VideoProvider):
            def __init__(self):
                super().__init__(name="lying_test")

            def prepare(self, run_ctx, character):
                pass

            def generate(self, scene, character, out_path, target=None):
                # Generates a 64x64 1.0s video
                make_tiny_mp4(out_path, w=64, h=64, seconds=1.0)
                # But falsely claims it's 4K 60 seconds
                return ClipInfo(
                    scene_id=scene.get("id", 1),
                    path=str(out_path),
                    duration_s=60.0,
                    width=3840,
                    height=2160,
                    provider=self.name,
                )

        register_provider("lying_test", LyingProvider)

        scenes = [{"id": 1, "visual_prompt": "Prompt", "duration_hint_s": 2.0}]
        task = self._setup_run(scenes=scenes, provider="lying_test")
        agent = VideoGenAgent()
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(result.outputs["clips"], "r", encoding="utf-8") as f:
            data = json.load(f)

        clip = data["clips"][0]
        # Must reflect the real probed 64x64 dimensions, NOT the engine's 3840x2160 claim
        self.assertEqual(clip["width"], 64)
        self.assertEqual(clip["height"], 64)
        self.assertLess(clip["duration_s"], 2.0)

    def test_string_scene_id_works_end_to_end(self):
        """String scene IDs ('s1', 's2') are preserved exactly in clips.json and output files."""
        scenes = [
            {"id": "s1", "narration": "Intro", "visual_prompt": "Prompt 1", "duration_hint_s": 2.0},
            {"id": "s2", "narration": "Outro", "visual_prompt": "Prompt 2", "duration_hint_s": 2.0},
        ]
        task = self._setup_run(scenes=scenes, provider="placeholder")
        agent = VideoGenAgent()
        result = agent.run(task)

        self.assertEqual(result.status, "ok")
        with open(result.outputs["clips"], "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(len(data["clips"]), 2)
        clip1, clip2 = data["clips"]
        self.assertEqual(clip1["scene_id"], "s1")
        self.assertEqual(clip2["scene_id"], "s2")
        self.assertIn("scene_s1.mp4", clip1["path"])
        self.assertIn("scene_s2.mp4", clip2["path"])
        self.assertTrue((Path(task.run_dir) / clip1["path"]).is_file())
        self.assertTrue((Path(task.run_dir) / clip2["path"]).is_file())

    def test_placeholder_requested_size_and_length(self):
        """PlaceholderProvider generates exact requested width, height, and clamped duration."""
        from agents.video_gen.providers.placeholder import PlaceholderProvider
        provider = PlaceholderProvider()
        out_file = self.base_dir / "custom_placeholder.mp4"
        character = CharacterModel.build(description="Test")
        scene = {"id": "custom", "duration_hint_s": 3.0}

        clip = provider.generate(
            scene=scene,
            character=character,
            out_path=out_file,
            target={"width": 720, "height": 1280, "duration_s": 3.0},
        )
        self.assertEqual(clip.scene_id, "custom")
        self.assertEqual(clip.width, 720)
        self.assertEqual(clip.height, 1280)
        self.assertAlmostEqual(clip.duration_s, 3.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
