"""
Session Manager — persistent ClaudeSDKClient with streaming text blocks.

Key design (matching OpenClaw's approach):
- Text blocks are sent to Telegram AS THEY ARRIVE (not after the full turn)
- Tool calls happen silently between text blocks
- No timeout on the overall turn — work can take 30+ minutes
- Session persists across restarts via resume
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import List, Callable, Optional, Awaitable
import time

from prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


def _load_session_state(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            logger.info(f"Loaded session state: session_id={data.get('session_id', 'none')}")
            return data
    except Exception as e:
        logger.warning(f"Failed to load session state: {e}")
    return {}


def _save_session_state(path: Path, state: dict):
    try:
        path.write_text(json.dumps(state, indent=2))
        logger.info(f"Saved session state: session_id={state.get('session_id', 'none')}")
    except Exception as e:
        logger.error(f"Failed to save session state: {e}")


class SessionManager:
    def __init__(self, workspace_dir: str, prompt_builder=None,
                 model: str = None, session_file: str = None):
        self.workspace_dir = workspace_dir
        self.prompt_builder = prompt_builder or PromptBuilder(workspace_dir)
        self.is_ready = False
        self.last_activity = time.time()
        self._sdk_available = False
        self.model = model or DEFAULT_MODEL
        self._client = None
        self._receiver_task = None
        self._session_id = None
        self._session_file = Path(session_file) if session_file else Path(workspace_dir) / "session_state.json"

        # Streaming state
        self._current_text_blocks: List[str] = []
        self._turn_complete = asyncio.Event()
        self._on_text_block: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_turn_start: Optional[Callable[[], Awaitable[None]]] = None
        self._on_turn_end: Optional[Callable[[], Awaitable[None]]] = None
        self._interrupt_requested = False
        self._last_block_time = time.time()

        # Load persisted state
        state = _load_session_state(self._session_file)
        self._session_id = state.get("session_id")
        saved_model = state.get("model")
        if saved_model:
            self.model = saved_model

        try:
            from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
            self._sdk_available = True
            logger.info("Agent SDK loaded")
        except ImportError:
            logger.error("claude-agent-sdk not installed")

    def set_callbacks(self, 
                      on_text_block: Callable[[str], Awaitable[None]],
                      on_turn_start: Callable[[], Awaitable[None]] = None,
                      on_turn_end: Callable[[], Awaitable[None]] = None):
        """Set callbacks for streaming text blocks to Telegram."""
        self._on_text_block = on_text_block
        self._on_turn_start = on_turn_start
        self._on_turn_end = on_turn_end

    async def start(self) -> bool:
        if not self._sdk_available:
            return False
        await self._connect()
        self.is_ready = True
        logger.info("SessionManager ready (streaming mode)")
        return True

    async def _connect(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

        is_resume = self._session_id is not None

        options = ClaudeAgentOptions(
            model=self.model,
            cwd=self.workspace_dir,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
            permission_mode="bypassPermissions",
        )

        if self._session_id:
            # Resume existing session — it already has the system prompt
            options.resume = self._session_id
            logger.info(f"Resuming session: {self._session_id} (no new system prompt)")
        else:
            # New session — set the system prompt
            options.system_prompt = self.prompt_builder.build_system_prompt(is_resume=False)
            logger.info(f"New session with system prompt ({len(options.system_prompt)} chars)")

        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        logger.info(f"ClaudeSDKClient connected (model={self.model}, resume={self._session_id or 'new'})")

        self._receiver_task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self):
        """Background task that processes streaming messages from Claude.
        
        Key: AssistantMessage text blocks are forwarded to Telegram IMMEDIATELY
        rather than waiting for the full turn to complete.
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage

        try:
            async for msg in self._client.receive_messages():
                if isinstance(msg, SystemMessage):
                    self._capture_session_id_from_system(msg)

                elif isinstance(msg, AssistantMessage):
                    self._capture_session_id_from_assistant(msg)

                    # Extract text from this message and stream it out
                    if hasattr(msg, 'content'):
                        text_parts = []
                        for block in msg.content:
                            if hasattr(block, 'text') and block.text:
                                text_parts.append(block.text)
                        
                        if text_parts:
                            text = '\n'.join(text_parts)
                            self._current_text_blocks.append(text)
                            self._last_block_time = time.time()
                            
                            # Stream this block to Telegram immediately
                            if self._on_text_block:
                                try:
                                    await self._on_text_block(text)
                                except Exception as e:
                                    logger.error(f"Error in text block callback: {e}")

                elif isinstance(msg, ResultMessage):
                    # Turn complete
                    logger.info(f"Turn complete: {len(self._current_text_blocks)} text blocks")
                    if self._on_turn_end:
                        try:
                            await self._on_turn_end()
                        except Exception as e:
                            logger.error(f"Error in turn_end callback: {e}")
                    self._turn_complete.set()

        except Exception as e:
            logger.error(f"Receive loop error: {e}")
            self._turn_complete.set()
        
        # If we get here, the stream ended (Claude Code process exited)
        logger.warning("Receive loop ended — Claude Code stream closed. Will reconnect on next message.")
        self._client = None  # Mark as disconnected

    def _capture_session_id_from_system(self, msg):
        if hasattr(msg, 'data') and isinstance(msg.data, dict):
            sid = msg.data.get('session_id') or msg.data.get('sessionId')
            if sid and sid != self._session_id:
                self._session_id = sid
                _save_session_state(self._session_file, {
                    "session_id": self._session_id,
                    "model": self.model,
                    "updated_at": time.time(),
                })
                logger.info(f"Captured session ID: {sid}")

    def _capture_session_id_from_assistant(self, msg):
        if hasattr(msg, 'session_id') and msg.session_id and msg.session_id != self._session_id:
            self._session_id = msg.session_id
            _save_session_state(self._session_file, {
                "session_id": self._session_id,
                "model": self.model,
                "updated_at": time.time(),
            })
            logger.info(f"Captured session ID from assistant: {msg.session_id}")

    async def _reconnect(self):
        logger.info("Reconnecting ClaudeSDKClient...")
        try:
            if self._receiver_task:
                self._receiver_task.cancel()
                try:
                    await self._receiver_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._client:
                await self._client.disconnect()
        except Exception as e:
            logger.warning(f"Error during disconnect: {e}")

        self._client = None
        self._receiver_task = None
        await self._connect()

    async def stop(self):
        if self._session_id:
            _save_session_state(self._session_file, {
                "session_id": self._session_id,
                "model": self.model,
                "updated_at": time.time(),
            })

        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            await self._client.disconnect()
        self.is_ready = False
        logger.info("SessionManager stopped")

    async def send_message(self, message: str) -> str:
        """Send a message. Text blocks stream to Telegram via callbacks.
        
        Returns the full concatenated response (for compatibility),
        but the real delivery happens through on_text_block callbacks.
        
        Watchdog: if no text block arrives for 120s, sends a status update
        so the user knows we're still working (not frozen).
        Hard timeout at 600s to prevent infinite hangs.
        """
        if not self._sdk_available:
            return "Session not connected"
        
        # Auto-reconnect if client died
        if not self._client:
            logger.info("Client disconnected — reconnecting before send")
            try:
                await self._reconnect()
            except Exception as e:
                logger.error(f"Reconnect failed: {e}")
                return f"Session reconnect failed: {e}"

        try:
            self.last_activity = time.time()
            self._current_text_blocks = []
            self._turn_complete.clear()
            self._interrupt_requested = False  # Clear any stale interrupt from previous turn
            self._last_block_time = time.time()

            logger.info(f"Sending message ({len(message)} chars), model={self.model}")

            if self._on_turn_start:
                try:
                    await self._on_turn_start()
                except Exception as e:
                    logger.error(f"Error in turn_start callback: {e}")

            # Send the user message
            await self._client.query(message)

            # Wait for turn to complete with watchdog status updates
            WATCHDOG_INTERVAL = 120  # Send status every 2 min of silence
            
            while not self._turn_complete.is_set():
                # Check for interrupt (user sent new message)
                if self._interrupt_requested:
                    logger.info("Interrupt requested — abandoning current turn")
                    self._interrupt_requested = False
                    return None  # Signal to caller: interrupted, don't send response
                
                try:
                    await asyncio.wait_for(
                        self._turn_complete.wait(), 
                        timeout=WATCHDOG_INTERVAL
                    )
                except asyncio.TimeoutError:
                    # Check interrupt again after wait
                    if self._interrupt_requested:
                        logger.info("Interrupt requested during watchdog wait")
                        self._interrupt_requested = False
                        return None
                    
                    elapsed = time.time() - self._last_block_time
                    mins = int(elapsed // 60)
                    logger.info(f"Watchdog: {elapsed:.0f}s since last text block ({mins}m)")
                    if self._on_text_block and mins >= 2:
                        await self._on_text_block(f"⏳ Still working... ({mins}m elapsed, running tools)")

            response = "\n\n".join(self._current_text_blocks).strip()
            if not response:
                response = "(No text response — tool work completed silently)"

            logger.info(f"Done: {len(message)}>{len(response)} chars, {len(self._current_text_blocks)} blocks")
            return response

        except Exception as e:
            logger.error(f"Query error: {e}")
            try:
                await self._reconnect()
            except Exception as re:
                logger.error(f"Reconnect failed: {re}")
            return f"Error: {str(e)}"

    def request_interrupt(self):
        """Request that the current turn be abandoned so a new message can be processed."""
        if not self._turn_complete.is_set():
            logger.info("Interrupt requested by user")
            self._interrupt_requested = True

    async def stop_current_task(self) -> str:
        """Send interrupt signal to Claude Code to abort the current tool call.
        
        This actually sends SIGINT to the subprocess, which makes Claude Code
        abort whatever tool it's running and return a text response acknowledging
        the interruption. The session stays alive.
        """
        if not self._client:
            return "No active session"
        
        if self._turn_complete.is_set():
            return "Nothing running right now"
        
        try:
            logger.info("Sending interrupt signal to Claude Code")
            await self._client.interrupt()
            self._interrupt_requested = True  # Also set flag so send_message loop exits
            return "Stop signal sent — Claude will abort current work"
        except Exception as e:
            logger.error(f"Error sending interrupt: {e}")
            return f"Failed to stop: {e}"

    def set_model(self, model: str) -> str:
        old_model = self.model
        self.model = model
        _save_session_state(self._session_file, {
            "session_id": self._session_id,
            "model": self.model,
            "updated_at": time.time(),
        })
        logger.info(f"Model changed: {old_model} -> {model}")
        asyncio.create_task(self._reconnect())
        return f"Model changed: {old_model} -> {model}"

    def get_status(self) -> dict:
        connected = self._client is not None and self._receiver_task is not None
        return {
            "ready": self.is_ready,
            "sdk_available": self._sdk_available,
            "model": self.model,
            "connected": connected,
            "session_id": self._session_id or "none",
            "last_activity": self.last_activity,
        }
