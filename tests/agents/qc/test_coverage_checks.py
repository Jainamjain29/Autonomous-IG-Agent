import sys
import tempfile
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
while repo_root in sys.path:
    sys.path.remove(repo_root)
sys.path.insert(0, repo_root)

from agents.qc.checks.coverage import (
    check_narration_coverage,
    check_scene_timings,
    check_subtitles,
    parse_srt,
)


class TestCoverageChecks(unittest.TestCase):
    def test_srt_parsing_and_cue_coverage(self):
        sample_srt = """1
00:00:00,000 --> 00:00:04,500
First line of speech

2
00:00:04,500 --> 00:00:09,000
Second line of speech
"""
        cues = parse_srt(sample_srt)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start_s, 0.0)
        self.assertEqual(cues[0].end_s, 4.5)
        self.assertEqual(cues[1].start_s, 4.5)
        self.assertEqual(cues[1].end_s, 9.0)

        with tempfile.TemporaryDirectory() as tmp:
            srt_path = Path(tmp) / "subtitles.srt"
            srt_path.write_text(sample_srt, encoding="utf-8")

            # 9.0s covered over 10.0s video -> 90% coverage (passes 80% threshold)
            checks, srt_text = check_subtitles(srt_path, video_duration=10.0)
            by_name = {c["name"]: c for c in checks}
            self.assertTrue(by_name["subtitles_validity"]["passed"])
            self.assertEqual(by_name["subtitles_validity"]["severity"], "blocker")
            self.assertTrue(by_name["subtitles_cue_coverage"]["passed"])
            self.assertEqual(by_name["subtitles_cue_coverage"]["severity"], "warn")
            self.assertIn("First line", srt_text)

            # 9.0s covered over 20.0s video -> 45% coverage (warns)
            checks_low, _ = check_subtitles(srt_path, video_duration=20.0)
            by_name_low = {c["name"]: c for c in checks_low}
            self.assertTrue(by_name_low["subtitles_validity"]["passed"])
            self.assertFalse(by_name_low["subtitles_cue_coverage"]["passed"])
            self.assertEqual(by_name_low["subtitles_cue_coverage"]["severity"], "warn")

    def test_srt_missing_and_corrupt(self):
        # Missing file in normal mode -> blocker fail
        checks, _ = check_subtitles("/nonexistent/file.srt", video_duration=10.0, is_existing_video=False)
        self.assertFalse(checks[0]["passed"])
        self.assertEqual(checks[0]["severity"], "blocker")

        # Missing file in existing video mode -> warn fail only
        checks_ev, _ = check_subtitles("/nonexistent/file.srt", video_duration=10.0, is_existing_video=True)
        self.assertFalse(checks_ev[0]["passed"])
        self.assertEqual(checks_ev[0]["severity"], "warn")

        # Empty file
        with tempfile.TemporaryDirectory() as tmp:
            empty_srt = Path(tmp) / "empty.srt"
            empty_srt.write_text("", encoding="utf-8")
            checks_empty, _ = check_subtitles(empty_srt, video_duration=10.0)
            self.assertFalse(checks_empty[0]["passed"])
            self.assertEqual(checks_empty[0]["severity"], "blocker")

    def test_scene_timings_validity_and_continuity(self):
        plan_scenes = [
            {"id": 1, "narration": "Scene one"},
            {"id": 2, "narration": "Scene two"},
            {"id": 3, "narration": "Scene three"},
        ]
        scene_timings = [
            {"scene_id": 1, "start_s": 0.0, "end_s": 3.0},
            {"scene_id": 2, "start_s": 3.0, "end_s": 6.0},
            {"scene_id": 3, "start_s": 6.0, "end_s": 9.0},
        ]
        # Normal pass
        checks = check_scene_timings(plan_scenes, scene_timings, video_duration=9.0)
        by_name = {c["name"]: c for c in checks}
        self.assertTrue(by_name["scene_timings_validity"]["passed"])
        self.assertEqual(by_name["scene_timings_validity"]["severity"], "blocker")
        self.assertTrue(by_name["scene_timings_continuity"]["passed"])
        self.assertEqual(by_name["scene_timings_continuity"]["severity"], "warn")

        # Missing scene_id in timings -> blocker fail
        timings_missing = [
            {"scene_id": 1, "start_s": 0.0, "end_s": 3.0},
            {"scene_id": 2, "start_s": 3.0, "end_s": 6.0},
        ]
        checks_missing = check_scene_timings(plan_scenes, timings_missing, video_duration=9.0)
        self.assertFalse(checks_missing[0]["passed"])
        self.assertEqual(checks_missing[0]["severity"], "blocker")

        # Out of bounds timing -> blocker fail
        timings_oob = [
            {"scene_id": 1, "start_s": 0.0, "end_s": 3.0},
            {"scene_id": 2, "start_s": 3.0, "end_s": 6.0},
            {"scene_id": 3, "start_s": 6.0, "end_s": 15.0},  # video is 9.0s
        ]
        checks_oob = check_scene_timings(plan_scenes, timings_oob, video_duration=9.0)
        self.assertFalse(checks_oob[0]["passed"])

        # Overlapping scenes -> warn fail on continuity
        timings_overlap = [
            {"scene_id": 1, "start_s": 0.0, "end_s": 4.0},
            {"scene_id": 2, "start_s": 3.0, "end_s": 6.0},
            {"scene_id": 3, "start_s": 6.0, "end_s": 9.0},
        ]
        checks_overlap = check_scene_timings(plan_scenes, timings_overlap, video_duration=9.0)
        by_name_ol = {c["name"]: c for c in checks_overlap}
        self.assertTrue(by_name_ol["scene_timings_validity"]["passed"])
        self.assertFalse(by_name_ol["scene_timings_continuity"]["passed"])
        self.assertEqual(by_name_ol["scene_timings_continuity"]["severity"], "warn")

        # Gap > 1.0s -> warn fail on continuity
        timings_gap = [
            {"scene_id": 1, "start_s": 0.0, "end_s": 3.0},
            {"scene_id": 2, "start_s": 4.5, "end_s": 7.0},  # 1.5s gap
            {"scene_id": 3, "start_s": 7.0, "end_s": 9.0},
        ]
        checks_gap = check_scene_timings(plan_scenes, timings_gap, video_duration=9.0)
        by_name_gap = {c["name"]: c for c in checks_gap}
        self.assertFalse(by_name_gap["scene_timings_continuity"]["passed"])

        # Existing video mode -> skipped
        checks_ev = check_scene_timings(plan_scenes, [], video_duration=9.0, is_existing_video=True)
        for c in checks_ev:
            self.assertTrue(c["passed"])
            self.assertIn("skipped", c["detail"])

    def test_narration_word_coverage(self):
        plan_scenes = [
            {"narration": "Welcome to today's tech breakdown on artificial intelligence."},
            {"narration": "Here are three tools you must know right now."},
        ]

        # Full match: 100% -> pass (>= 85%)
        srt_full = "Welcome to today's tech breakdown on artificial intelligence. Here are three tools you must know right now."
        res_full = check_narration_coverage(plan_scenes, srt_full)
        self.assertTrue(res_full["passed"])
        self.assertEqual(res_full["severity"], "blocker")

        # 75% match -> warn fail (between 60% and 85%)
        # Missing 4 words out of ~16
        srt_partial = "Welcome to today's breakdown on intelligence. Here are three tools you know right now."
        res_partial = check_narration_coverage(plan_scenes, srt_partial)
        self.assertFalse(res_partial["passed"])
        self.assertEqual(res_partial["severity"], "warn")

        # 30% match -> blocker fail (< 60%)
        srt_poor = "Welcome hello world test other words."
        res_poor = check_narration_coverage(plan_scenes, srt_poor)
        self.assertFalse(res_poor["passed"])
        self.assertEqual(res_poor["severity"], "blocker")

        # Existing video mode -> skipped
        res_ev = check_narration_coverage(plan_scenes, srt_poor, is_existing_video=True)
        self.assertTrue(res_ev["passed"])
        self.assertIn("skipped", res_ev["detail"])


if __name__ == "__main__":
    unittest.main()
