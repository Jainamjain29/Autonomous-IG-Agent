"""Unit tests for video probing utilities."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from core.testing import make_tiny_mp4
from agents.video_gen.probe import probe_video


class TestVideoProbe(unittest.TestCase):
    def test_probe_valid_mp4(self):
        with tempfile.TemporaryDirectory() as td:
            video_path = Path(td) / "sample.mp4"
            make_tiny_mp4(video_path, w=1080, h=1920, seconds=2.0)
            self.assertTrue(video_path.exists())

            dur, width, height = probe_video(video_path)
            self.assertEqual(width, 1080)
            self.assertEqual(height, 1920)
            self.assertGreaterEqual(dur, 1.9)
            self.assertLessEqual(dur, 2.5)

    def test_probe_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            probe_video("/nonexistent/video.mp4")

    def test_probe_corrupt_file_raises(self):
        with tempfile.TemporaryDirectory() as td:
            bad_path = Path(td) / "bad.mp4"
            bad_path.write_text("this is not a valid video file", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                probe_video(bad_path)


if __name__ == "__main__":
    unittest.main()
