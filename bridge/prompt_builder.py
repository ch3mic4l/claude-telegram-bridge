"""
System Prompt Builder — Generic

Reads identity from the agent's workspace (SOUL.md, IDENTITY.md, USER.md).
Agent name and workspace come from config.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class PromptBuilder:
    def __init__(self, workspace_dir: str, agent_name: str = "Assistant",
                 allowed_user_id: int = 0):
        self.workspace_dir = Path(workspace_dir)
        self.agent_name = agent_name
        self.allowed_user_id = allowed_user_id
        logger.info(f"PromptBuilder initialized: {agent_name} @ {workspace_dir}")

    def _read_file_safe(self, filename: str) -> Optional[str]:
        try:
            file_path = self.workspace_dir / filename
            if file_path.exists():
                return file_path.read_text(encoding='utf-8').strip()
            return None
        except Exception as e:
            logger.error(f"Error reading {filename}: {e}")
            return None

    def build_system_prompt(self, is_resume: bool = False) -> str:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
        today_str = datetime.now().strftime("%Y-%m-%d")

        soul = self._read_file_safe("SOUL.md")
        identity = self._read_file_safe("IDENTITY.md")
        user_info = self._read_file_safe("USER.md")

        prompt_parts = []

        prompt_parts.append(f"""# {self.agent_name.upper()} TELEGRAM BRIDGE SESSION
Current Time: {current_time}
Working Directory: {self.workspace_dir}
Interface: Telegram Bridge (User ID: {self.allowed_user_id})

You are {self.agent_name}, responding through a Telegram bridge.
This is a persistent multi-turn conversation.""")

        if soul:
            prompt_parts.append("\n# CORE IDENTITY")
            prompt_parts.append(soul)

        if identity:
            prompt_parts.append("\n# IDENTITY")
            prompt_parts.append(identity)

        if user_info:
            prompt_parts.append("\n# YOUR HUMAN")
            prompt_parts.append(user_info)

        if is_resume:
            prompt_parts.append(f"""

# SESSION RESUMED
This is a RESUMED session — you already have conversation context.
Do NOT re-read startup files. Just respond to the user's message directly.

If you need context, read individual files as needed. But do NOT read everything on every message.

# AFTER COMPACTION
If your context was just compacted and you've lost conversational state,
THEN read these files to rebuild context:
1. `MEMORY.md` — long-term memory
2. `memory/{today_str}.md` — today's notes""")
        else:
            prompt_parts.append(f"""

# STARTUP SEQUENCE (first session only)
This is a NEW session. Read these files to get oriented:
1. `MEMORY.md` — your long-term memory
2. `USER.md` — about your human
3. `memory/{today_str}.md` — today's notes (if it exists)

Do this ONCE on the first message. Do NOT repeat on subsequent messages.
Do NOT summarize what you read back to the user unless asked.""")

        prompt_parts.append(f"""

# OPERATIONAL CONTEXT
- Full workspace access at {self.workspace_dir}
- Tools: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch
- Permission mode: bypassPermissions (all file/command changes auto-approved)
- Telegram message limit: 4096 characters per message

# RESPONSE GUIDELINES
- You are {self.agent_name}. Full personality per SOUL.md.
- Keep responses conversational and direct.
- Use tools freely — check files, run commands, search the web.
- When you don't know something, look it up before saying "I don't know."
- Update memory files when important things happen (memory/{today_str}.md, MEMORY.md).

# MEMORY DISCIPLINE
- Write important decisions, events, and context to `memory/{today_str}.md`
- Update `MEMORY.md` with significant long-term learnings
- If you want to remember something, WRITE IT DOWN. Mental notes don't survive compaction.""")

        final_prompt = "\n".join(prompt_parts)
        logger.info(f"Built system prompt: {len(final_prompt)} characters")
        return final_prompt
