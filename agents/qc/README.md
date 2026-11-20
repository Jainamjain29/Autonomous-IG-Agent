# QC Agent

The QC (Quality Control) Agent inspects the generated output to ensure aspect ratio, duration, audio levels, subtitle readability, and policy compliance meet standards before human review.

## Contract Specification

### Inputs
- Reads `content/plan.json` produced by Content Agent.
- Reads `sync_merge/output.json` produced by Sync Merge Agent (or `task.spec.video_path` if in video mode).

### Primary Output File
`qc/report.json` written inside the run folder (`task.run_dir/qc/report.json`).

### Exact Output JSON Fields
```json
{
  "passed": true,
  "checks": [
    {
      "name": "aspect_ratio",
      "passed": true,
      "severity": "blocker",
      "detail": "1080x1920 (9:16) verified"
    },
    {
      "name": "audio_levels",
      "passed": true,
      "severity": "warn",
      "detail": "Integrated LUFS -14.2"
    }
  ],
  "ai_review": {
    "enabled": false,
    "model": "gemini-2.5-flash",
    "suggestions": [
      {
        "area": "pacing",
        "suggestion": "Trim opening hook by 0.5s to increase retention."
      }
    ],
    "error": null
  }
}
```
