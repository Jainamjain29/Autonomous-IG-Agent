import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
while repo_root in sys.path:
    sys.path.remove(repo_root)
sys.path.insert(0, repo_root)

import imageio_ffmpeg

from agents.qc.probe import check_mp4_faststart, probe_video


def generate_test_video(
    path: Path,
    *,
    w: int = 1080,
    h: int = 1920,
    duration: float = 3.0,
    fps: int = 30,
    with_audio: bool = True,
    silent_audio: bool = False,
    faststart: bool = True,
) -> Path:
    """Generate a tiny valid MP4 video clip for real media testing."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg_exe,
        "-y",
        "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size={w}x{h}:rate={fps}",
    ]

    if with_audio:
        if silent_audio:
            cmd.extend(["-f", "lavfi", "-i", f"aevalsrc=0:d={duration}"])
        else:
            cmd.extend(["-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration}"])
        cmd.extend(["-c:a", "aac"])

    cmd.extend([
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
    ])

    if faststart:
        cmd.extend(["-movflags", "+faststart"])

    cmd.append(str(path))
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg failed to create test video: {res.stderr}")

    return path


class TestProbeRealMedia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp_dir.name)

        # 1. Standard valid 1080x1920 with sine audio and faststart
        cls.valid_reel = generate_test_video(
            cls.base / "valid_reel.mp4",
            w=1080,
            h=1920,
            duration=3.0,
            with_audio=True,
            silent_audio=False,
            faststart=True,
        )

        # 2. 16:9 landscape video
        cls.landscape_video = generate_test_video(
            cls.base / "landscape.mp4",
            w=1920,
            h=1080,
            duration=3.0,
            with_audio=True,
            faststart=True,
        )

        # 3. No audio video
        cls.no_audio_video = generate_test_video(
            cls.base / "no_audio.mp4",
            w=1080,
            h=1920,
            duration=3.0,
            with_audio=False,
            faststart=True,
        )

        # 4. Silent audio video
        cls.silent_video = generate_test_video(
            cls.base / "silent.mp4",
            w=1080,
            h=1920,
            duration=3.0,
            with_audio=True,
            silent_audio=True,
            faststart=True,
        )

        # 5. Non-faststart video
        cls.no_faststart_video = generate_test_video(
            cls.base / "no_faststart.mp4",
            w=1080,
            h=1920,
            duration=3.0,
            with_audio=True,
            faststart=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp_dir.cleanup()

    def test_probe_valid_reel(self):
        probe = probe_video(self.valid_reel)
        self.assertTrue(probe.file_exists)
        self.assertTrue(probe.readable)
        self.assertEqual(probe.width, 1080)
        self.assertEqual(probe.height, 1920)
        self.assertAlmostEqual(probe.aspect_ratio, 9.0 / 16.0, places=3)
        self.assertIn("h264", probe.video_codec.lower())
        self.assertTrue(probe.has_audio)
        self.assertEqual(probe.audio_codec.lower(), "aac")
        self.assertIsNotNone(probe.mean_volume_db)
        self.assertGreater(probe.mean_volume_db, -60.0)
        self.assertTrue(probe.is_faststart)
        self.assertAlmostEqual(probe.duration_s, 3.0, delta=0.5)

    def test_probe_landscape_video(self):
        probe = probe_video(self.landscape_video)
        self.assertTrue(probe.readable)
        self.assertEqual(probe.width, 1920)
        self.assertEqual(probe.height, 1080)
        self.assertAlmostEqual(probe.aspect_ratio, 16.0 / 9.0, places=3)

    def test_probe_no_audio(self):
        probe = probe_video(self.no_audio_video)
        self.assertTrue(probe.readable)
        self.assertFalse(probe.has_audio)
        self.assertIsNone(probe.mean_volume_db)

    def test_probe_silent_audio(self):
        probe = probe_video(self.silent_video)
        self.assertTrue(probe.readable)
        self.assertTrue(probe.has_audio)
        self.assertIsNotNone(probe.mean_volume_db)
        # In ffmpeg volumedetect, a totally silent signal is -91.0 dB or <= -60 dB
        self.assertLessEqual(probe.mean_volume_db, -60.0)

    def test_faststart_detection(self):
        self.assertTrue(check_mp4_faststart(self.valid_reel))
        self.assertFalse(check_mp4_faststart(self.no_faststart_video))


if __name__ == "__main__":
    unittest.main()
