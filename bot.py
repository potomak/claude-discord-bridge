#!/usr/bin/env python3
"""
Claude Discord Bot — generic bridge from Discord to a Claude Code tmux session.

Configuration (set in your merchant's config/.env or pass --env-file):

  Required:
    DISCORD_BOT_TOKEN     — Discord bot token
    DISCORD_CHANNEL_ID    — Channel ID to listen on
    TMUX_SESSION          — Name of the tmux session running Claude
    CLAUDE_PROJECTS_DIR   — Path to ~/.claude/projects/<project-slug>

  Optional:
    BOT_LOG_FILE          — Log file path (default: /tmp/<BOT_NAME>.log)
    BOT_NAME              — Display name for logs (default: TMUX_SESSION)
    BOT_STARTUP_MSG       — Message posted in Discord on startup
    BOT_ENABLE_IMAGES     — "true" to download image attachments (default: false)

Audio transcription:
  Audio attachments are transcribed automatically via whisper + ffmpeg when both
  are present on the system. If either is missing and an audio attachment arrives,
  the bot replies with an unsupported notice instead of silently dropping it.

Run standalone:
  python3 bot.py --env-file /path/to/config/.env

Run as a systemd service — see install.sh.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import discord
import whisper as _whisper_mod
from dotenv import load_dotenv

# ── CLI args ───────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="Claude Discord Bot")
_parser.add_argument("--env-file", help="Path to .env file to load")
_args = _parser.parse_args()

if _args.env_file:
    load_dotenv(_args.env_file)
else:
    # Fallback for local dev: look for config/.env relative to cwd
    load_dotenv(Path.cwd() / "config" / ".env")

# ── Configuration ─────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
TMUX_SESSION       = os.getenv("TMUX_SESSION", "")
PROJECTS_DIR       = Path(os.getenv("CLAUDE_PROJECTS_DIR", ""))
BOT_NAME           = os.getenv("BOT_NAME") or TMUX_SESSION or "claude-bot"
BOT_STARTUP_MSG    = os.getenv("BOT_STARTUP_MSG") or f"🤖 {BOT_NAME} online — Claude is listening."
ENABLE_IMAGES      = os.getenv("BOT_ENABLE_IMAGES", "false").lower() == "true"
LOG_FILE           = Path(os.getenv("BOT_LOG_FILE", f"/tmp/{BOT_NAME}.log"))

# ── Audio transcription capability ────────────────────────────────────────────
_FFMPEG_BIN  = shutil.which("ffmpeg")
AUDIO_SUPPORT = bool(_FFMPEG_BIN)
_whisper_model = None  # loaded lazily on first audio message

THINK_TIMEOUT      = 4    # seconds of JSONL quiet before capturing tmux for thinking
STALL_TIMEOUT      = 8    # seconds of JSONL quiet before checking for permission prompt
MAX_TURN_DURATION  = 600  # 10 minutes — abandon request if no resolution by then
POLL_INTERVAL      = 0.5  # seconds between JSONL polls
MSG_LIMIT          = 1900 # Discord message character limit (2000 minus margin)

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger(BOT_NAME)
log.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
fh.setFormatter(fmt)
sh = logging.StreamHandler()
sh.setFormatter(fmt)
log.addHandler(fh)
log.addHandler(sh)

# ── tmux helpers ──────────────────────────────────────────────────────────────
def tmux_send(text: str):
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, text, "Enter"])

def tmux_send_permission(choice: str, num_options: int = 3):
    """Arrow-key navigation for Claude Code permission prompts."""
    downs = int(choice) - 1
    for _ in range(downs):
        subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Down", ""])
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "", "Enter"])

def count_permission_options(pane: str) -> int:
    lines = pane.split("\n")
    count, in_prompt = 0, False
    for line in lines:
        if "Do you want to proceed?" in line:
            in_prompt = True
        if in_prompt and line.strip() and any(f"{n}." in line for n in range(1, 6)):
            count += 1
    return max(count, 1)

def tmux_capture() -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p"],
        capture_output=True, text=True
    )
    return result.stdout

_PERMISSION_TRIGGERS = (
    "Do you want to proceed?",
    "Do you want to create",
    "Do you want to overwrite",
    "Do you want to edit",
    "Do you want to run",
)

def is_permission_prompt(pane: str) -> bool:
    return any(t in pane for t in _PERMISSION_TRIGGERS)

def extract_permission_text(pane: str) -> str:
    lines = pane.split("\n")
    out, capturing = [], False
    for line in lines:
        if any(t in line for t in _PERMISSION_TRIGGERS):
            capturing = True
        if capturing:
            out.append(line)
            if "3. No" in line or "2. No" in line:
                break
    return "\n".join(out).strip()

# Matches Claude Code's thinking spinner lines, e.g.:
#   "· Envisioning…"
#   "✢ Envisioning…"
#   "✶ Envisioning…"
#   "* Envisioning… (1m 4s · ↓ 473 tokens)"
#
# Glyph set from Claude Code source (PQ_ function):
#   default:      · (U+00B7)  ✢ (U+2722)  * (U+002A)  ✶ (U+2736)  ✻ (U+273B)  ✿ (U+273D)
#   ghostty:      · (U+00B7)  ✢ (U+2722)  ✳ (U+2733)  ✶ (U+2736)  ✻ (U+273B)  * (U+002A)
_SPINNER_RE = re.compile(r'^[·✢✳✶✻✿*] [A-Z][a-z]+ing…')

def find_thinking_spinner(pane: str) -> str | None:
    """Return the last thinking spinner line visible in the tmux pane, or None."""
    for line in reversed(pane.split("\n")):
        stripped = line.strip()
        if stripped and _SPINNER_RE.match(stripped):
            return stripped
    return None

# ── JSONL helpers ─────────────────────────────────────────────────────────────
def get_active_jsonl() -> Path | None:
    if not PROJECTS_DIR or not PROJECTS_DIR.exists():
        return None
    files = list(PROJECTS_DIR.glob("*.jsonl"))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None

def read_new_entries(path: Path, from_pos: int) -> tuple[list[dict], int]:
    entries = []
    with open(path) as f:
        f.seek(from_pos)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        new_pos = f.tell()
    return entries, new_pos

def collect_response(entries: list[dict]) -> str:
    texts = []
    for e in entries:
        if e.get("type") != "assistant":
            continue
        if e.get("message", {}).get("stop_reason") != "end_turn":
            continue
        for block in e["message"].get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    texts.append(text)
    return "\n\n".join(texts)

def split_message(text: str) -> list[str]:
    if len(text) <= MSG_LIMIT:
        return [text]
    chunks = []
    while text:
        chunk = text[:MSG_LIMIT]
        split_at = chunk.rfind("\n")
        if split_at > MSG_LIMIT // 2:
            chunk = text[:split_at]
        chunks.append(chunk)
        text = text[len(chunk):].lstrip("\n")
    return chunks

# ── Tool formatting ────────────────────────────────────────────────────────────
_TOOL_EMOJI = {
    "Bash": "🔧",
    "Read": "📄",
    "Write": "✏️",
    "Edit": "✏️",
    "Glob": "🔍",
    "Grep": "🔍",
    "WebFetch": "🌐",
    "WebSearch": "🌐",
    "Agent": "🤖",
    "Task": "📋",
}

def format_tool_call(name: str, inp: dict) -> str:
    emoji = _TOOL_EMOJI.get(name, "🔧")
    label = _format_tool_input(name, inp)
    if label:
        return f"{emoji} **{name}** — `{label}`"
    return f"{emoji} **{name}**"

def _format_tool_input(name: str, inp: dict) -> str:
    if name == "Bash":
        return (inp.get("description") or inp.get("command", ""))[:120]
    if name in ("Read", "Write", "Edit"):
        return inp.get("file_path", "")
    if name == "Glob":
        return inp.get("pattern", "")
    if name == "Grep":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        return f"{pat}" + (f" in {path}" if path else "")
    if name == "WebFetch":
        return inp.get("url", "")[:80]
    if name == "WebSearch":
        return inp.get("query", "")[:80]
    if name == "Agent":
        return inp.get("description", "")[:80]
    for v in inp.values():
        if isinstance(v, str) and len(v) < 100:
            return v
    return ""

# ── Audio transcription ───────────────────────────────────────────────────────
def _load_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        log.info("Loading whisper model (first audio message)...")
        _whisper_model = _whisper_mod.load_model("base")
    return _whisper_model

async def _transcribe_audio(path: Path) -> str:
    """Transcribe an audio file using the whisper Python API. Returns text or ''."""
    def _run():
        model = _load_whisper_model()
        result = model.transcribe(str(path))
        return result["text"].strip()
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        log.warning(f"Whisper transcription failed for {path.name}: {e}")
        return ""

# ── Bot ───────────────────────────────────────────────────────────────────────
class ClaudeDiscordBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self._request_channel: discord.TextChannel | None = None
        self._jsonl_pos: int = 0
        self._accumulated: list[dict] = []
        self._last_activity: float = 0.0
        self._has_end_turn: bool = False
        self._permission_pending: bool = False
        self._permission_num_options: int = 3
        self._permission_queue: asyncio.Queue = asyncio.Queue()
        self._last_tmux_hash: str = ""
        self._last_think_at: float = 0.0
        self._last_progress: float = 0.0
        self._response_file: Path | None = None

    async def on_ready(self):
        log.info(f"Bot ready: {self.user} — watching channel {DISCORD_CHANNEL_ID}")
        jsonl = get_active_jsonl()
        if jsonl:
            with open(jsonl) as f:
                f.seek(0, 2)
                self._jsonl_pos = f.tell()
        asyncio.create_task(self._watch_jsonl())
        try:
            channel = await self.fetch_channel(DISCORD_CHANNEL_ID)
            await channel.send(BOT_STARTUP_MSG)
        except Exception as e:
            log.warning(f"Could not post startup message: {e}")

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return
        if message.channel.id != DISCORD_CHANNEL_ID:
            return

        log.info(f"Discord message from {message.author}: {message.content[:80]!r}")

        valid = [str(i+1) for i in range(self._permission_num_options)]
        if self._permission_pending and message.content.strip() in valid:
            await self._permission_queue.put(message.content.strip())
            return

        if self._request_channel is not None:
            await message.reply("⏳ Still working on the previous request, please wait...")
            return

        jsonl = get_active_jsonl()
        if not jsonl:
            await message.channel.send("❌ No active Claude session found. Is Claude running in tmux?")
            return

        with open(jsonl) as f:
            f.seek(0, 2)
            self._jsonl_pos = f.tell()

        self._request_channel = message.channel
        self._accumulated = []
        self._has_end_turn = False
        self._last_activity = time.time()
        self._last_progress = time.time()
        self._last_tmux_hash = ""
        self._last_think_at = 0.0

        # Generate a unique reply file for this request
        self._response_file = Path(f"/tmp/{BOT_NAME}-reply-{uuid.uuid4().hex[:8]}.txt")
        self._response_file.unlink(missing_ok=True)

        text = message.content.strip()
        if ENABLE_IMAGES:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    dest = Path(f"/tmp/{BOT_NAME}_{int(time.time())}_{attachment.filename}")
                    await attachment.save(dest)
                    text += f"\n[Image attached: {dest} — please read this file to view it]"
                    log.info(f"Image saved: {dest}")

        audio_attachments = [
            a for a in message.attachments
            if a.content_type and a.content_type.startswith("audio/")
        ]
        if audio_attachments:
            if not AUDIO_SUPPORT:
                missing = ["ffmpeg"]
                await message.channel.send(
                    f"⚠️ Audio attachments are not supported — missing: {', '.join(missing)}."
                )
            else:
                await message.channel.send("🎙️ Transcribing audio...")
                for attachment in audio_attachments:
                    dest = Path(f"/tmp/{BOT_NAME}_{int(time.time())}_{attachment.filename}")
                    await attachment.save(dest)
                    log.info(f"Audio saved: {dest}")
                    transcript = await _transcribe_audio(dest)
                    dest.unlink(missing_ok=True)
                    if transcript:
                        text += f"\n[Audio message transcription: {transcript}]"
                        log.info(f"Audio transcribed: {transcript[:80]!r}")
                    else:
                        text += "\n[Audio message: transcription failed]"
                        log.warning(f"Transcription failed for {attachment.filename}")

        # Append instruction so Claude writes its final reply to the temp file
        text += (
            f"\n\n---\nWhen you have finished, write your complete reply for the user "
            f"to this file using the Write tool: {self._response_file}"
        )

        tmux_send(text)
        log.info(f"Sent to tmux: {text[:80]!r}")

    async def _watch_jsonl(self):
        """Continuous background JSONL watcher.
        Only processes entries when a Discord request is pending (_request_channel gate).
        Autonomous Claude activity is silently ignored.
        """
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            if self._request_channel is None:
                continue

            if time.time() - self._last_progress > MAX_TURN_DURATION:
                log.warning("Request timed out after MAX_TURN_DURATION")
                channel = self._request_channel
                self._request_channel = None
                self._accumulated = []
                self._has_end_turn = False
                self._permission_pending = False
                if self._response_file:
                    self._response_file.unlink(missing_ok=True)
                    self._response_file = None
                mins, secs = divmod(MAX_TURN_DURATION, 60)
                duration_str = f"{mins}m" if not secs else f"{mins}m {secs}s"
                await channel.send(f"⚠️ Request timed out — no response from Claude after {duration_str}.")
                continue

            jsonl = get_active_jsonl()
            if not jsonl:
                continue

            new_entries, self._jsonl_pos = read_new_entries(jsonl, self._jsonl_pos)

            if new_entries:
                self._accumulated.extend(new_entries)
                self._last_activity = time.time()

                for e in new_entries:
                    if e.get("type") != "assistant" or e.get("isSidechain"):
                        continue
                    msg = e.get("message", {})

                    if msg.get("stop_reason") == "end_turn":
                        self._has_end_turn = True

                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            # Don't surface the Write call that delivers the reply file
                            if (block.get("name") == "Write"
                                    and self._response_file
                                    and block.get("input", {}).get("file_path") == str(self._response_file)):
                                continue
                            line = format_tool_call(block.get("name", "?"), block.get("input", {}))
                            log.info(f"Tool call: {line}")
                            await self._request_channel.send(line)
                            self._last_progress = time.time()

                meaningful = [e for e in new_entries
                              if e.get("type") in ("user", "assistant")
                              and not e.get("isSidechain")]
                if meaningful and self._permission_pending:
                    log.info("Permission resolved externally")
                    self._permission_pending = False
                    while not self._permission_queue.empty():
                        self._permission_queue.get_nowait()
                    await self._request_channel.send("✅ Permission granted — continuing...")

                for e in new_entries:
                    if e.get("type") == "system" and e.get("subtype") == "turn_duration":
                        log.info("Turn complete via turn_duration")
                        await self._finish_request("turn_duration")
                        break

                if self._request_channel is None:
                    continue

            # Primary completion signal: Claude wrote the reply file
            if self._response_file and self._response_file.exists():
                log.info(f"Response file detected: {self._response_file}")
                await self._finish_request("response file")
                continue

            stalled = time.time() - self._last_activity

            if stalled >= STALL_TIMEOUT and not self._permission_pending:
                pane = tmux_capture()
                if is_permission_prompt(pane):
                    self._permission_pending = True
                    self._permission_num_options = count_permission_options(pane)
                    prompt_text = extract_permission_text(pane)
                    log.info(f"Permission prompt detected ({self._permission_num_options} options)")
                    options_hint = " / ".join(f"`{i+1}`" for i in range(self._permission_num_options))
                    await self._request_channel.send(
                        f"⚠️ **Permission request:**\n```\n{prompt_text}\n```\n"
                        f"Reply with {options_hint} — or approve directly in the terminal."
                    )
                elif self._has_end_turn:
                    log.info("Turn complete via stall+end_turn fallback")
                    await self._finish_request("stall+end_turn fallback")
                    continue

            elif stalled >= THINK_TIMEOUT and not self._permission_pending:
                if time.time() - self._last_think_at >= THINK_TIMEOUT:
                    pane = tmux_capture()
                    if not is_permission_prompt(pane):
                        spinner = find_thinking_spinner(pane)
                        if spinner:
                            msg = f"💭 *{spinner}*"
                            h = hashlib.md5(msg.encode()).hexdigest()
                            if h != self._last_tmux_hash:
                                self._last_tmux_hash = h
                                self._last_think_at = time.time()
                                log.info(f"Thinking spinner: {spinner!r}")
                                await self._request_channel.send(msg)

            if self._permission_pending and not self._permission_queue.empty():
                reply = self._permission_queue.get_nowait()
                log.info(f"Permission reply from Discord: {reply}")
                tmux_send_permission(reply, self._permission_num_options)
                self._permission_pending = False
                self._last_activity = time.time()

    async def _finish_request(self, reason: str):
        channel = self._request_channel
        accumulated = self._accumulated
        response_file = self._response_file

        self._request_channel = None
        self._accumulated = []
        self._has_end_turn = False
        self._permission_pending = False
        self._last_tmux_hash = ""
        self._response_file = None

        # Primary: read response from the file Claude was instructed to write
        response = ""
        if response_file and response_file.exists():
            response = response_file.read_text().strip()
            response_file.unlink(missing_ok=True)
            log.info(f"Response read from file ({len(response)} chars, reason: {reason})")
        else:
            # Fallback: collect text blocks from accumulated JSONL entries
            response = collect_response(accumulated)
            log.info(f"Response from JSONL fallback ({len(response)} chars, reason: {reason})")

        if response:
            for chunk in split_message(response):
                await channel.send(chunk)
        else:
            await channel.send("✅ Done.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN not set")
    if not DISCORD_CHANNEL_ID:
        raise ValueError("DISCORD_CHANNEL_ID not set")
    if not TMUX_SESSION:
        raise ValueError("TMUX_SESSION not set")
    if not str(PROJECTS_DIR):
        raise ValueError("CLAUDE_PROJECTS_DIR not set")
    log.info(f"Starting {BOT_NAME} (tmux: {TMUX_SESSION}, projects: {PROJECTS_DIR})")
    ClaudeDiscordBot().run(DISCORD_TOKEN)
