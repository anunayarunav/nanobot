---
name: artisan
description: "Generate and edit images and videos using AI (Gemini). Use when the user asks to create, edit, or modify images, photos, illustrations, concept art, or videos. Also use when they send a photo and ask for visual changes."
metadata: '{"nanobot":{"emoji":"🎨","os":["darwin","linux"],"requires":{"bins":["artisan"],"env":["GEMINI_API_KEY"]}}}'
---

# Artisan — AI Image & Video Generation

Artisan is a fully autonomous AI image/video editing agent. It plans, executes, and iterates on visual content without human interaction during execution. You invoke it via the `artisan` CLI and send results back to the user with the `message` tool.

## Quick Reference

```bash
# Generate an image from a text prompt
artisan chat --project "default" -m "A serene Japanese garden at sunset" -o /tmp/artisan-out/result.png

# Edit a user's photo
artisan chat --project "default" -m "Make this look like a watercolor painting" -i /path/to/user/photo.jpg -o /tmp/artisan-out/result.png

# Use style reference
artisan chat --project "default" -m "Apply this art style to the photo" -i /path/to/photo.jpg -r /path/to/style.jpg -o /tmp/artisan-out/result.png

# Control output quality
artisan chat --project "default" -m "A portrait" --image-size 4K --aspect-ratio 9:16 -o /tmp/artisan-out/result.png
```

## How to Use Artisan

### Step 1: Prepare the output directory

```bash
mkdir -p /tmp/artisan-out
```

### Step 2: Build the artisan command

Map the user's request to artisan flags:

| User intent | Artisan flags |
|---|---|
| Generate from text | `-m "prompt"` |
| Edit their photo | `-m "prompt" -i /path/to/photo.jpg` |
| Apply a style reference | `-m "prompt" -r /path/to/style.jpg` |
| Edit with style ref | `-m "prompt" -i /path/to/photo.jpg -r /path/to/style.jpg` |
| Specific aspect ratio | `--aspect-ratio 16:9` (options: 1:1, 2:3, 3:2, 3:4, 4:3, 9:16, 16:9) |
| High resolution | `--image-size 4K` (options: 1K, 2K, 4K; default: 2K) |

### Step 3: Run artisan

Always use `--project "default"` and set `-o` to a known output path:

```bash
artisan chat --project "default" -m "USER_PROMPT_HERE" -o /tmp/artisan-out/result.png
```

For user images (when the user sends a photo with their message), pass the file path from the incoming message as `-i`:

```bash
artisan chat --project "default" -m "Make this more vibrant" -i /path/from/message.jpg -o /tmp/artisan-out/result.png
```

### Step 4: Send the result back

After artisan completes, use the `message` tool with the `media` parameter to send the generated image back:

```
message(content="Here's your image!", media=["/tmp/artisan-out/result.png"])
```

If artisan reports the output path differently (check its stdout), use that path instead.

## Handling User Images

When users send photos/images, the channel downloads them to `~/.nanobot/media/` and the message content includes a tag like `[image: /path/to/file.jpg]`. Extract that path and pass it as `-i` to artisan.

Example: User sends a photo with caption "remove the background"
- Incoming content: `[image: /home/deploy/.nanobot/media/AgACAgIAAxkB.jpg] remove the background`
- Command: `artisan chat --project "default" -m "remove the background" -i /home/deploy/.nanobot/media/AgACAgIAAxkB.jpg -o /tmp/artisan-out/result.png`

## Output Location

Always use `/tmp/artisan-out/` as the output directory. Use descriptive filenames when generating multiple images:

```bash
# Single image
-o /tmp/artisan-out/result.png

# Multiple requests in same conversation
-o /tmp/artisan-out/portrait.png
-o /tmp/artisan-out/landscape.png
```

## Error Handling

- If artisan fails, its stderr will contain the error. Common issues:
  - `GEMINI_API_KEY` not set → tell user to contact the admin
  - "did not return an image" → the prompt was refused by the model; suggest rephrasing
  - Timeout → the generation took too long; suggest simpler prompt or retry
- If the output file doesn't exist after artisan exits successfully, check the session directory printed in stdout for intermediate images

## Tips for Better Prompts

When translating user requests into artisan prompts:
- Be descriptive and specific — artisan works best with detailed narrative prompts
- Include style cues: "digital art", "photorealistic", "watercolor", "anime style"
- For edits, describe what to change AND what to keep: "Change the sky to sunset colors, keep the foreground unchanged"
- For character art, include physical details: "tall woman with silver hair, blue eyes, wearing a red coat"

## Advanced: Video Generation

Artisan also supports video generation (Veo 3.1). Video commands take longer (60-300s):

```bash
# Generate a video from text
artisan chat --project "default" -m "A bird flying over a mountain lake, cinematic, slow motion" -o /tmp/artisan-out/result.mp4
```

The agent decides whether to generate an image or video based on the prompt. If the user explicitly asks for a video or animation, mention "video" or "animate" in the prompt to guide the agent.

## Advanced: Continuing a Session

To iterate on a previous result, use `--session` with the session directory from the previous run:

```bash
# First run
artisan chat --project "default" -m "A sunset landscape" -o /tmp/artisan-out/v1.png
# stdout: Session: projects/default/sessions/session_2026-02-07_175105

# Continue with refinement
artisan chat --session projects/default/sessions/session_2026-02-07_175105 -m "Make the colors more vivid" -o /tmp/artisan-out/v2.png
```

This preserves the full conversation context, allowing the agent to refine its previous work.
