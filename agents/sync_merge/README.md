# Sync Merge Agent

The Sync Merge Agent generates audio from voiceover narration, aligns scene durations with word-level speech timestamps, and merges audio, video, and burnt subtitles.

## Contract Specification

### Inputs
- Reads `content/plan.json` produced by Content Agent.
- Reads `video_gen/clips.json` produced by Video Gen Agent (or `task.spec.video_path` if in video mode).

### Primary Output File
`sync_merge/output.json` written inside the run folder (`task.run_dir/sync_merge/output.json`).

### Exact Output JSON Fields
```json
{
  "video_path": "sync_merge/final_video.mp4",
  "audio_path": "sync_merge/voiceover.mp3",
  "srt_path": "sync_merge/subtitles.srt",
  "duration_s": 28.5,
  "scene_timings": [
    {
      "scene_id": 1,
      "start_s": 0.0,
      "end_s": 5.2
    }
  ]
}
```
