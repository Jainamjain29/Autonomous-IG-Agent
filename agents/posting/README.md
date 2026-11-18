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
      "status": "published",
      "platform_post_id": "17901234567890123",
      "permalink": "https://www.instagram.com/reel/Cxxxxxx/",
      "error": null
    }
  ]
}
```

#### Allowed Status Values
- `published`: Post was successfully published to the platform (also accepts legacy `ok`).
- `failed`: Publish attempt failed for this platform.
- `skipped`: Platform was skipped (e.g. not configured, such as missing credentials).
- `dry_run`: Dry run validation succeeded; no network call or publishing performed.
