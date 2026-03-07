---
name: artisan-pi
description: "AI production assistant for animated shows — generates and iterates on images and videos. Runs as a terminal agent using a Pi coding agent. Use when the user asks to create, edit, or iterate on images, videos, character designs, shot frames, or any visual production work."
metadata: '{"nanobot":{"emoji":"🎨","os":["darwin","linux"],"terminal":true}}'
---

# Artisan Pi — AI Production Assistant (Terminal Agent)

Artisan Pi is a fully autonomous AI production assistant that runs as a **nanobot terminal agent**. It uses a Pi coding agent (Node.js) with Python CLI tools for Gemini image generation and Veo 3.1 video generation. All production knowledge lives in markdown files that artisan reads and writes itself.

**You do NOT invoke CLI commands.** Messages sent to the terminal go directly to artisan. Artisan sees the director's text + any attached images, reads project files, generates/reviews images, and responds.

## How It Works

```
Director message + images (via nanobot)
       ↓
  Nanobot terminal protocol (JSON envelope → stdin)
       ↓
  Node.js adapter → Pi coding agent
  - Reads bible.md, character files, shot files
  - Calls python cli/optimize_prompt.py → cli/generate_image.py
  - Reviews generated images (multimodal — it SEES them)
  - Iterates if needed, writes results to project files
       ↓
  Response + generated media (JSONL frames → stdout)
       ↓
  Director sees response + images/videos
```

## CRITICAL: Pass Messages Through Unchanged

**Forward the user's message EXACTLY as they wrote it.** Artisan is an intelligent agent — it reads the project bible, loads references, plans the prompt, and handles all production logic internally.

- User says: "Generate a ref for Luna in her night outfit" → send exactly that
- Do NOT rewrite, elaborate, or add technical details
- The ONLY thing you may add is context about attached images if relevant
- If the user's message is short or vague, that's fine — artisan reads project files for context

## Setting Up a New Project

### 1. Create the project directory

```
<workspace>/projects/<project-slug>/
```

### 2. Create `config.json` with production settings

```json
{
    "image_aspect_ratio": "9:16",
    "image_size": "4K",
    "video_aspect_ratio": "9:16",
    "video_resolution": "1080p"
}
```

**Available settings:**

| Key | Options | Description |
|-----|---------|-------------|
| `image_aspect_ratio` | `1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `9:16`, `16:9` | Default aspect ratio for image generation |
| `image_size` | `1K`, `2K`, `4K` | Image output resolution |
| `video_aspect_ratio` | `9:16`, `16:9` | Default aspect ratio for video generation |
| `video_resolution` | `720p`, `1080p` | Video output resolution |

### 3. Create `bible.md` with the show bible

```markdown
# Show Title

## Style
Art style, color palette, rendering approach, visual rules.

## Characters
- **Character Name**: visual description, personality, distinctive features

## Locations
- **Location Name**: visual description, mood, lighting

## Rules
- Style constraints, things that should never appear
```

### 4. Update the nanobot terminal config

```json
{
    "terminal": {
        "enabled": true,
        "command": "cd ~/artisan-pi && source .venv/bin/activate && node src/main.js <WORKSPACE>/projects/<project-slug>",
        "protocol": "rich",
        "passMedia": true,
        "timeout": 1800,
        "providers": {
            "gemini": { "api_keys": ["..."] }
        }
    }
}
```

**Provider requirements:**
- `gemini`: At least one API key (used for image gen, video gen via Veo 3.1, and prompt optimization via Flash)

### 5. Project is ready

Once the directory exists with `config.json` and `bible.md`, artisan creates all other files as needed:

```
project/
  config.json           ← production settings
  bible.md              ← show bible
  active.md             ← artisan's working memory (auto-created)
  guardrails.md         ← director's production rules (on request)
  characters/           ← one dir per character with .md + media
  locations/            ← one dir per location with .md + media
  props/                ← one dir per prop with .md + media
  arcs/                 ← arc outlines
  chapters/             ← chapter outlines + shot files
  output/               ← generated images and videos
  serve.log             ← debug log
```

## Production Workflow

The director just talks naturally — artisan figures out what to do.

### Image Generation Flow

1. Director describes what they want
2. Artisan reads `bible.md` + relevant character/location files
3. Calls `optimize_prompt` (Gemini Flash translates intent → visual prompt)
4. Calls `generate_image` (Gemini with up to 14 reference images)
5. Reviews the result (multimodal — it SEES the image)
6. Presents the result and waits for director feedback
7. Updates project files on approval

### Shot Workflow

Each shot progresses through stages:

```
Start Frame → End Frame → Video Prompt → Take
```

- Artisan works in order — won't generate end frame before start frame is approved
- Director approves each stage before moving to the next
- Video prompts are generated by artisan, but video is generated via `/generate` command

### Approval Language

Artisan recognizes: "Lock it", "Yes", "Good", "Perfect", "That's it", "Approved", "Next", or moving to a different task.

### Corrections

- Artisan notes the correction in the file
- Regenerates with an updated prompt
- Keeps previous versions (increments version number)
- 2-4 rounds of correction is normal

## The `/generate` Command (Video)

Video generation is **director-controlled**. Artisan prepares the video prompt, but the director triggers generation.

### Syntax

```
/generate interpolate start.png end.png 8s output/prompt.txt
/generate animate start.png 6s output/prompt.txt
/generate generate 8s output/prompt.txt
/generate extend video.mp4 4s
```

| Mode | Description | Requires |
|------|-------------|----------|
| `interpolate` | Transition between two frames | 2 images + prompt file |
| `animate` | Animate a still image | 1 image + prompt file |
| `generate` | Text-to-video with references | Prompt file |
| `extend` | Add footage to existing video | Video path |

Duration: default 6s. Override with `4s`, `6s`, or `8s`.

## What Artisan Can Do

| Capability | How |
|-----------|-----|
| Generate character references | Reads bible + existing refs, optimizes prompt, generates with style consistency |
| Generate location art | Same flow with location references |
| Generate shot frames | Reads shot file, loads character refs, generates start/end frames |
| Prepare video prompts | Structured beat breakdown with character positions, camera, FX |
| Iterate on feedback | Adjusts prompt based on director corrections, regenerates |
| Manage project files | Creates/updates bible.md, character sheets, shot files, active.md |
| Review its own output | Sees generated images, checks against intent and style |

## What Artisan Does NOT Do

- Does not generate video directly (director uses `/generate`)
- Does not search the web
- Does not approve its own work (only the director approves)
- Does not invent characters or plot points unprompted
- Does not delete files (creates new versions instead)

## Debugging

```bash
tail -50 <project-dir>/serve.log
```

| Symptom | Cause | Fix |
|---------|-------|-----|
| No response, timeout | Agent took too long | Increase terminal timeout or simplify request |
| `MISSING_PROVIDER` | No gemini API key in envelope | Check nanobot terminal providers config |
| Images wrong aspect ratio | Missing or wrong config.json | Check `config.json` in project directory |
