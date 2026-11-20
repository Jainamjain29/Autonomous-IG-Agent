"""Sample runner script for Video Generation Agent."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import yaml

from core.contracts import AgentTask, RunSpec
from core.runfolder import create_run, get_run_dir
from agents.video_gen.agent import VideoGenAgent


SAMPLE_TOPIC = "What is an API?"
SAMPLE_CHARACTER_DESC = (
    "Ovi, a small friendly round robot with a glossy white body, "
    "a teal visor for a face with two simple glowing eyes, short stubby arms, "
    "floating slightly above the ground"
)

SAMPLE_SCENES: List[Dict[str, Any]] = [
    {
        "id": "s1",
        "title": "Hook",
        "narration": "Every time an app shows you the weather, it's secretly asking another computer for it.",
        "visual_prompt": "A phone app asking a question, a glowing request flying out",
        "duration_hint_s": 6.0,
    },
    {
        "id": "s2",
        "title": "Explain",
        "narration": "An API is like a waiter: it takes your request to the kitchen and brings back exactly what you asked for.",
        "visual_prompt": "A waiter carrying an order between a customer table and a kitchen",
        "duration_hint_s": 7.0,
    },
    {
        "id": "s3",
        "title": "Payoff",
        "narration": "That's how apps talk to each other — through APIs.",
        "visual_prompt": "Many app icons connecting to many services with light lines",
        "duration_hint_s": 6.0,
    },
]


def run_sample(engine: str = "claude_code", scenes_count: int = 3, run_id: str | None = None) -> None:
    """Create or resume a run folder, write plan, run VideoGenAgent, and display results."""
    print("=" * 60)
    print(f"🎬 VideoGenAgent Sample Run: engine='{engine}', scenes={scenes_count}")
    print("=" * 60)

    if run_id:
        run_dir = get_run_dir(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run directory not found: {run_dir}")
        spec_file = run_dir / "spec.yaml"
        with open(spec_file, "r", encoding="utf-8") as f:
            spec_data = yaml.safe_load(f) or {}
        spec = RunSpec.from_dict(spec_data)
        print(f"Resuming existing run: {run_id}")
    else:
        spec = RunSpec(
            mode="generate",
            topic=SAMPLE_TOPIC,
            character_text=SAMPLE_CHARACTER_DESC,
            extra={"video": {"provider": engine}},
        )
        run_id = create_run(spec)
        run_dir = get_run_dir(run_id)
        print(f"Created fresh run: {run_id} at {run_dir}")

        # Write content/plan.json
        content_dir = run_dir / "content"
        content_dir.mkdir(parents=True, exist_ok=True)
        selected_scenes = SAMPLE_SCENES[:scenes_count]
        plan_data = {
            "title": SAMPLE_TOPIC,
            "scenes": selected_scenes,
        }
        plan_file = content_dir / "plan.json"
        with open(plan_file, "w", encoding="utf-8") as f:
            json.dump(plan_data, f, indent=2)
        print(f"Wrote plan with {len(selected_scenes)} scenes to {plan_file}")

    task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
    agent = VideoGenAgent()

    print("\n⏳ Running VideoGenAgent...")
    t0 = time.monotonic()
    result = agent.run(task)
    elapsed = time.monotonic() - t0

    print("\n" + "=" * 60)
    print(f"📊 Result Status: {result.status} (elapsed: {elapsed:.2f}s)")
    if result.summary:
        print(f"Summary: {result.summary}")
    if result.error:
        print(f"Error: {result.error}")
    if result.outputs:
        print(f"Outputs: {result.outputs}")

    clips_json = run_dir / "video_gen" / "clips.json"
    if clips_json.is_file():
        print(f"\n📄 clips.json content ({clips_json}):")
        with open(clips_json, "r", encoding="utf-8") as f:
            print(f.read())
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run VideoGenAgent sample workflow")
    parser.add_argument("--engine", default="claude_code", choices=["claude_code", "flow", "placeholder"], help="Video provider engine")
    parser.add_argument("--scenes", type=int, default=3, help="Number of scenes to generate (1-3)")
    parser.add_argument("--run-id", type=str, default=None, help="Optional run ID to resume")

    args = parser.parse_args()
    run_sample(engine=args.engine, scenes_count=args.scenes, run_id=args.run_id)
