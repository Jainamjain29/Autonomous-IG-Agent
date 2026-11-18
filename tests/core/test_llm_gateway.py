"""Tests for core.llm gateway with fake provider."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[2])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from core.llm.gateway import complete, LLMResponse
from core.llm.providers import LLMError
from core.testing import FakeLLM


class TestLLMGateway(unittest.TestCase):
    def setUp(self):
        FakeLLM.reset()
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp_dir.name) / "test_core.db"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_json_validation_and_schema_success(self):
        """Test successful JSON parsing and schema validation."""
        fake = FakeLLM.get_instance()
        fake.set_response("content", {"title": "AI in 2026", "hook": "Listen up!"})

        schema = {
            "type": "object",
            "required": ["title", "hook"],
        }
        resp = complete(
            agent="content",
            messages=[{"role": "user", "content": "Generate a title"}],
            json_schema=schema,
            provider="fake",
            model="fake-model",
            db_path=self.db_path,
        )

        self.assertIsInstance(resp, LLMResponse)
        self.assertEqual(resp.data["title"], "AI in 2026")
        self.assertEqual(resp.data["hook"], "Listen up!")
        self.assertFalse(resp.backup_used)

    def test_repair_retry_on_invalid_json(self):
        """Test that invalid JSON triggers one repair retry and succeeds if repaired."""
        fake = FakeLLM.get_instance()
        # First call returns malformed JSON, second (repair) returns valid schema JSON
        calls = 0

        def custom_call(agent, model, messages, expect_json=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                return "Not valid json at all", 10, 10
            return json.dumps({"title": "Repaired Title", "hook": "Repaired Hook"}), 15, 15

        fake.call = custom_call

        schema = {"type": "object", "required": ["title", "hook"]}
        resp = complete(
            agent="content",
            messages=[{"role": "user", "content": "Make plan"}],
            json_schema=schema,
            provider="fake",
            model="fake-model",
            db_path=self.db_path,
        )

        self.assertEqual(calls, 2)
        self.assertEqual(resp.data["title"], "Repaired Title")

    def test_repair_retry_failure_raises_clear_error(self):
        """Test that if repair also fails, a clear LLMError is raised."""
        fake = FakeLLM.get_instance()
        fake.set_response("content", "Still not json")

        schema = {"type": "object", "required": ["title"]}
        with self.assertRaises(LLMError) as ctx:
            complete(
                agent="content",
                messages=[{"role": "user", "content": "Make plan"}],
                json_schema=schema,
                provider="fake",
                model="fake-model",
                db_path=self.db_path,
            )
        self.assertIn("JSON validation failed after repair retry", str(ctx.exception))

    def test_transient_retry_and_backup_fallback(self):
        """Test retry on transient errors and fallback to backup model when primary fails."""
        fake = FakeLLM.get_instance()
        # Simulate primary failing continuously, backup succeeding
        def fail_on_primary(agent, model, messages, expect_json=False):
            from core.llm.providers import RetryableLLMError
            if model == "primary-model":
                raise RetryableLLMError("Primary server timeout")
            return "Success from backup", 10, 10

        fake.call = fail_on_primary

        resp = complete(
            agent="video_gen",
            messages=[{"role": "user", "content": "generate"}],
            provider="fake",
            model="primary-model",
            backup={"provider": "fake", "model": "backup-model"},
            db_path=self.db_path,
        )

        self.assertTrue(resp.backup_used)
        self.assertEqual(resp.model, "backup-model")
        self.assertEqual(resp.text, "Success from backup")

    def test_usage_row_written_and_no_prompts_stored(self):
        """Verify that usage metadata is logged in SQLite and prompt/response text is NEVER stored."""
        secret_prompt = "TOP_SECRET_PROMPT_KEY_12345"
        secret_reply = "TOP_SECRET_REPLY_DATA_67890"

        fake = FakeLLM.get_instance()
        fake.set_response("qc", secret_reply)

        complete(
            agent="qc",
            messages=[{"role": "user", "content": secret_prompt}],
            provider="fake",
            model="fake-model",
            run_id="run_test_privacy",
            db_path=self.db_path,
        )

        # Inspect database contents
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM llm_calls")
            rows = cursor.fetchall()
            col_names = [desc[0] for desc in cursor.description]

            self.assertEqual(len(rows), 1)
            row_dict = dict(zip(col_names, rows[0]))

            self.assertEqual(row_dict["agent"], "qc")
            self.assertEqual(row_dict["model"], "fake-model")
            self.assertEqual(row_dict["run_id"], "run_test_privacy")
            self.assertEqual(row_dict["success"], 1)

            # Strict Privacy Check: ensure no column contains prompt or response text
            for val in row_dict.values():
                val_str = str(val)
                self.assertNotIn(secret_prompt, val_str)
                self.assertNotIn(secret_reply, val_str)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
