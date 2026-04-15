# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# First-time setup — creates venv from system python3, installs requirements.txt
./ctb install

# Run a single agent (foreground, logs to stdout)
python run.py agents/<name>

# Manage agents via controller
./ctb create <name>         # interactive — prompts for config, creates workspace files
./ctb start <name|all>
./ctb stop <name|all>
./ctb restart <name|all>
./ctb status all
./ctb logs <name>           # tails last 20 lines of bridge.log
./ctb delete <name>         # confirms, stops process, removes agents/<name>/

# Test notification endpoint
curl -X POST http://localhost:9998/notify \
  -H 'Content-Type: application/json' \
  -d '{"message": "test"}'
```

Agents run via `nohup` with stdout captured to `agents/<name>/bridge.log`. PIDs are tracked in `agents/<name>/bridge.pid`.

## Architecture

Three async components wired together in `run.py:TelegramBridge`:

**`SessionManager`** (`bridge/session_manager.py`) — Owns the `ClaudeSDKClient`. Sends messages via `client.query()` and streams responses: `AssistantMessage` text blocks are forwarded to Telegram *immediately* via `on_text_block` callback rather than waiting for the full turn. Session IDs are persisted to `agents/<name>/session_state.json` so conversations survive restarts. Auto-reconnects if the Claude Code subprocess exits. Interrupt/stop works by setting a flag that the `send_message` wait loop checks between `asyncio.wait_for` calls.

**`TelegramPoller`** (`bridge/telegram_poller.py`) — Long-polls Telegram. Commands (`/stop`, `/model`, `/status`) are handled immediately and never blocked by an in-progress Claude turn. Regular messages are dispatched as background `asyncio.Task`s so the poll loop stays responsive. A new message while Claude is processing sends a SIGINT to the Claude subprocess then waits up to 30s for the lock to clear. Group chat support: only responds to @mentions or replies to the bot.

**`NotificationServer`** (`bridge/notify.py`) — `aiohttp` HTTP server. External processes POST `{"message": "..."}` to `/notify`; it calls `TelegramPoller.send_notification()` which queues an async send to the allowed user.

### Adding an agent

The fastest path is `./ctb create <name>` — it prompts for all config values (with an arrow-key model selector), then walks through SOUL.md and USER.md field by field, and writes a MEMORY.md template. All identity fields are optional.

`NOTIFY_PORT` is assigned automatically: `ctb create` scans all existing `config.env` files, takes the highest port found, and keeps incrementing until it finds a port not currently in use (checked via `ss`). The suggested value can be accepted or overridden.

`TELEGRAM_ALLOWED_USER` and `TELEGRAM_ALLOWED_USERS` are pre-filled from the first existing `config.env` found. Press Enter to reuse the same values, or type to override.

To add one manually:
1. Create `agents/<name>/config.env`. Required keys: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER`, `WORKSPACE_DIR`, `NOTIFY_PORT` (pick a free port not used by any other agent).
2. Populate workspace files (`SOUL.md`, `USER.md`, `MEMORY.md`) in `WORKSPACE_DIR`.
3. Optionally add `agents/<name>/prompt_builder.py` with a `PromptBuilder` class — `run.py` dynamically imports it and falls back to `bridge/prompt_builder.py` if absent.
4. `./ctb start <name>`

### Workspace files

| File | Purpose |
|------|---------|
| `SOUL.md` | Agent personality, communication style, focus areas — baked into the system prompt |
| `USER.md` | Information about the human — baked into the system prompt |
| `MEMORY.md` | Long-term memory — read by the agent on startup and after context compaction |
| `IDENTITY.md` | Optional additional identity context |
| `memory/<date>.md` | Daily notes written by the agent during conversations |

### Streaming flow

```
User message → TelegramPoller._handle_claude_message()
  → SessionManager.send_message()
    → ClaudeSDKClient.query()
    → _receive_loop() fires on_text_block() per AssistantMessage
      → TelegramPoller.on_text_block() → Telegram sendMessage (immediate)
    → ResultMessage → _turn_complete.set()
  → returns full concatenated text (used only if no blocks were streamed)
```

### Session persistence

`session_state.json` stores `session_id` + `model`. On restart, `ClaudeSDKClient` is initialized with `options.resume = session_id` instead of a system prompt, continuing the existing conversation. Model changes (`/model`) trigger a reconnect with the new model and update the persisted state.
