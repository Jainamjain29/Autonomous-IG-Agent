"""Live self-test for core.llm gateway via Google Gemini."""
import os
import sys
from dotenv import load_dotenv

from core.llm.gateway import complete


def run_selftest() -> int:
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[selftest] ERROR: GEMINI_API_KEY not found in environment or .env file.")
        return 1

    print("[selftest] Running live test through core.llm gateway...")
    try:
        resp = complete(
            agent="selftest",
            messages=[{"role": "user", "content": "Reply with exactly one word: PONG"}],
            model="gemini-2.5-flash",
            temperature=0.1,
            timeout_s=30,
        )
    except Exception as e:
        print(f"[selftest] FAILED with error: {e}")
        return 1

    print("\n--- LLM Gateway Self-Test Result ---")
    print(f"Provider:    {resp.provider}")
    print(f"Model:       {resp.model}")
    print(f"Tokens In:   {resp.tokens_in}")
    print(f"Tokens Out:  {resp.tokens_out}")
    print(f"Cost USD:    ${resp.cost_usd:.6f}")
    print(f"Latency:     {resp.latency_ms} ms")
    print(f"Backup Used: {resp.backup_used}")
    print(f"Response:    {resp.text.strip()}")
    print("------------------------------------\n")
    print("[selftest] SUCCESS: Core LLM gateway is operational.")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest())
