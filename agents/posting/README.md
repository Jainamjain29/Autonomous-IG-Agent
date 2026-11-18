# Posting Agent

The Posting Agent takes an approved video and publishes it across target social media platforms (e.g. Instagram Reels, YouTube Shorts).

## Contract Specification

### Inputs
- Only executed after `approval.json` exists and QC passed.
- Reads `content/plan.json` for captions and hashtags.
- Reads `sync_merge/output.json` for final video path (or `task.spec.video_path`).
- `task.spec.platforms`: List of target platforms (default: `["instagram"]`).

### Primary Output File
`posting/result.json` written inside the run folder (`task.run_dir/posting/result.json`).

### Exact Output JSON Fields
```json
{
  "posts": [
    {
      "platform": "instagram",
      "status": "ok",
      "platform_post_id": "17901234567890123",
      "permalink": "https://www.instagram.com/reel/Cxxxxxx/",
      "error": null
    }
  ]
}
```
