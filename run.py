#!/usr/bin/env python3
"""
Telegram Bridge Runner

Usage:
    python run.py <agent_config_dir>
    python run.py agents/ares
    python run.py agents/athena

Each agent dir needs:
    config.env  — TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER, WORKSPACE_DIR, NOTIFY_PORT
    
Optional per-agent overrides:
    prompt_builder.py  — custom PromptBuilder class (falls back to generic)
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Add bridge/ to path for imports
sys.path.insert(0, str(Path(__file__).parent / "bridge"))

from session_manager import SessionManager
from telegram_poller import TelegramPoller
from notify import NotificationServer
from prompt_builder import PromptBuilder

# Setup logging — stdout only, nohup captures to log file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


class TelegramBridge:
    """Main daemon that coordinates all bridge components."""

    def __init__(self, agent_dir: str):
        self.agent_dir = Path(agent_dir).resolve()
        
        # Load agent config
        env_file = self.agent_dir / "config.env"
        if not env_file.exists():
            raise ValueError(f"Config not found: {env_file}")
        load_dotenv(env_file, override=True)

        self.agent_name = os.getenv('AGENT_NAME', self.agent_dir.name.capitalize())
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.allowed_user_id = int(os.getenv('TELEGRAM_ALLOWED_USER', '0'))
        allowed_users_env = os.getenv('TELEGRAM_ALLOWED_USERS', '')
        if allowed_users_env:
            self.allowed_user_ids = set(int(uid.strip()) for uid in allowed_users_env.split(',') if uid.strip())
        else:
            self.allowed_user_ids = {self.allowed_user_id} if self.allowed_user_id else set()
        self.workspace_dir = os.getenv('WORKSPACE_DIR', str(self.agent_dir))
        self.notify_port = int(os.getenv('NOTIFY_PORT', '9998'))
        self.model = os.getenv('MODEL', 'claude-sonnet-4-6')

        # Validate
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set in config.env")
        if not self.allowed_user_id:
            raise ValueError("TELEGRAM_ALLOWED_USER not set in config.env")
        if not Path(self.workspace_dir).exists():
            raise ValueError(f"Workspace not found: {self.workspace_dir}")

        # Components
        self.session_manager: Optional[SessionManager] = None
        self.telegram_poller: Optional[TelegramPoller] = None
        self.notification_server: Optional[NotificationServer] = None
        self.shutdown_event = asyncio.Event()

        # Session state lives in agent dir (not workspace)
        self.session_file = self.agent_dir / "session_state.json"

        logger.info(f"Bridge initialized: {self.agent_name} | workspace={self.workspace_dir} | port={self.notify_port}")

    def _setup_signal_handlers(self):
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down...")
            asyncio.create_task(self._shutdown())
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    async def start(self):
        try:
            logger.info(f"Starting {self.agent_name} Telegram Bridge...")
            self._setup_signal_handlers()

            # Try to load custom prompt builder from agent dir, fall back to generic
            prompt_builder = self._load_prompt_builder()

            # Initialize session manager
            self.session_manager = SessionManager(
                workspace_dir=self.workspace_dir,
                prompt_builder=prompt_builder,
                model=self.model,
                session_file=str(self.session_file),
            )

            # Initialize Telegram poller
            self.telegram_poller = TelegramPoller(
                bot_token=self.bot_token,
                allowed_user_id=self.allowed_user_id,
                allowed_user_ids=self.allowed_user_ids,
                claude_callback=self._handle_claude_message,
                command_callback=self._handle_command,
                stop_callback=self._handle_stop,
            )

            # Wire streaming callbacks
            self.session_manager.set_callbacks(
                on_text_block=self.telegram_poller.on_text_block,
                on_turn_start=self.telegram_poller.on_turn_start,
                on_turn_end=self.telegram_poller.on_turn_end,
            )

            # Initialize notification server
            self.notification_server = NotificationServer(
                port=self.notify_port,
                telegram_callback=self._handle_notification,
            )

            # Start all
            logger.info("Starting Claude session...")
            if not await self.session_manager.start():
                raise RuntimeError("Failed to start Claude session")

            logger.info("Starting notification server...")
            await self.notification_server.start()

            logger.info("Starting Telegram poller...")
            if not await self.telegram_poller.start():
                raise RuntimeError("Failed to start Telegram poller")

            logger.info(f"🚀 {self.agent_name} Telegram Bridge started successfully!")
            logger.info(f"Monitoring user {self.allowed_user_id}")
            logger.info(f"Notification endpoint: http://localhost:{self.notify_port}/notify")

            self._handle_notification(f"🚀 {self.agent_name} Telegram Bridge online")

            await self.shutdown_event.wait()

        except Exception as e:
            logger.error(f"Error starting bridge: {e}")
            await self._shutdown()
            raise

    async def _shutdown(self):
        if self.shutdown_event.is_set():
            return
        logger.info(f"Shutting down {self.agent_name} Telegram Bridge...")
        try:
            if self.telegram_poller:
                await self.telegram_poller.stop()
                self.telegram_poller = None
            if self.notification_server:
                await self.notification_server.stop()
                self.notification_server = None
            if self.session_manager:
                await self.session_manager.stop()
                self.session_manager = None
            logger.info("All components stopped")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        finally:
            self.shutdown_event.set()

    def _load_prompt_builder(self) -> PromptBuilder:
        """Load custom prompt_builder.py from agent dir if it exists."""
        custom = self.agent_dir / "prompt_builder.py"
        if custom.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location("custom_prompt_builder", str(custom))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, 'PromptBuilder'):
                logger.info(f"Loaded custom PromptBuilder from {custom}")
                return mod.PromptBuilder(self.workspace_dir)
        
        # Generic
        return PromptBuilder(
            self.workspace_dir,
            agent_name=self.agent_name,
            allowed_user_id=self.allowed_user_id,
        )

    async def _handle_claude_message(self, message: str) -> str:
        if self.session_manager:
            return await self.session_manager.send_message(message)
        return "❌ Claude session not available"

    async def _handle_stop(self) -> str:
        if self.session_manager:
            return await self.session_manager.stop_current_task()
        return "Session manager not available"

    def _handle_command(self, command: str, arg: str = None):
        if command == "set_model" and arg and self.session_manager:
            return self.session_manager.set_model(arg)
        elif command == "interrupt" and self.session_manager:
            self.session_manager.request_interrupt()
            return "interrupted"
        elif command == "get_status":
            if self.session_manager:
                return self.session_manager.get_status()
            return {"error": "session manager not available"}
        return f"Unknown command: {command}"

    def _handle_notification(self, message: str):
        if self.telegram_poller:
            self.telegram_poller.send_notification(message)
            logger.info(f"Forwarded notification: {len(message)} chars")


async def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <agent_config_dir>")
        print(f"Example: {sys.argv[0]} agents/ares")
        sys.exit(1)

    agent_dir = sys.argv[1]
    # Resolve relative to this script's directory
    if not Path(agent_dir).is_absolute():
        agent_dir = str(Path(__file__).parent / agent_dir)

    bridge = None
    try:
        bridge = TelegramBridge(agent_dir)
        await bridge.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
    finally:
        if bridge:
            await bridge._shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
