---
name: nihon-no-sensei
description: "Autonomous Japanese language teacher — JLPT N5→N1 curriculum with per-student progress tracking. Runs as a terminal agent using Claude Agent SDK. Use when the user asks to learn Japanese, practice Japanese, study JLPT, or anything related to Japanese language instruction."
metadata: '{"nanobot":{"emoji":"🇯🇵","os":["darwin","linux"],"terminal":true}}'
---

# Nihon no Sensei — Japanese Language Teacher (Terminal Agent)

Nihon no Sensei is a fully autonomous Japanese teacher that runs as a **nanobot terminal agent**. It uses the Claude Agent SDK to deliver personalized JLPT N5→N1 instruction. The agent reads curriculum from markdown files, maintains per-student progress notes ("bible"), and makes all teaching decisions autonomously.

**You do NOT invoke CLI commands.** Messages sent to the terminal go directly to the teaching agent. The agent reads the student's message, checks their progress, decides what to teach or review, and responds.

## How It Works

```
Student message (via nanobot)
       ↓
  Nanobot terminal protocol (JSON envelope → stdin)
       ↓
  serve.py → Claude Agent SDK
  - Reads bible: mastery.md, handoff.md, scratchpad.md (injected as context)
  - Decides mode: onboarding / teaching / reviewing / checkpoint
  - Reads curriculum files on demand
  - Teaches, quizzes, grades responses
  - Updates bible files (handoff, mastery, chapters)
       ↓
  Response (JSONL frames → stdout)
       ↓
  Student sees Japanese lesson
```

## CRITICAL: Pass Messages Through Unchanged

**Forward the student's message EXACTLY as they wrote it.** The teaching agent is fully autonomous — it reads the student's bible, checks their mastery level, picks the right curriculum section, and handles all teaching logic internally.

- Student says: "I want to learn particles" → send exactly that
- Student says: "こんにちは" → send exactly that
- Do NOT rewrite, elaborate, or add teaching instructions
- If the student's message is short or vague, that's fine — the agent checks handoff.md for context

## Deploying on VPS

### 1. Clone the repo

```bash
cd ~/
git clone https://github.com/anunayarunav/japanese-sensei.git
```

### 2. Create venv and install

```bash
cd /home/deploy/japanese-sensei
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**Dependency**: `claude-agent-sdk` (the only runtime dependency — pulled automatically).

### 3. Update the nanobot terminal config

```json
{
    "terminal": {
        "enabled": true,
        "command": "cd /home/deploy/japanese-sensei && source .venv/bin/activate && python main.py serve",
        "protocol": "rich",
        "timeout": 300,
        "providers": {
            "anthropic": {
                "oauthAccessToken": "..."
            }
        }
    }
}
```

**Provider requirements:**
- `anthropic`: OAuth access token (used by Claude Agent SDK via `CLAUDE_CODE_OAUTH_TOKEN` env var). Alternatively, an API key in `api_keys` works too.

### 4. Updating

To pull the latest version:

```bash
cd /home/deploy/japanese-sensei && git pull && source .venv/bin/activate && pip install -e .
```

Then restart the bot service.

## Per-Student State

Each student gets isolated state automatically. Nanobot passes `{workspace}/users/{chat_id}/` as `user_data_dir`. The agent creates and maintains:

```
{user_data_dir}/bible/
  profile.md         ← name, interests, goals, learning style
  mastery.md         ← top-level progress: active level, % per level
  mastery-n5.md      ← level index: one row per sequence step with scores
  scratchpad.md      ← current teaching state, active exercises
  handoff.md         ← session continuity (what happened, what's next)
  observations.md    ← accumulated teaching observations
  chapters/          ← concept-level detail files (auto-generated)
```

Files are bootstrapped on first encounter (never overwritten). The agent reads and writes these files via Claude Agent SDK tools (Read, Write, Edit, Glob, Grep).

## Teaching Workflow

The agent follows a mandatory 4-step loop every turn:

1. **Read context** — mastery + handoff + scratchpad (auto-injected, no tool calls needed)
2. **Decide mode** — onboarding / teaching / reviewing / checkpoint / conversing
3. **Act** — read curriculum, teach, quiz, or grade
4. **Update state** — always writes handoff.md + scratchpad.md; updates mastery when relevant

### Teaching Rules
- No romaji — hiragana/katakana only
- All kanji with furigana: 食（た）べる
- Max 4 paragraphs per message (conversational, not textbook)
- Follows curriculum sequence as default path
- Score scale: ☆☆☆☆☆ (not started) → ★★★★★ (mastered)

## Curriculum

92 markdown files covering JLPT N5→N1:

| Level | Content | Sequence Steps |
|-------|---------|----------------|
| N5 | ~80 kanji, ~800 vocab, basic grammar | 52 |
| N4 | ~300 kanji, te-form, conditionals | 36 |
| N3 | ~650 kanji, keigo, complex sentences | 11 |
| N2 | ~1000 kanji, business, academic | 10 |
| N1 | ~2000 kanji, literary, classical | 11 |

Curriculum files are bundled in the repo at `curriculum/`. The agent reads them on demand — they are never loaded all at once.

## What the Agent Does

| Capability | How |
|-----------|-----|
| Onboard new students | Asks about goals, interests, prior experience |
| Teach grammar/vocab/kanji | Reads curriculum, explains with examples, gives exercises |
| Grade student responses | Checks answers, gives corrections with explanations |
| Track mastery progress | Updates scores per concept (☆→★ scale) |
| Review previous material | Quizzes on past topics to reinforce learning |
| Level gate assessments | Checkpoint tests before advancing to next JLPT level |
| Session continuity | Writes handoff notes so next session picks up seamlessly |

## What the Agent Does NOT Do

- Does not search the web
- Does not run shell commands
- Does not use romaji (hiragana readings only)
- Does not skip ahead in curriculum without student demonstrating mastery
- Does not delete student state files

## Debugging

| Symptom | Cause | Fix |
|---------|-------|-----|
| No response, timeout | Agent took too many turns | Increase terminal timeout (default 300s) |
| `MISSING_PROVIDER` error | No OAuth token or API key | Check terminal providers config |
| Student state reset | Wrong user_data_dir path | Verify workspace/users/{chat_id} mapping |
| Agent doesn't remember | handoff.md not being written | Check agent logs for tool call errors |
