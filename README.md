# Claude Discord Bot

A generic bridge between a Discord channel and a Claude Code agent running in a tmux session. One shared script (`bot.py`) serves multiple projects — each project supplies its own config via a `.env` file and runs the bot as a dedicated systemd service.

---

## Architecture

```
Discord user
    │  (message)
    ▼
discord.py bot  ──────────────────────────────────────────────────────┐
    │                                                                   │
    │  1. Appends reply-file instruction to message                     │
    │  2. tmux send-keys → injects text into Claude session            │
    ▼                                                                   │
tmux session (Claude Code)                                             │
    │                                                                   │
    │  • Does its work (reads files, calls tools, browses web, etc.)   │
    │  • Streams JSONL to ~/.claude/projects/<slug>/*.jsonl             │
    │  • Writes final reply → /tmp/<bot>-reply-<uuid>.txt              │
    │                                                                   │
    ▼                                                                   │
bot _watch_jsonl loop (asyncio, every 0.5 s)                          │
    │                                                                   │
    ├── JSONL events → tool call notifications posted to Discord        │
    ├── Permission prompt detected via tmux pane → relayed to Discord   │
    └── Reply file appears → read, post to Discord, delete file ───────┘
```

---

## Response delivery: the reply-file mechanism

When a Discord message arrives the bot appends a one-line instruction:

```
When you have finished, write your complete reply for the user to this file
using the Write tool: /tmp/<bot>-reply-<uuid>.txt
```

Claude writes its response to that path as its final action. The bot polls for the file's existence; when it appears the contents are read and posted to Discord. This gives a clean, unambiguous signal for when the response is ready and what it contains — avoiding the noise that comes from scraping tmux output or parsing intermediate JSONL text blocks.

Writes to `/tmp/` are globally auto-approved in `~/.claude/settings.json` so Claude is never prompted for permission on this step.

---

## Progress feedback while Claude works

While waiting for the reply file the bot also:

- **Tool calls** — each `tool_use` block from the JSONL stream is posted to Discord as a one-liner (e.g. `🔧 Bash — list files in scripts/`) so the user can follow along. The Write call that delivers the reply file is suppressed.
- **Thinking spinners** — when JSONL goes quiet the bot captures the tmux pane looking for Claude's extended-thinking spinner (e.g. `✻ Pouncing… (43s · thinking)`) and posts it as a `💭` message.
- **Permission prompts** — when Claude Code needs tool-use approval the bot detects the prompt via tmux pane scraping, posts the prompt text to Discord, and lets the user reply with `1` / `2` / `3` to choose an option.

---

## Configuration (per project, in `config/.env`)

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✓ | Bot token from discord.com/developers |
| `DISCORD_CHANNEL_ID` | ✓ | ID of the channel to listen on |
| `TMUX_SESSION` | ✓ | Name of the tmux session running Claude |
| `CLAUDE_PROJECTS_DIR` | ✓ | Path to `~/.claude/projects/<slug>` |
| `BOT_LOG_FILE` | | Log path (default: `/tmp/<BOT_NAME>.log`) |
| `BOT_NAME` | | Display name for logs (default: `TMUX_SESSION`) |
| `BOT_STARTUP_MSG` | | Message posted in Discord on startup |
| `BOT_ENABLE_IMAGES` | | `true` to download image attachments (default: `false`) |

---

## Files

```
claude-discord-bot/
├── bot.py        # The bot — fully configured via env vars
├── install.sh    # Generates and installs a systemd service for a project
├── SKILL.md      # Step-by-step setup guide for Claude agents
└── README.md     # This file
```

---

## Adding a new project

```bash
./install.sh /home/pi/<project> <service-name>
```

Then follow the steps in `SKILL.md`.
