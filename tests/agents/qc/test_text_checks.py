import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
while repo_root in sys.path:
    sys.path.remove(repo_root)
sys.path.insert(0, repo_root)

from agents.qc.checks.text import check_text_compliance, extract_deduped_hashtags


class TestTextChecks(unittest.TestCase):
    def test_hashtag_extraction_and_deduplication(self):
        caption = "Check this out! #AI #Tech #Innovation and #ai again"
        hashtags_list = ["#tech", "Python", "#Startup"]
        deduped = extract_deduped_hashtags(caption, hashtags_list)
        # Expected: #tech, #python, #startup, #ai, #innovation (5 tags)
        self.assertEqual(len(deduped), 5)
        self.assertIn("#tech", [t.lower() for t in deduped])
        self.assertIn("#ai", [t.lower() for t in deduped])
        self.assertIn("#python", [t.lower() for t in deduped])

    def test_required_platform_metadata_presence(self):
        # Empty Instagram caption fails blocker
        plan_no_caption = {"caption": "", "hashtags": ["#a", "#b", "#c"]}
        checks_ig = check_text_compliance(plan_no_caption, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks_ig}
        self.assertFalse(by_name["text_instagram_caption_present"]["passed"])
        self.assertEqual(by_name["text_instagram_caption_present"]["severity"], "blocker")

        # Empty YouTube title fails blocker
        plan_no_title = {"title": "", "caption": "Valid caption"}
        checks_yt = check_text_compliance(plan_no_title, platforms=["youtube"])
        by_name_yt = {c["name"]: c for c in checks_yt}
        self.assertFalse(by_name_yt["text_youtube_title_present"]["passed"])
        self.assertEqual(by_name_yt["text_youtube_title_present"]["severity"], "blocker")

        # Both platforms selected, one missing fails
        checks_both = check_text_compliance(plan_no_title, platforms=["instagram", "youtube"])
        by_name_both = {c["name"]: c for c in checks_both}
        self.assertTrue(by_name_both["text_instagram_caption_present"]["passed"])
        self.assertFalse(by_name_both["text_youtube_title_present"]["passed"])

    def test_instagram_caption_length_and_hook(self):
        # Normal hook (< 125) and normal caption (< 2200)
        valid_caption = "Hook line under 125 chars\n\nBody text describing the reel."
        plan = {"caption": valid_caption, "hashtags": ["#tag1", "#tag2", "#tag3"]}
        checks = check_text_compliance(plan, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks}
        self.assertTrue(by_name["text_instagram_caption_length"]["passed"])
        self.assertTrue(by_name["text_instagram_hook_length"]["passed"])

        # Caption exceeding 2,200 chars fails blocker
        long_caption = "a" * 2201
        plan_long = {"caption": long_caption, "hashtags": ["#tag1", "#tag2", "#tag3"]}
        checks_long = check_text_compliance(plan_long, platforms=["instagram"])
        by_name_long = {c["name"]: c for c in checks_long}
        self.assertFalse(by_name_long["text_instagram_caption_length"]["passed"])
        self.assertEqual(by_name_long["text_instagram_caption_length"]["severity"], "blocker")

        # First line hook exceeding 125 chars warns
        long_hook = ("This is a very long opening hook line that goes on and on and on "
                     "and easily exceeds the one hundred and twenty five character threshold for feed previews.\nSecond line")
        plan_hook = {"caption": long_hook, "hashtags": ["#tag1", "#tag2", "#tag3"]}
        checks_hook = check_text_compliance(plan_hook, platforms=["instagram"])
        by_name_hook = {c["name"]: c for c in checks_hook}
        self.assertFalse(by_name_hook["text_instagram_hook_length"]["passed"])
        self.assertEqual(by_name_hook["text_instagram_hook_length"]["severity"], "warn")

    def test_instagram_hashtag_counts(self):
        # 4 hashtags: passes max (<= 5) and min (>= 3)
        plan_4 = {"caption": "Reel #tag1 #tag2", "hashtags": ["#tag3", "#tag4"]}
        checks_4 = check_text_compliance(plan_4, platforms=["instagram"])
        by_name = {c["name"]: c for c in checks_4}
        self.assertTrue(by_name["text_instagram_hashtags_max"]["passed"])
        self.assertTrue(by_name["text_instagram_hashtags_min"]["passed"])

        # 6 hashtags: fails blocker max (> 5)
        plan_6 = {"caption": "Reel", "hashtags": ["#t1", "#t2", "#t3", "#t4", "#t5", "#t6"]}
        checks_6 = check_text_compliance(plan_6, platforms=["instagram"])
        by_name_6 = {c["name"]: c for c in checks_6}
        self.assertFalse(by_name_6["text_instagram_hashtags_max"]["passed"])
        self.assertEqual(by_name_6["text_instagram_hashtags_max"]["severity"], "blocker")

        # 2 hashtags: fails warn min (< 3)
        plan_2 = {"caption": "Reel", "hashtags": ["#t1", "#t2"]}
        checks_2 = check_text_compliance(plan_2, platforms=["instagram"])
        by_name_2 = {c["name"]: c for c in checks_2}
        self.assertTrue(by_name_2["text_instagram_hashtags_max"]["passed"])
        self.assertFalse(by_name_2["text_instagram_hashtags_min"]["passed"])
        self.assertEqual(by_name_2["text_instagram_hashtags_min"]["severity"], "warn")

    def test_youtube_title_and_description(self):
        # 100 char title passes
        title_100 = "x" * 100
        desc_5000 = "y" * 5000
        plan = {
            "platform_text": {
                "youtube": {
                    "title": title_100,
                    "description": desc_5000,
                }
            }
        }
        checks = check_text_compliance(plan, platforms=["youtube"])
        by_name = {c["name"]: c for c in checks}
        self.assertTrue(by_name["text_youtube_title_length"]["passed"])
        self.assertTrue(by_name["text_youtube_description_length"]["passed"])

        # Title > 100 chars fails blocker
        plan_bad_title = {
            "platform_text": {
                "youtube": {
                    "title": "x" * 101,
                    "description": "Short description",
                }
            }
        }
        checks_bad_t = check_text_compliance(plan_bad_title, platforms=["youtube"])
        by_name_bad_t = {c["name"]: c for c in checks_bad_t}
        self.assertFalse(by_name_bad_t["text_youtube_title_length"]["passed"])
        self.assertEqual(by_name_bad_t["text_youtube_title_length"]["severity"], "blocker")

        # Description > 5000 chars fails blocker
        plan_bad_desc = {
            "platform_text": {
                "youtube": {
                    "title": "Good title",
                    "description": "y" * 5001,
                }
            }
        }
        checks_bad_d = check_text_compliance(plan_bad_desc, platforms=["youtube"])
        by_name_bad_d = {c["name"]: c for c in checks_bad_d}
        self.assertFalse(by_name_bad_d["text_youtube_description_length"]["passed"])
        self.assertEqual(by_name_bad_d["text_youtube_description_length"]["severity"], "blocker")


if __name__ == "__main__":
    unittest.main()
