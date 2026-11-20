"""Unit tests for Google Flow provider with mocked Playwright."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from core.testing import make_tiny_mp4
from agents.video_gen.providers import flow_selectors as sel
from agents.video_gen.providers.base import CharacterModel
from agents.video_gen.providers.flow import FlowProvider


class TestFlowProvider(unittest.TestCase):
    def setUp(self):
        self.flow_tmp_dir = tempfile.TemporaryDirectory()
        self.profile_dir = Path(self.flow_tmp_dir.name) / "flow_profile"

    def tearDown(self):
        self.flow_tmp_dir.cleanup()

    def test_selectors_configured(self):
        """Ensure all required DOM selectors are defined in flow_selectors."""
        self.assertTrue(sel.FLOW_URL.startswith("https://"))
        self.assertIn("textarea", sel.PROMPT_TEXTAREA)
        self.assertIn("Generate", sel.GENERATE_BUTTON)
        self.assertIn("Download", sel.DOWNLOAD_BUTTON)
        self.assertTrue(sel.ASPECT_RATIO_SELECTOR)
        self.assertTrue(sel.ASPECT_RATIO_9_16_OPTION)
        self.assertTrue(sel.INGREDIENT_FILE_INPUT)

    def test_prompt_building_includes_character_block(self):
        """Prompt combines locked character block with visual prompt."""
        provider = FlowProvider()
        character = CharacterModel.build(description="Cyberpunk detective with neon coat")
        scene = {"id": 1, "visual_prompt": "Walking down rainy alleyway"}

        prompt = provider._build_prompt(scene=scene, character=character)
        self.assertIn("[CHARACTER SPECIFICATION]", prompt)
        self.assertIn("Cyberpunk detective with neon coat", prompt)
        self.assertIn("Scene Prompt: Walking down rainy alleyway", prompt)

    def test_unauthenticated_profile_raises_login_message(self):
        """Missing or empty profile directory raises instruction to run the login helper."""
        provider = FlowProvider()
        provider.profile_dir = self.profile_dir  # Empty/non-existent
        character = CharacterModel.build(description="Test")

        with self.assertRaises(RuntimeError) as ctx:
            provider.prepare(run_ctx={}, character=character)
        self.assertIn("python -m agents.video_gen.providers.flow --login", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            provider.generate(scene={"id": 1}, character=character, out_path=Path("out.mp4"))
        self.assertIn("python -m agents.video_gen.providers.flow --login", str(ctx.exception))

    @mock.patch("agents.video_gen.providers.flow.sync_playwright")
    def test_mocked_playwright_flow_success(self, mock_playwright):
        """Mocked Playwright automation executes navigation, prompt fill, generate, and download."""
        # Initialize profile dir with dummy file so it passes authentication check
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "Default").mkdir()

        out_mp4 = Path(self.flow_tmp_dir.name) / "output.mp4"

        # Setup Playwright mocks
        mock_p = mock.MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_p

        mock_context = mock.MagicMock()
        mock_p.chromium.launch_persistent_context.return_value = mock_context

        mock_page = mock.MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_page.is_visible.return_value = True

        # Mock download context manager
        mock_download = mock.MagicMock()
        def fake_save_as(dest):
            # Write tiny valid mp4 to dest
            make_tiny_mp4(dest, w=1080, h=1920, seconds=2.0)
        mock_download.save_as.side_effect = fake_save_as

        mock_dl_info = mock.MagicMock()
        mock_dl_info.value = mock_download
        mock_page.expect_download.return_value.__enter__.return_value = mock_dl_info

        provider = FlowProvider()
        provider.profile_dir = self.profile_dir
        character = CharacterModel.build(description="Robot")
        scene = {"id": 1, "visual_prompt": "Futuristic city"}

        clip_info = provider.generate(
            scene=scene,
            character=character,
            out_path=out_mp4,
        )

        self.assertEqual(clip_info.scene_id, 1)
        self.assertEqual(clip_info.provider, "flow")
        self.assertEqual(clip_info.width, 1080)
        self.assertEqual(clip_info.height, 1920)

        # Verify selector interactions
        mock_page.goto.assert_called_with(sel.FLOW_URL, timeout=60000)
        mock_page.fill.assert_called_with(sel.PROMPT_TEXTAREA, mock.ANY)
        mock_page.click.assert_any_call(sel.GENERATE_BUTTON)

    @mock.patch("agents.video_gen.providers.flow.sync_playwright")
    def test_mocked_playwright_timeout_raises_timeout_error(self, mock_playwright):
        """Playwright timeout during generation raises TimeoutError."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "Default").mkdir()

        mock_p = mock.MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_p
        mock_context = mock.MagicMock()
        mock_p.chromium.launch_persistent_context.return_value = mock_context
        mock_page = mock.MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_page.is_visible.return_value = True

        mock_page.wait_for_selector.side_effect = PlaywrightTimeoutError("Download button timed out")

        provider = FlowProvider()
        provider.profile_dir = self.profile_dir
        character = CharacterModel.build(description="Robot")
        scene = {"id": 1, "visual_prompt": "Scene"}

        out_mp4 = Path(self.flow_tmp_dir.name) / "fail.mp4"
        with self.assertRaises(TimeoutError):
            provider.generate(scene=scene, character=character, out_path=out_mp4)

    @mock.patch("agents.video_gen.providers.flow.sync_playwright")
    def test_flow_reference_image_upload_and_download_rename(self, mock_playwright):
        """Flow uploads reference image via file input and saves downloaded video to target out_path."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "Default").mkdir()

        # Create dummy reference image
        ref_image = Path(self.flow_tmp_dir.name) / "character_ref.png"
        ref_image.write_bytes(b"\x89PNG\r\n\x1a\nfakeimage")

        out_mp4 = Path(self.flow_tmp_dir.name) / "renamed_clip.mp4"

        # Mock Playwright
        mock_p = mock.MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_p
        mock_context = mock.MagicMock()
        mock_p.chromium.launch_persistent_context.return_value = mock_context
        mock_page = mock.MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_page.is_visible.return_value = True

        mock_download = mock.MagicMock()
        def fake_save_as(dest):
            make_tiny_mp4(dest, w=1080, h=1920, seconds=2.0)
        mock_download.save_as.side_effect = fake_save_as

        mock_dl_info = mock.MagicMock()
        mock_dl_info.value = mock_download
        mock_page.expect_download.return_value.__enter__.return_value = mock_dl_info

        provider = FlowProvider()
        provider.profile_dir = self.profile_dir
        character = CharacterModel.build(description="Robot", reference_image=str(ref_image))
        scene = {"id": "s1", "visual_prompt": "Scene with image reference"}

        clip_info = provider.generate(
            scene=scene,
            character=character,
            out_path=out_mp4,
        )

        self.assertEqual(clip_info.scene_id, "s1")
        # Verify set_input_files was called with ingredient selector and resolved image path
        mock_page.set_input_files.assert_called_with(
            sel.INGREDIENT_FILE_INPUT,
            str(ref_image.resolve()),
        )
        # Verify download.save_as was called with target out_mp4
        mock_download.save_as.assert_called_with(str(out_mp4))
        self.assertTrue(out_mp4.is_file())


if __name__ == "__main__":
    unittest.main()
