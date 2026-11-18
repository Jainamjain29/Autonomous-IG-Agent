# Video Gen Agent

The Video Gen Agent generates individual video clips for each planned scene using text-to-video or image-to-video models.

## Contract Specification

### Inputs
- Reads `content/plan.json` produced by Content Agent.
- `task.spec.character_image`: (Optional) Path to reference image for character consistency.

### Primary Output File
`video_gen/clips.json` written inside the run folder (`task.run_dir/video_gen/clips.json`).

### Exact Output JSON Fields
```json
{
  "clips": [
    {
      "scene_id": 1,
      "path": "video_gen/scene_1.mp4",
      "duration_s": 5.0,
      "width": 1080,
      "height": 1920,
      "provider": "luma"
    }
  ]
}
```
