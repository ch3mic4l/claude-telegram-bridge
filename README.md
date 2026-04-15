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

agents/              # Per-agent config
    ares/
        config.env
        prompt_builder.py  # Custom (optional)
    athena/
        config.env
    jarvis/
        config.env

run.py              # Entry point: python run.py agents/<name>
ctl.sh              # Controller: ./ctl.sh start|stop|restart|status|logs <name|all>
```

## Usage

```bash
# Start one agent
./ctl.sh start ares

# Start all
./ctl.sh start all

# Check status
./ctl.sh status all

# View logs
./ctl.sh logs athena

# Restart one
./ctl.sh restart jarvis
```

## Adding a new agent

1. Create `agents/<name>/config.env`:
```env
AGENT_NAME=NewAgent
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER=...
WORKSPACE_DIR=/path/to/workspace/
NOTIFY_PORT=9995
MODEL=claude-sonnet-4-6
```

2. Create workspace files (`SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`) in the workspace dir.

3. Optionally add `agents/<name>/prompt_builder.py` for custom system prompts.

4. `./ctl.sh start <name>`

## Features

- Streaming text blocks (responses arrive as they're generated)
- Photo/document/voice/video download and analysis
- Reply context (sees what you're replying to)
- `/stop` — abort current task
- `/model <name>` — switch model (sonnet, opus, haiku)
- `/status` — bridge status
- Group chat filtering (only responds to @mentions and replies)
- Auto-reconnect on Claude process death
- Message interrupt (new message cancels stuck task)
