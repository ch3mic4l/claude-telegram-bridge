# Telegram Bridge

Multi-agent Telegram bot framework using Claude Agent SDK.

## Structure

```
bridge/              # Shared code (all agents use this)
    session_manager.py
    telegram_poller.py
    media_handler.py
    notify.py
    prompt_builder.py  # Generic prompt builder

agents/              # Per-agent config and state
    ares/
        config.env
        prompt_builder.py  # Custom (optional)
    athena/
        config.env
    jarvis/
        config.env

run.py              # Entry point: python run.py agents/<name>
ctb                 # Controller: ./ctb <command> <name|all>
```

## Usage

```bash
# First-time setup — creates venv and installs dependencies
./ctb install

# Create a new agent (interactive)
./ctb create myagent

# Start / stop / restart
./ctb start ares
./ctb start all
./ctb stop jarvis
./ctb restart athena

# Check status
./ctb status all

# View logs
./ctb logs ares

# Delete an agent (prompts for confirmation)
./ctb delete myagent
```

## Getting started

```bash
git clone <repo>
cd claude-telegram-bridge
./ctb install        # creates venv, installs requirements.txt
./ctb create myagent # interactive setup
./ctb start myagent
```

## Adding a new agent

The easiest way is `./ctb create <name>`, which prompts for all config values and
creates `config.env`, `SOUL.md`, `USER.md`, and a `MEMORY.md` template in the workspace.

`NOTIFY_PORT` is assigned automatically: `ctb create` scans all existing `config.env`
files, takes the highest port found, and increments until it finds a port that is not
currently in use. You can accept the suggestion or type a different value.

`TELEGRAM_ALLOWED_USER` and `TELEGRAM_ALLOWED_USERS` are pre-filled from the first
existing `config.env` found — just press Enter to reuse the same user IDs.

To set one up manually:

1. Create `agents/<name>/config.env`:
```env
AGENT_NAME=NewAgent
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER=...
TELEGRAM_ALLOWED_USERS=...   # optional, comma-separated
WORKSPACE_DIR=/path/to/workspace/
NOTIFY_PORT=10000            # pick a free port not used by any other agent
MODEL=claude-sonnet-4-6
```

2. Create workspace files in `WORKSPACE_DIR`:
   - `SOUL.md` — agent personality and character
   - `USER.md` — information about the user
   - `MEMORY.md` — long-term memory (start with an empty template)
   - `IDENTITY.md` — optional additional identity context

3. Optionally add `agents/<name>/prompt_builder.py` for a custom system prompt.

4. `./ctb start <name>`

## Models

| Alias | Model ID |
|-------|----------|
| `sonnet` | `claude-sonnet-4-6` |
| `opus` | `claude-opus-4-6` |
| `haiku` | `claude-haiku-4-5` |

Switch models at runtime with `/model <alias>` in Telegram.

## Features

- Streaming text blocks (responses arrive as they're generated)
- Photo/document/voice/video download and analysis
- Reply context (sees what you're replying to)
- `/stop` — abort current task
- `/model <name>` — switch model
- `/status` — bridge status
- Group chat filtering (only responds to @mentions and replies)
- Auto-reconnect on Claude process death
- Message interrupt (new message cancels stuck task)
- Notification endpoint: `POST http://localhost:<NOTIFY_PORT>/notify` with `{"message": "..."}`
