import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
while repo_root in sys.path:
    sys.path.remove(repo_root)
sys.path.insert(0, repo_root)

from agents.qc.checks.video import (
    check_aspect_ratio,
    check_audio,
    check_codecs_and_container,
    check_duration,
    check_file_size,
    check_resolution,
    check_video_readable,
)
from agents.qc.probe import ProbeData


class TestVideoChecks(unittest.TestCase):
    def test_video_readable_pass_and_fail(self):
        probe_ok = ProbeData(file_exists=True, readable=True, width=1080, height=1920, duration_s=30.0)
        res = check_video_readable(probe_ok)
        self.assertTrue(res["passed"])
        self.assertEqual(res["severity"], "blocker")

        probe_missing = ProbeData(file_exists=False, readable=False, error="File not found")
        res2 = check_video_readable(probe_missing)
        self.assertFalse(res2["passed"])
        self.assertEqual(res2["severity"], "blocker")

        probe_unreadable = ProbeData(file_exists=True, readable=False, error="Corrupt header")
        res3 = check_video_readable(probe_unreadable)
        self.assertFalse(res3["passed"])

    def test_aspect_ratio(self):
        # 1080x1920 is exactly 9:16 (0.5625)
        probe = ProbeData(width=1080, height=1920, aspect_ratio=1080 / 1920)
        res = check_aspect_ratio(probe)
        self.assertTrue(res["passed"])
        self.assertEqual(res["severity"], "blocker")

        # 9:16 + 0.8% tolerance -> pass
        probe_tight_high = ProbeData(width=1080, height=1906, aspect_ratio=1080 / 1906)
        self.assertTrue(check_aspect_ratio(probe_tight_high)["passed"])

        # 9:16 - 0.8% tolerance -> pass
        probe_tight_low = ProbeData(width=1080, height=1935, aspect_ratio=1080 / 1935)
        self.assertTrue(check_aspect_ratio(probe_tight_low)["passed"])

        # 9:16 + 2.0% tolerance -> fail
        probe_wide = ProbeData(width=1080, height=1880, aspect_ratio=1080 / 1880)
        self.assertFalse(check_aspect_ratio(probe_wide)["passed"])

        # 16:9 landscape -> fail
        probe_landscape = ProbeData(width=1920, height=1080, aspect_ratio=1920 / 1080)
        self.assertFalse(check_aspect_ratio(probe_landscape)["passed"])

    def test_resolution_minimum_and_standard(self):
        # 1080x1920: passes minimum and standard
        probe_std = ProbeData(width=1080, height=1920)
        checks = check_resolution(probe_std, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks}
        self.assertTrue(by_name["video_resolution_minimum"]["passed"])
        self.assertTrue(by_name["video_resolution_standard"]["passed"])

        # 720x1280: passes minimum, warns on not standard
        probe_720 = ProbeData(width=720, height=1280)
        checks_720 = check_resolution(probe_720, platforms=["instagram"])
        by_name_720 = {c["name"]: c for c in checks_720}
        self.assertTrue(by_name_720["video_resolution_minimum"]["passed"])
        self.assertFalse(by_name_720["video_resolution_standard"]["passed"])
        self.assertEqual(by_name_720["video_resolution_standard"]["severity"], "warn")

        # 719x1280: fails minimum blocker
        probe_low_w = ProbeData(width=719, height=1280)
        checks_low_w = check_resolution(probe_low_w, platforms=["instagram"])
        by_name_low_w = {c["name"]: c for c in checks_low_w}
        self.assertFalse(by_name_low_w["video_resolution_minimum"]["passed"])
        self.assertEqual(by_name_low_w["video_resolution_minimum"]["severity"], "blocker")

        # YouTube platform: above 1080p warning
        probe_2k = ProbeData(width=1440, height=2560)
        checks_yt = check_resolution(probe_2k, platforms=["youtube"])
        by_name_yt = {c["name"]: c for c in checks_yt}
        self.assertFalse(by_name_yt["video_resolution_youtube_1080p"]["passed"])
        self.assertEqual(by_name_yt["video_resolution_youtube_1080p"]["severity"], "warn")

    def test_codecs_and_container(self):
        # Valid H.264, 30fps, faststart
        probe_ok = ProbeData(video_codec="h264", fps=30.0, is_faststart=True)
        checks = check_codecs_and_container(probe_ok, platforms=["instagram"])
        for c in checks:
            self.assertTrue(c["passed"], f"{c['name']} failed")

        # HEVC/H.265 supported
        probe_hevc = ProbeData(video_codec="hevc", fps=24.0, is_faststart=True)
        checks_hevc = check_codecs_and_container(probe_hevc, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks_hevc}
        self.assertTrue(by_name["video_codec_instagram"]["passed"])

        # Invalid codec VP9
        probe_vp9 = ProbeData(video_codec="vp9", fps=30.0, is_faststart=True)
        checks_vp9 = check_codecs_and_container(probe_vp9, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks_vp9}
        self.assertFalse(by_name["video_codec_instagram"]["passed"])

        # Fps boundaries
        probe_fps_low = ProbeData(video_codec="h264", fps=20.0, is_faststart=True)
        self.assertFalse(check_codecs_and_container(probe_fps_low, platforms=["instagram"])[1]["passed"])

        probe_fps_23_98 = ProbeData(video_codec="h264", fps=23.976, is_faststart=True)
        self.assertTrue(check_codecs_and_container(probe_fps_23_98, platforms=["instagram"])[1]["passed"])

        probe_fps_high = ProbeData(video_codec="h264", fps=65.0, is_faststart=True)
        self.assertFalse(check_codecs_and_container(probe_fps_high, platforms=["instagram"])[1]["passed"])

        # Faststart False
        probe_no_fs = ProbeData(video_codec="h264", fps=30.0, is_faststart=False)
        checks_no_fs = check_codecs_and_container(probe_no_fs, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks_no_fs}
        self.assertFalse(by_name["video_faststart_instagram"]["passed"])

    def test_duration_checks(self):
        # 30s video passes Instagram and YouTube and target
        probe = ProbeData(duration_s=30.0)
        checks = check_duration(probe, platforms=["instagram", "youtube"], sync_merge_duration=30.2)
        by_name = {c["name"]: c for c in checks}
        self.assertTrue(by_name["duration_instagram"]["passed"])
        self.assertTrue(by_name["duration_youtube"]["passed"])
        self.assertTrue(by_name["duration_target_range"]["passed"])
        self.assertTrue(by_name["duration_sync_merge_drift"]["passed"])

        # Instagram < 3s fails blocker
        probe_short = ProbeData(duration_s=2.5)
        res = check_duration(probe_short, platforms=["instagram"])[0]
        self.assertFalse(res["passed"])
        self.assertEqual(res["severity"], "blocker")

        # Instagram > 15 min fails blocker
        probe_long = ProbeData(duration_s=905.0)
        res = check_duration(probe_long, platforms=["instagram"])[0]
        self.assertFalse(res["passed"])

        # YouTube Shorts > 180s fails blocker
        probe_yt_long = ProbeData(duration_s=185.0)
        checks_yt = check_duration(probe_yt_long, platforms=["youtube"])
        by_name = {c["name"]: c for c in checks_yt}
        self.assertFalse(by_name["duration_youtube"]["passed"])
        self.assertEqual(by_name["duration_youtube"]["severity"], "blocker")

        # Target range warnings (10s and 100s)
        checks_target_10 = check_duration(ProbeData(duration_s=10.0), platforms=["instagram"])
        by_name_10 = {c["name"]: c for c in checks_target_10}
        self.assertFalse(by_name_10["duration_target_range"]["passed"])
        self.assertEqual(by_name_10["duration_target_range"]["severity"], "warn")

        checks_target_100 = check_duration(ProbeData(duration_s=100.0), platforms=["instagram"])
        by_name_100 = {c["name"]: c for c in checks_target_100}
        self.assertFalse(by_name_100["duration_target_range"]["passed"])

        # Drift > 0.5s warns
        checks_drift = check_duration(ProbeData(duration_s=30.0), platforms=["instagram"], sync_merge_duration=30.8)
        by_name_drift = {c["name"]: c for c in checks_drift}
        self.assertFalse(by_name_drift["duration_sync_merge_drift"]["passed"])
        self.assertEqual(by_name_drift["duration_sync_merge_drift"]["severity"], "warn")

        # Drift in existing video mode skips
        checks_exist = check_duration(ProbeData(duration_s=30.0), platforms=["instagram"], is_existing_video=True)
        by_name_exist = {c["name"]: c for c in checks_exist}
        self.assertTrue(by_name_exist["duration_sync_merge_drift"]["passed"])
        self.assertIn("skipped", by_name_exist["duration_sync_merge_drift"]["detail"])

    def test_audio_checks(self):
        # Audio present, AAC, -20 dB -> all pass
        probe_ok = ProbeData(has_audio=True, audio_codec="aac", mean_volume_db=-20.0)
        checks = check_audio(probe_ok, platforms=["instagram"])
        for c in checks:
            self.assertTrue(c["passed"], f"{c['name']} failed")

        # Missing audio stream -> blocker fail
        probe_no_audio = ProbeData(has_audio=False)
        checks_no_audio = check_audio(probe_no_audio, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks_no_audio}
        self.assertFalse(by_name["audio_stream_present"]["passed"])
        self.assertEqual(by_name["audio_stream_present"]["severity"], "blocker")
        self.assertFalse(by_name["audio_volume_level"]["passed"])

        # Silent audio (-65 dB) -> blocker fail
        probe_silent = ProbeData(has_audio=True, audio_codec="aac", mean_volume_db=-65.0)
        checks_silent = check_audio(probe_silent, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks_silent}
        self.assertFalse(by_name["audio_volume_level"]["passed"])
        self.assertEqual(by_name["audio_volume_level"]["severity"], "blocker")

        # Boundary volume check (-59.9 dB passes, -60.1 dB fails)
        probe_edge_pass = ProbeData(has_audio=True, audio_codec="aac", mean_volume_db=-59.9)
        self.assertTrue(check_audio(probe_edge_pass, platforms=["instagram"])[1]["passed"])

        probe_edge_fail = ProbeData(has_audio=True, audio_codec="aac", mean_volume_db=-60.1)
        self.assertFalse(check_audio(probe_edge_fail, platforms=["instagram"])[1]["passed"])

        # Audio codec not AAC on Instagram -> warn fail
        probe_mp3 = ProbeData(has_audio=True, audio_codec="mp3", mean_volume_db=-18.0)
        checks_mp3 = check_audio(probe_mp3, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks_mp3}
        self.assertFalse(by_name["audio_codec_instagram"]["passed"])
        self.assertEqual(by_name["audio_codec_instagram"]["severity"], "warn")

    def test_file_size_checks(self):
        # 50 MB passes
        probe_50mb = ProbeData(file_size_bytes=50 * 1024 * 1024)
        res = check_file_size(probe_50mb, platforms=["instagram"])[0]
        self.assertTrue(res["passed"])
        self.assertEqual(res["severity"], "blocker")

        # 301 MB fails blocker
        probe_301mb = ProbeData(file_size_bytes=301 * 1024 * 1024)
        res = check_file_size(probe_301mb, platforms=["instagram"])[0]
        self.assertFalse(res["passed"])
        self.assertEqual(res["severity"], "blocker")


if __name__ == "__main__":
    unittest.main()
