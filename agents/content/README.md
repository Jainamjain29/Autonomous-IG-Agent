# Content Agent

The Content Agent is responsible for brainstorming topics, writing the narration script, designing visual scene prompts, and formatting metadata.

## Contract Specification

### Inputs
- `task.spec.topic`: (Optional) User-specified topic
- `task.spec.script`: (Optional) User-specified script override
- `task.spec.mode`: Execution mode (`generate` | `script` | `character` | `video`)
- `task.spec.character_text`: (Optional) Character description
- `task.spec.platforms`: Target platforms (default: `["instagram"]`)

### Primary Output File
`content/plan.json` written inside the run folder (`task.run_dir/content/plan.json`).

### Exact Output JSON Fields
```json
{
  "title": "string",
  "topic": "string",
  "hook": "string",
  "script": "string",
  "scenes": [
    {
      "id": 1,
      "narration": "string",
      "visual_prompt": "string",
      "duration_hint_s": 5.0
    }
  ],
  "caption": "string",
  "hashtags": ["#tag1", "#tag2"],
  "platform_text": {
    "instagram": {
      "caption": "string"
    },
    "youtube": {
      "title": "string",
      "description": "string"
    }
  }
}
```
