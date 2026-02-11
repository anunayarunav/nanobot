# Terminal Protocol v1

Specification for building terminal micro-agents that connect to nanobot's gateway and communicate with users on any chat channel (Telegram, Discord, WhatsApp, etc.).

A terminal micro-agent is any CLI program that reads a JSON envelope from **stdin** and writes **JSONL frames** to **stdout**. Nanobot runs the program as a subprocess, streams the output, and routes messages to the user's chat channel.

## Quick Start

### 1. Write a micro-agent

```python
#!/usr/bin/env python3
"""Minimal nanobot terminal micro-agent."""
import json, sys

# Read input envelope from stdin
envelope = json.loads(sys.stdin.readline())
user_text = envelope["text"]
media = envelope.get("media", [])

# Send a progress update
print(json.dumps({"type": "progress", "text": "Thinking..."}), flush=True)

# Send the response
print(json.dumps({"type": "message", "text": f"You said: {user_text}"}), flush=True)
```

### 2. Configure nanobot

```json
{
  "terminal": {
    "enabled": true,
    "command": "python /path/to/agent.py",
    "timeout": 120,
    "protocol": "rich"
  }
}
```

### 3. Chat via Telegram

The user sends a message on Telegram → nanobot runs your agent → your agent's output appears in the chat.

---

## Input: JSON Envelope on stdin

Nanobot writes exactly one JSON object (followed by a newline) to the subprocess's stdin, then closes stdin. The envelope contains the user's message and session context.

### Schema

```json
{
  "version": 1,
  "text": "the user's message text",
  "media": ["/absolute/path/to/image.jpg", "/absolute/path/to/doc.pdf"],
  "channel": "telegram",
  "chat_id": "123456789",
  "session_key": "telegram:123456789",
  "workspace": "/home/deploy/bots/myproject",
  "user_data_dir": "/home/deploy/bots/myproject/users/123456789",
  "providers": {
    "anthropic": {
      "api_keys": ["sk-ant-..."],
      "models": ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"]
    },
    "replicate": {
      "api_keys": ["r8_key1", "r8_key2", "r8_key3"],
      "base_url": "https://api.replicate.com/v1"
    }
  }
}
```

### Fields

| Field | Type | Always present | Description |
|-------|------|----------------|-------------|
| `version` | `int` | yes | Protocol version. Currently `1`. |
| `text` | `string` | yes | The user's message text. May be empty if the user only sent media. |
| `media` | `string[]` | no | Absolute paths to media files the user attached (images, documents, audio, video). Only present when `passMedia: true` in config and the user sent media. Files are already downloaded to disk by the channel. |
| `channel` | `string` | yes | Source channel: `"telegram"`, `"discord"`, `"whatsapp"`, `"cli"`, etc. |
| `chat_id` | `string` | yes | Chat/conversation identifier within the channel. |
| `session_key` | `string` | yes | Unique session key (`channel:chat_id`). |
| `workspace` | `string` | yes | Absolute path to the bot's workspace directory. |
| `user_data_dir` | `string` | yes | Per-user persistent storage directory. Created automatically by nanobot. Store user state (settings, progress, history) here. Layout: `{workspace}/users/{chat_id}/`. |
| `providers` | `object` | no | LLM/API providers configured for this bot. Only present when providers are configured in the terminal config. See [Providers](#providers) below. |

### Reading the envelope

The envelope is a single JSON line on stdin. Read it with:

**Python:**
```python
import json, sys
envelope = json.loads(sys.stdin.readline())
```

**Node.js:**
```javascript
const chunks = [];
process.stdin.on('data', c => chunks.push(c));
process.stdin.on('end', () => {
  const envelope = JSON.parse(Buffer.concat(chunks).toString());
  // ... process envelope
});
```

**Bash:**
```bash
read -r INPUT
TEXT=$(echo "$INPUT" | jq -r '.text')
```

**Go:**
```go
var envelope map[string]interface{}
json.NewDecoder(os.Stdin).Decode(&envelope)
```

If your program does not read stdin, it can ignore it — stdin is closed after writing and will not block the subprocess.

### Providers

When `providers` is configured in the terminal config, the envelope includes a `providers` object. Each key is a provider name, and the value contains:

| Field | Type | Always present | Description |
|-------|------|----------------|-------------|
| `api_keys` | `string[]` | yes | One or more API keys. Use multiple keys for rotation (pick one per request). |
| `models` | `string[]` | no | Available model IDs for this provider. |
| `base_url` | `string` | no | Custom API base URL (for self-hosted or alternative endpoints). |

**Key rotation** is the micro-agent's responsibility. A simple approach:

```python
import random

providers = envelope.get("providers", {})
anthropic = providers.get("anthropic", {})
api_key = random.choice(anthropic["api_keys"])  # rotate per request
```

Providers are configured in the bot's `config.json` under `terminal.providers` (see [Configuration Reference](#configuration-reference)). API keys live in the per-bot config on the server — never in the micro-agent source code.

---

## Output: JSONL Frames on stdout

In **rich** mode, the subprocess writes one JSON object per line to stdout. Each object is a **frame** with a `type` field that determines how nanobot handles it.

### Frame Types

#### `message` — Send a chat message to the user

```json
{"type": "message", "text": "Here's your result"}
{"type": "message", "text": "Generated image:", "media": ["/tmp/output.png"]}
{"type": "message", "text": "", "media": ["/tmp/a.jpg", "/tmp/b.jpg", "/tmp/c.jpg"]}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | `string` | yes | Message text (markdown supported). Can be empty if only sending media. |
| `media` | `string[]` | no | Absolute paths to files to attach. Images and videos are sent as albums on Telegram. Audio and documents are sent individually. |

You can emit **multiple** `message` frames. Each one is sent as a separate chat message. On Telegram, this means the user sees multiple bubbles.

Supported media types by extension:
- **Images**: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`
- **Videos**: `.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`
- **Audio**: `.mp3`, `.ogg`, `.m4a`, `.wav`, `.flac`
- **Documents**: `.pdf` (and any other extension — sent as a file)

#### `progress` — Real-time status update

```json
{"type": "progress", "text": "Downloading model weights..."}
{"type": "progress", "text": "Generating image (step 3/10)..."}
{"type": "progress", "text": "Rendering at 2048x2048..."}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | `string` | yes | Status text shown to the user. |

Progress frames are sent to the user **immediately** as real chat messages (prefixed with ⏳). Use them for long-running operations so the user knows the agent is working. They are not persisted in session history.

#### `error` — Report an error

```json
{"type": "error", "text": "API rate limit exceeded"}
{"type": "error", "text": "Failed to render image", "code": "RENDER_FAIL"}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | `string` | yes | Human-readable error message. |
| `code` | `string` | no | Machine-readable error code for programmatic handling. |

Error frames are sent to the user as the final message. If the subprocess also exits with a non-zero code, the exit code is appended.

#### `log` — Debug output (not shown to user)

```json
{"type": "log", "text": "Calling OpenAI API with model=gpt-4"}
{"type": "log", "text": "Response received in 2.3s", "level": "info"}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | `string` | yes | Log message. |
| `level` | `string` | no | Log level: `"debug"` (default), `"info"`, `"warning"`, `"error"`. |

Log frames are sent to nanobot's logger, not to the user. Useful for debugging without cluttering the chat.

### Non-JSON output

If a line on stdout is not valid JSON (or is JSON without a `type` field), it is treated as plain text and accumulated. When the process exits, accumulated text is sent as a single `message` frame, with media auto-detected by scanning for file paths. This means you can mix structured frames with raw `print()` output and it still works.

### Important: flush stdout

Most languages buffer stdout when it's a pipe. You **must flush after each frame** for real-time streaming to work:

- **Python**: `print(..., flush=True)` or `sys.stdout.flush()`
- **Node.js**: `process.stdout.write(... + '\n')` (unbuffered by default)
- **Go**: `fmt.Println(...)` (unbuffered by default)
- **Bash**: output is unbuffered by default
- **C/C++**: `fflush(stdout)` after each line

---

## Configuration Reference

Config lives in the bot's `config.json` under the `terminal` key. On disk, field names are **camelCase**.

```json
{
  "terminal": {
    "enabled": true,
    "command": "python /opt/my-agent/main.py",
    "timeout": 300,
    "protocol": "rich",
    "passMedia": true,
    "env": {
      "CUSTOM_VAR": "value"
    },
    "providers": {
      "anthropic": {
        "apiKeys": ["sk-ant-key1"],
        "models": ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"]
      },
      "replicate": {
        "apiKeys": ["r8_key1", "r8_key2", "r8_key3"],
        "baseUrl": "https://api.replicate.com/v1"
      }
    }
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `false` | Enable terminal mode (bypasses the LLM pipeline entirely). |
| `command` | `string` | `""` | Shell command to execute. May contain `{message}` placeholder which is replaced with the shell-escaped user text. |
| `timeout` | `int` | `120` | Maximum seconds the subprocess can run before being killed. |
| `protocol` | `string` | `"plain"` | Output protocol: `"plain"` (regex media detection) or `"rich"` (JSONL frames). |
| `passMedia` | `bool` | `true` | Include user's media file paths in the stdin JSON envelope. |
| `env` | `object` | `{}` | Extra environment variables injected into the subprocess. Merged with the parent environment. |
| `providers` | `object` | `{}` | LLM/API providers available to the micro-agent. Passed in the stdin envelope. See below. |

### Provider configuration

Each key under `providers` is a provider name (your choice — `anthropic`, `openai`, `replicate`, `my-service`, etc.). Values:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `apiKeys` | `string[]` | yes | One or more API keys. Multiple keys enable rotation. |
| `models` | `string[]` | no | Model IDs available from this provider. |
| `baseUrl` | `string` | no | Custom API base URL. |

On disk (config.json) these use **camelCase**. In the stdin envelope they arrive as **snake_case** (`api_keys`, `base_url`).

API keys are stored in the per-bot `config.json` on the server, **never** in the micro-agent source code. The master bot manages these through the config.

### The `{message}` placeholder

If your command template contains `{message}`, it is replaced with the shell-escaped user text. This is for backward compatibility with simple CLI tools. For rich micro-agents, you typically omit `{message}` and read the full envelope from stdin instead.

```json
// Simple: pass message as CLI arg (plain mode)
"command": "echo {message}"

// Rich: read from stdin (no placeholder needed)
"command": "python /opt/my-agent/main.py"

// Hybrid: both CLI arg and stdin
"command": "artisan chat -m {message} -v"
```

---

## Protocol Modes

### Plain mode (`protocol: "plain"`)

The original behavior. Nanobot waits for the subprocess to exit, captures all stdout/stderr, scans stdout for absolute file paths with media extensions, and returns a single response.

Use this for simple CLI tools that just print output.

### Rich mode (`protocol: "rich"`)

Nanobot reads stdout **line-by-line in real time**, parsing each line as a JSONL frame. Progress and message frames are forwarded to the user immediately. This enables:

- Real-time progress updates during long operations
- Multiple separate messages in one invocation
- Structured media attachments (no regex guessing)
- Structured error reporting
- Debug logging

Use this for micro-agents that need chat-like interactivity.

---

## Examples

### Stateful agent with per-user data

```python
#!/usr/bin/env python3
"""Agent that remembers user preferences and tracks history."""
import json, sys
from pathlib import Path

envelope = json.loads(sys.stdin.readline())
text = envelope["text"]
user_dir = Path(envelope["user_data_dir"])  # e.g. /workspace/users/123456789/

# Load user state (or create defaults)
state_file = user_dir / "state.json"
if state_file.exists():
    state = json.loads(state_file.read_text())
else:
    state = {"message_count": 0, "preferences": {}}

state["message_count"] += 1

# ... do work with the user's message ...

# Save updated state
state_file.write_text(json.dumps(state, indent=2))

print(json.dumps({
    "type": "message",
    "text": f"Got it! (message #{state['message_count']})"
}), flush=True)
```

### Image generator

```python
#!/usr/bin/env python3
"""Generate images from text descriptions."""
import json, sys
from pathlib import Path

envelope = json.loads(sys.stdin.readline())
prompt = envelope["text"]
user_dir = Path(envelope["user_data_dir"])

print(json.dumps({"type": "progress", "text": "Generating image..."}), flush=True)

# Store output in user's directory
output_path = user_dir / "generated.png"
# ... generation logic ...

print(json.dumps({
    "type": "message",
    "text": f"Generated from: {prompt}",
    "media": [str(output_path)]
}), flush=True)
```

### Code analyzer with multiple messages

```python
#!/usr/bin/env python3
"""Analyze code and return findings."""
import json, sys

envelope = json.loads(sys.stdin.readline())
ref_images = envelope.get("media", [])

print(json.dumps({"type": "progress", "text": "Analyzing..."}), flush=True)

# First message: summary
print(json.dumps({
    "type": "message",
    "text": "## Analysis Results\n\nFound 3 issues in your code."
}), flush=True)

# Second message: details with a file
print(json.dumps({
    "type": "message",
    "text": "Here's the fixed version:",
    "media": ["/tmp/fixed_code.py"]
}), flush=True)
```

### Handling user images

```python
#!/usr/bin/env python3
"""Process user-uploaded images."""
import json, sys
from pathlib import Path

envelope = json.loads(sys.stdin.readline())
user_images = envelope.get("media", [])

if not user_images:
    print(json.dumps({
        "type": "message",
        "text": "Please send an image to process."
    }), flush=True)
    sys.exit(0)

for img_path in user_images:
    p = Path(img_path)
    print(json.dumps({
        "type": "progress",
        "text": f"Processing {p.name}..."
    }), flush=True)

    # Process image...
    output = f"/tmp/processed_{p.name}"
    # ... processing logic ...

    print(json.dumps({
        "type": "message",
        "text": f"Processed {p.name}:",
        "media": [output]
    }), flush=True)
```

### Error handling

```python
#!/usr/bin/env python3
import json, sys

envelope = json.loads(sys.stdin.readline())

try:
    # ... do work ...
    result = do_something(envelope["text"])
    print(json.dumps({"type": "message", "text": result}), flush=True)
except TimeoutError:
    print(json.dumps({
        "type": "error",
        "text": "Operation timed out",
        "code": "TIMEOUT"
    }), flush=True)
    sys.exit(1)
except Exception as e:
    print(json.dumps({
        "type": "error",
        "text": str(e),
        "code": "INTERNAL"
    }), flush=True)
    sys.exit(1)
```

---

## Lifecycle

1. User sends a message on Telegram/Discord/etc.
2. Nanobot builds the command from the `command` template.
3. Subprocess is started with `stdin=PIPE, stdout=PIPE, stderr=PIPE`.
4. JSON input envelope is written to stdin, then stdin is closed.
5. (Rich mode) stdout is read line-by-line. Frames are dispatched in real time.
6. (Plain mode) Process runs to completion. stdout is captured whole.
7. Process exits. stderr and exit code are captured.
8. Final response is sent to the user's chat channel.

### Timeout

If the subprocess exceeds the configured `timeout` (seconds), it is killed (`SIGKILL`) and the user receives a timeout error message. Any messages already sent (from `message` or `progress` frames) remain delivered.

### stderr

stderr is always captured separately. If non-empty, it is appended to the final response text as `STDERR: ...`. Use stderr for diagnostics that should not be parsed as protocol frames.

### Exit codes

A non-zero exit code is appended to the final response. In rich mode, prefer emitting an `error` frame before exiting non-zero so the user gets a clear error message.

---

## Implementation

The protocol is implemented in `nanobot/agent/terminal.py`. The entry point is `run_terminal_command()`, which routes to plain or rich mode based on config. The agent loop calls this from `agent/loop.py:_process_terminal_message()`.

Key functions:
- `_build_input_envelope()` — constructs the stdin JSON
- `_parse_frame()` — parses a single JSONL frame
- `_execute_terminal_rich()` — streaming JSONL reader
- `execute_terminal_command()` — plain mode (original behavior)
- `run_terminal_command()` — unified entry point
