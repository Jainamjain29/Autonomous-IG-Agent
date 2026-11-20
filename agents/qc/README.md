# QC Agent

The QC (Quality Control) Agent inspects the generated output to ensure aspect ratio, duration, audio levels, subtitle readability, and policy compliance meet standards before human review.

## Contract Specification

### Inputs
- Reads `content/plan.json` produced by Content Agent.
- Reads `sync_merge/output.json` produced by Sync Merge Agent (or `task.spec.video_path` if in existing-video mode).
- Reads `spec.yaml` (selected platforms: Instagram, YouTube).

### Primary Output File
`qc/report.json` written inside the run folder (`task.run_dir/qc/report.json`).

### Exact Output JSON Fields
```json
{
  "passed": true,
  "checks": [
    {
      "name": "video_aspect_ratio",
      "passed": true,
      "severity": "blocker",
      "detail": "1080x1920 (ratio 0.5625) within 9:16 ±1% tolerance"
    },
    {
      "name": "video_resolution_standard",
      "passed": true,
      "severity": "warn",
      "detail": "Resolution is standard 1080x1920"
    }
  ],
  "ai_review": {
    "enabled": false,
    "model": "",
    "suggestions": [],
    "error": null
  }
}
```

> **Note on AI Review**: The `ai_review` field is advisory only — it provides improvement suggestions for subsequent runs and never alters `passed` or any check result. It is switched off by default (`settings.ENABLE_AI_CRITIC = False`). When disabled or on call failure, `model` is `""` and `suggestions` is `[]`.

## Severity & Decision Rules

- **`passed = true`** only if NO check with `severity: "blocker"` has failed (`passed: false`).
- Checks with `severity: "warn"` are recorded with details but **warnings never fail the run**.
- When `passed` is `false`, the QC stage returns `status="failed"`, stopping the pipeline before human review.

### Existing-Video Mode Rules
When running in existing-video mode (`spec.mode == "video"`):
- Hard format, resolution, duration, audio, file size, caption, and hashtag limits remain blockers.
- `video_faststart_instagram` check is downgraded to `severity: "warn"` (detail includes fix: `ffmpeg -i in.mp4 -c copy -movflags +faststart out.mp4`).
- Scene timings and narration word coverage checks are skipped and recorded as `passed: true` with detail `"skipped – existing video mode"`.
- Subtitle checks are downgraded to `severity: "warn"` only (burned-in subtitles cannot be reliably detected).
