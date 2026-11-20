"""Unit and integration tests for ClaudeCodeProvider engine."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import core.llm.gateway as gateway
from core.testing import FakeLLM
from agents.video_gen.probe import probe_video
from agents.video_gen.providers.base import CharacterModel
from agents.video_gen.providers.claude_code import ClaudeCodeProvider


VALID_DESIGN_KIT = {
    "palette": {
        "primary": "#3b82f6",
        "secondary": "#1d4ed8",
        "accent": "#f59e0b",
        "background": "#0f172a",
        "text": "#f8fafc",
    },
    "fonts": {
        "heading": "system-ui, -apple-system, sans-serif",
        "body": "system-ui, -apple-system, sans-serif",
    },
    "character_svg": "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='40' fill='#3b82f6'/></svg>",
}

VALID_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {
      margin: 0;
      padding: 0;
      width: 1080px;
      height: 1920px;
      background: #0f172a;
      color: #f8fafc;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      font-family: system-ui, sans-serif;
    }
    #box {
      width: 300px;
      height: 300px;
      background: #3b82f6;
      border-radius: 20px;
    }
  </style>
</head>
<body>
  <div id="box"></div>
  <script>
    window.renderFrame = function(t) {
      var box = document.getElementById('box');
      if (box) {
        box.style.transform = 'rotate(' + (t * 45) + 'deg)';
      }
    };
  </script>
</body>
</html>"""


class TestClaudeCodeProvider(unittest.TestCase):
    def setUp(self):
        FakeLLM.reset()
        self.fake = FakeLLM.get_instance()
        self._orig_config = gateway._AGENTS_CONFIG_CACHE
        gateway._AGENTS_CONFIG_CACHE = {
            "defaults": {"provider": "fake", "model": "fake-model"},
            "agents": {
                "video_gen": {
                    "provider": "fake",
                    "model": "fake-model",
                    "backup": {"provider": "fake", "model": "fake-backup-model"},
                }
            },
        }
        self.claude_tmp_dir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.claude_tmp_dir.name)
        self.agent_dir = self.run_dir / "video_gen"
        self.agent_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.claude_tmp_dir.cleanup()
        gateway._AGENTS_CONFIG_CACHE = self._orig_config
        FakeLLM.reset()

    def test_real_render_to_mp4_with_render_frame(self):
        """FakeLLM returns valid HTML with renderFrame -> real render to short 1080x1920 MP4."""
        # 1. Script prepare response
        self.fake.set_response("video_gen", VALID_DESIGN_KIT)

        provider = ClaudeCodeProvider()
        character = CharacterModel.build(description="Friendly tech robot")
        provider.prepare(run_ctx={"run_dir": str(self.run_dir), "agent_dir": self.agent_dir}, character=character)

        # 2. Script scene generation HTML
        self.fake.set_response("video_gen", {"html": VALID_HTML})

        out_mp4 = self.agent_dir / "clips" / "scene_1.mp4"
        scene = {"id": 1, "visual_prompt": "Rotating robot interface", "duration_hint_s": 2.0}

        # Fast render in test: 5 FPS, 2.0s, real 1080x1920
        clip_info = provider.generate(
            scene=scene,
            character=character,
            out_path=out_mp4,
            target={"fps": 5, "duration_s": 2.0, "width": 1080, "height": 1920},
        )

        self.assertTrue(out_mp4.exists())
        self.assertEqual(clip_info.scene_id, 1)
        self.assertEqual(clip_info.provider, "claude_code")
        self.assertEqual(clip_info.width, 1080)
        self.assertEqual(clip_info.height, 1920)
        self.assertGreaterEqual(clip_info.duration_s, 1.9)

        # Verify scenes HTML was saved
        scene_html_file = self.agent_dir / "scenes" / "1.html"
        self.assertTrue(scene_html_file.exists())

    def test_network_requests_blocked_in_sandbox(self):
        """Page requesting an external URL has requests aborted by Playwright route handler."""
        html_with_network = """<!DOCTYPE html>
<html>
<head>
  <style>body { margin: 0; width: 1080px; height: 1920px; background: #111; }</style>
</head>
<body>
  <img src="https://example.com/blocked_image.png" alt="Blocked External">
  <script>
    window.renderFrame = function(t) {};
  </script>
</body>
</html>"""
        self.fake.set_response("video_gen", VALID_DESIGN_KIT)
        provider = ClaudeCodeProvider()
        character = CharacterModel.build(description="Explorer")
        provider.prepare(run_ctx={"run_dir": str(self.run_dir), "agent_dir": self.agent_dir}, character=character)

        self.fake.set_response("video_gen", {"html": html_with_network})
        out_mp4 = self.agent_dir / "clips" / "scene_net.mp4"
        scene = {"id": 1, "visual_prompt": "Network test", "duration_hint_s": 2.0}

        # Renders without error because network requests are safely intercepted/aborted
        clip_info = provider.generate(
            scene=scene,
            character=character,
            out_path=out_mp4,
            target={"fps": 5, "duration_s": 2.0, "width": 1080, "height": 1920},
        )
        self.assertTrue(out_mp4.exists())
        self.assertEqual(clip_info.width, 1080)

    def test_missing_render_frame_retries_once_with_error_then_fails(self):
        """If renderFrame is missing and retry also lacks renderFrame, scene fails."""
        invalid_html = "<html><body><h1>No renderFrame here</h1></body></html>"

        self.fake.set_response("video_gen", VALID_DESIGN_KIT)
        provider = ClaudeCodeProvider()
        character = CharacterModel.build(description="Artist")
        provider.prepare(run_ctx={"run_dir": str(self.run_dir), "agent_dir": self.agent_dir}, character=character)

        # Both attempts return invalid HTML
        self.fake.set_response("video_gen", {"html": invalid_html})
        out_mp4 = self.agent_dir / "clips" / "scene_fail.mp4"
        scene = {"id": 42, "visual_prompt": "Broken animation", "duration_hint_s": 2.0}

        with self.assertRaises(RuntimeError) as ctx:
            provider.generate(
                scene=scene,
                character=character,
                out_path=out_mp4,
                target={"fps": 5, "duration_s": 2.0},
            )

        self.assertIn("Scene 42", str(ctx.exception))
        self.assertIn("window.renderFrame", str(ctx.exception))

        # Recorded calls must show the retry happened
        video_gen_calls = [c for c in self.fake.recorded_calls if c["agent"] == "video_gen"]
        # 1 prepare + 1 initial generate + 1 retry = 3 calls
        self.assertEqual(len(video_gen_calls), 3)

    def test_missing_render_frame_recovers_on_retry(self):
        """If initial HTML lacks renderFrame but retry corrects it, generation succeeds."""
        self.fake.set_response("video_gen", VALID_DESIGN_KIT)
        provider = ClaudeCodeProvider()
        character = CharacterModel.build(description="Scientist")
        provider.prepare(run_ctx={"run_dir": str(self.run_dir), "agent_dir": self.agent_dir}, character=character)

        # Call sequence for FakeLLM:
        # 1. Invalid HTML
        # 2. Valid HTML on retry
        call_count = [0]
        original_call = self.fake.call

        def dynamic_call(agent, model, messages, expect_json=False):
            if agent == "video_gen":
                call_count[0] += 1
                # Check if it is a scene retry message (has assistant role in history)
                has_assistant_msg = any(m.get("role") == "assistant" for m in messages)
                if has_assistant_msg:
                    return json.dumps({"html": VALID_HTML}), 10, 10
                else:
                    return json.dumps({"html": "<html><body>Missing renderFrame</body></html>"}), 10, 10
            return original_call(agent, model, messages, expect_json=expect_json)

        self.fake.call = dynamic_call

        out_mp4 = self.agent_dir / "clips" / "scene_recover.mp4"
        scene = {"id": 7, "visual_prompt": "Recovery test", "duration_hint_s": 2.0}

        clip_info = provider.generate(
            scene=scene,
            character=character,
            out_path=out_mp4,
            target={"fps": 5, "duration_s": 2.0},
        )
        self.assertTrue(out_mp4.exists())
        self.assertEqual(clip_info.scene_id, 7)

    def test_design_kit_and_character_created_once_and_reused(self):
        """prepare() is executed once per run, writing kit files and reusing them."""
        self.fake.set_response("video_gen", VALID_DESIGN_KIT)
        provider = ClaudeCodeProvider()
        character = CharacterModel.build(description="Chef")
        run_ctx = {"run_dir": str(self.run_dir), "agent_dir": self.agent_dir}

        provider.prepare(run_ctx=run_ctx, character=character)

        kit_path = self.agent_dir / "design_kit.json"
        svg_path = self.agent_dir / "character.svg"
        self.assertTrue(kit_path.exists())
        self.assertTrue(svg_path.exists())

        # Calling prepare again with existing files does not make another LLM call
        call_count_before = len(self.fake.recorded_calls)
        provider2 = ClaudeCodeProvider()
        provider2.prepare(run_ctx=run_ctx, character=character)
        self.assertEqual(len(self.fake.recorded_calls), call_count_before)

    def test_character_slot_replaced_with_identical_content_across_two_scenes(self):
        """{{CHARACTER}} slot token is replaced with identical content across multiple scenes."""
        self.fake.set_response("video_gen", VALID_DESIGN_KIT)
        provider = ClaudeCodeProvider()
        character = CharacterModel.build(description="Friendly robot")
        run_ctx = {"run_dir": str(self.run_dir), "agent_dir": self.agent_dir}
        provider.prepare(run_ctx=run_ctx, character=character)

        # HTML template containing {{CHARACTER}} slot
        html_slot_scene1 = VALID_HTML.replace('<div id="box"></div>', '<div id="box">{{CHARACTER}}</div>')
        html_slot_scene2 = VALID_HTML.replace('<div id="box"></div>', '<div id="box" class="scene2">{{CHARACTER}}</div>')

        # Generate scene 1
        self.fake.set_response("video_gen", {"html": html_slot_scene1})
        out1 = self.agent_dir / "clips" / "scene_1.mp4"
        provider.generate(
            scene={"id": 1, "visual_prompt": "Scene 1", "duration_hint_s": 2.0},
            character=character,
            out_path=out1,
            target={"fps": 5, "duration_s": 2.0},
        )

        # Generate scene 2
        self.fake.set_response("video_gen", {"html": html_slot_scene2})
        out2 = self.agent_dir / "clips" / "scene_2.mp4"
        provider.generate(
            scene={"id": 2, "visual_prompt": "Scene 2", "duration_hint_s": 2.0},
            character=character,
            out_path=out2,
            target={"fps": 5, "duration_s": 2.0},
        )

        # Verify saved scene HTML files
        scenes_dir = self.agent_dir / "scenes"
        html1_text = (scenes_dir / "1.html").read_text(encoding="utf-8")
        html2_text = (scenes_dir / "2.html").read_text(encoding="utf-8")

        self.assertNotIn("{{CHARACTER}}", html1_text)
        self.assertNotIn("{{CHARACTER}}", html2_text)
        self.assertIn(VALID_DESIGN_KIT["character_svg"], html1_text)
        self.assertIn(VALID_DESIGN_KIT["character_svg"], html2_text)

    def test_no_base64_or_svg_in_scene_prompt_messages(self):
        """Scene prompt messages contain character description and viewBox, but NO raw SVG or base64 bytes."""
        self.fake.set_response("video_gen", VALID_DESIGN_KIT)
        provider = ClaudeCodeProvider()
        character = CharacterModel.build(description="Teal Visor Robot")
        run_ctx = {"run_dir": str(self.run_dir), "agent_dir": self.agent_dir}
        provider.prepare(run_ctx=run_ctx, character=character)

        self.fake.set_response("video_gen", {"html": VALID_HTML})
        out1 = self.agent_dir / "clips" / "scene_inspect.mp4"
        provider.generate(
            scene={"id": 1, "visual_prompt": "Inspector prompt", "duration_hint_s": 2.0},
            character=character,
            out_path=out1,
            target={"fps": 5, "duration_s": 2.0},
        )

        # Inspect recorded calls
        scene_calls = [c for c in self.fake.recorded_calls if any("Scene ID:" in m.get("content", "") for m in c.get("messages", []))]
        self.assertTrue(len(scene_calls) >= 1)
        for call in scene_calls:
            for msg in call["messages"]:
                content = msg.get("content", "")
                self.assertNotIn("<svg", content)
                self.assertNotIn("data:image", content)
                self.assertNotIn("base64", content)
            # Ensure character description and slot contract instruction are present
            user_msg = next(m["content"] for m in call["messages"] if m["role"] == "user")
            self.assertIn("Teal Visor Robot", user_msg)
            self.assertIn("{{CHARACTER}}", user_msg)

    def test_watchdog_fires_on_infinite_loop_render_frame(self):
        """Watchdog closes browser and terminates stuck renderFrame with clear 'render timed out' error."""
        self.fake.set_response("video_gen", VALID_DESIGN_KIT)
        # Use a short timeout of 1.0 second for test speed
        provider = ClaudeCodeProvider(render_timeout_s=1.0)
        character = CharacterModel.build(description="Robot")
        run_ctx = {"run_dir": str(self.run_dir), "agent_dir": self.agent_dir}
        provider.prepare(run_ctx=run_ctx, character=character)

        infinite_loop_html = """<!DOCTYPE html>
<html>
<head>
  <script>
    window.renderFrame = function(t) {
      var start = Date.now();
      while (Date.now() - start < 30000) {}
    };
  </script>
</head>
<body>Infinite loop</body>
</html>"""

        self.fake.set_response("video_gen", {"html": infinite_loop_html})
        out_mp4 = self.agent_dir / "clips" / "scene_timeout.mp4"

        with self.assertRaises(RuntimeError) as ctx:
            provider.generate(
                scene={"id": "timeout_scene", "visual_prompt": "Hang", "duration_hint_s": 2.0},
                character=character,
                out_path=out_mp4,
                target={"fps": 5, "duration_s": 2.0},
            )
        self.assertIn("render timed out", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
