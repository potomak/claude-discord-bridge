# Skill: Discord Bot Setup

Sets up the Claude Discord bot bridge for a new project. Run this when a project needs a Discord channel wired to its Claude agent tmux session.

---

## Prerequisites

- A Discord bot token and target channel ID (create at discord.com/developers/applications; enable **Message Content Intent**)
- A running tmux session for the project's Claude agent
- `/home/pi/venv` with `discord.py` and `python-dotenv` installed (`pip install discord.py python-dotenv`)
- The project directory must have a `config/.env` file and a `logs/` directory

---

## Step 1 — Add env vars to `config/.env`

Append the following to `<project_dir>/config/.env` (only add what isn't already there):

```
DISCORD_BOT_TOKEN=<token>
DISCORD_CHANNEL_ID=<channel_id>
TMUX_SESSION=<tmux_session_name>
CLAUDE_PROJECTS_DIR=/home/pi/.claude/projects/<project-slug>
BOT_LOG_FILE=<project_dir>/logs/discord-bot.log
BOT_NAME=<project-name>-bot
BOT_STARTUP_MSG=<emoji> <Agent name> online — Claude is listening.
BOT_ENABLE_IMAGES=false
```

Set `BOT_ENABLE_IMAGES=true` if users will send image attachments that Claude should read.

The `CLAUDE_PROJECTS_DIR` slug matches how Claude Code names project directories: replace `/` with `-` in the absolute path (e.g. `/home/pi/my-project` → `-home-pi-my-project`).

---

## Step 2 — Install the systemd service

```bash
cd /home/pi/claude-discord-bot
./install.sh <project_dir> <service_name>
# e.g.: ./install.sh /home/pi/my-project my-project-bot
```

The script generates and installs the service, then enables and starts it. Confirm when prompted.

Verify it's running:
```bash
systemctl status <service_name>
journalctl -u <service_name> -f
```

---

## Step 3 — Update the project's `CLAUDE.md`

Add the following section (adapting bot name and service name):

```markdown
## Discord bridge

This session is bridged to Discord via a bot running as a systemd service (`<service_name>`).
Messages from Discord are injected into this tmux session verbatim.

**Reply file convention:** every incoming message ends with an instruction like:
\```
When you have finished, write your complete reply for the user to this file using the Write tool: /tmp/<bot-name>-reply-<id>.txt
\```
You **must** follow this instruction as your final action — use the Write tool to write exactly
what you want the user to read to that path. The bot detects the file, posts its contents to
Discord, and deletes it. If you skip this step the user receives no response.
```

---

## Managing the bot

```bash
# Restart
sudo systemctl restart <service_name>

# View live logs
journalctl -u <service_name> -f

# Stop / disable
sudo systemctl stop <service_name>
sudo systemctl disable <service_name>
```

---

## How it works (quick reference)

1. Discord message → bot injects it into the tmux session via `tmux send-keys`, appending a reply-file instruction
2. Bot watches `CLAUDE_PROJECTS_DIR/*.jsonl` for tool call activity and posts progress to Discord
3. Claude writes its final reply to the temp file using the Write tool
4. Bot detects the file, reads it, posts to Discord, deletes the file
5. Permission prompts detected via tmux pane scraping → relayed to Discord for user input

See `README.md` for the full architecture.
