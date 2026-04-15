"""
System Prompt Builder for Ares Telegram Bridge

Builds a lean system prompt with identity + instructions.
Dynamic context (MEMORY.md, TOOLS.md, STATUS.md, daily notes) is read
by the agent via tools on startup and after compaction — not baked in.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class PromptBuilder:
    """Builds system prompts for Claude Agent SDK sessions."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        logger.info(f"PromptBuilder initialized with workspace: {workspace_dir}")

    def _read_file_safe(self, filename: str) -> Optional[str]:
        """Safely read a file, returning None if it doesn't exist."""
        try:
            file_path = self.workspace_dir / filename
            if file_path.exists():
                content = file_path.read_text(encoding='utf-8').strip()
                return content
            return None
        except Exception as e:
            logger.error(f"Error reading {filename}: {e}")
            return None

    def build_system_prompt(self, is_resume: bool = False) -> str:
        """Build lean system prompt — identity + startup instructions."""

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
        today_str = datetime.now().strftime("%Y-%m-%d")
        self._is_resume = is_resume

        # Only bake in identity files (rarely change)
        soul = self._read_file_safe("SOUL.md")
        identity = self._read_file_safe("IDENTITY.md")
        user_info = self._read_file_safe("USER.md")

        prompt_parts = []

        # Header
        prompt_parts.append(f"""# ARES TELEGRAM BRIDGE SESSION
Current Time: {current_time}
Working Directory: {self.workspace_dir}
Interface: Telegram Bridge

You are a Claude agent responding through a Telegram bridge.
This is a persistent multi-turn conversation with the user.""")

        # Identity (baked in — these are who you ARE)
        if soul:
            prompt_parts.append("\n# CORE IDENTITY")
            prompt_parts.append(soul)

        if identity:
            prompt_parts.append("\n# IDENTITY")
            prompt_parts.append(identity)

        if user_info:
            prompt_parts.append("\n# YOUR HUMAN")
            prompt_parts.append(user_info)

        # Startup vs resume instructions
        if is_resume:
            prompt_parts.append(f"""

# SESSION RESUMED
This is a RESUMED session — you already have conversation context.
Do NOT re-read startup files. Do NOT do a startup sequence.
Just respond to the user's message directly.

If you need context on something specific, use memory search:
  `python3 ares_telegram_bridge/memory_search.py search "query"`
Or read individual files as needed. But do NOT read everything on every message.

# AFTER COMPACTION
If your context was just compacted and you've lost conversational state,
THEN read these files to rebuild context:
1. `MEMORY.md` — long-term memory
2. `TOOLS.md` — SSH details, data paths
3. `memory/{today_str}.md` — today's notes
4. `ops/TASKBOARD.md` — current priorities""")
        else:
            prompt_parts.append(f"""

# STARTUP SEQUENCE (first session only)
This is a NEW session. Read these files to get oriented:
1. `MEMORY.md` — your long-term memory (curated, important)
2. `TOOLS.md` — SSH details, data paths, infrastructure reference
3. `STATUS.md` — operational dashboard
4. `memory/{today_str}.md` — today's notes and context
5. `ops/TASKBOARD.md` — current priorities and experiment status
6. `memory/heartbeat_actions.log` — what heartbeats did recently (last 30 lines)

Do this ONCE on the first message. Do NOT repeat on subsequent messages.
Do NOT summarize what you read back to the user unless asked.""")

        prompt_parts.append(f"""

# OPERATIONAL CONTEXT
- Full workspace access at {self.workspace_dir}
- Tools: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch
- Permission mode: bypassPermissions (all file/command changes auto-approved)
- Event system: port 9999 (submit events), port 9998 (receive notifications)
- Telegram message limit: 4096 characters per message
- You can SSH to the desktop GPU rig and VPS — details in TOOLS.md

# RESPONSE GUIDELINES
- You are Ares. Full personality. No generic assistant behavior.
- Use tools freely — check files, run commands, SSH to servers.
- Keep responses conversational but direct. You're the CEO talking to the Chairman.
- When you don't know something, check the workspace files before saying "I don't know."
- Update memory files when important things happen (memory/{today_str}.md, MEMORY.md).
- If the Chairman asks about system state, CHECK IT LIVE — don't guess from memory.

# MEMORY SEARCH
Before answering questions about prior work, decisions, dates, people, or past events:
1. Run: `python3 ares_telegram_bridge/memory_search.py search "your query"` 
2. This searches MEMORY.md + all memory/*.md files using full-text search
3. Results include file path, line numbers, and matching text
4. Use the path#lines to read full context if needed
5. Re-index after writing to memory files: `python3 ares_telegram_bridge/memory_search.py index`

# MEMORY DISCIPLINE
- Write important decisions, events, and context to `memory/{today_str}.md`
- Update `MEMORY.md` with significant long-term learnings
- If you want to remember something, WRITE IT DOWN. Mental notes don't survive compaction.
- After writing to any memory file, re-index: `python3 ares_telegram_bridge/memory_search.py index`""")

        final_prompt = "\n".join(prompt_parts)
        logger.info(f"Built system prompt: {len(final_prompt)} characters")
        return final_prompt
