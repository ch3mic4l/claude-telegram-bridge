"""
Telegram Poller

Handles Telegram bot polling using httpx for API calls. Processes messages
from allowed users and coordinates with Claude Agent SDK session.
"""

import asyncio
import logging
import html
from typing import Optional, Callable, Dict, Any
import httpx
import time

from media_handler import (
    has_media, resolve_file_id, download_media,
    describe_reply_context, format_message_for_claude,
)

logger = logging.getLogger(__name__)

class TelegramPoller:
    """Telegram bot poller with message handling."""

    # Model aliases for easy switching
    # Current 4.6 generation model IDs (April 2026)
    MODEL_ALIASES = {
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-6",
        "haiku": "claude-haiku-4-5",
        "sonnet-4.6": "claude-sonnet-4-6",
        "opus-4.6": "claude-opus-4-6",
        # Legacy
        "sonnet-4": "claude-sonnet-4-20250514",
        "opus-4": "claude-opus-4-20250514",
    }

    def __init__(self, bot_token: str, allowed_user_id: int, claude_callback: Callable[[str], str],
                 command_callback: Callable = None, stop_callback: Callable = None,
                 allowed_user_ids: set = None):
        self.bot_token = bot_token
        self.allowed_user_id = allowed_user_id
        self.allowed_user_ids = allowed_user_ids if allowed_user_ids is not None else {allowed_user_id}
        self.claude_callback = claude_callback
        self.command_callback = command_callback  # For /model etc.
        self.stop_callback = stop_callback  # Async: stops current Claude task
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

        self.last_update_id = 0
        self.is_running = False
        self.client: Optional[httpx.AsyncClient] = None
        self.bot_username: Optional[str] = None  # Set on connect
        self.bot_id: Optional[int] = None  # Set on connect

        # Processing state
        self._processing = False  # True while Claude is thinking
        self._claude_task = None  # Background task for Claude processing
        self.last_message_time = 0
        self.min_message_interval = 1.0

        logger.info(f"TelegramPoller initialized for user {allowed_user_id}")

    async def start(self):
        """Start the Telegram poller."""
        self.client = httpx.AsyncClient(timeout=60.0)
        self.is_running = True

        # Test bot token
        if not await self._test_bot():
            logger.error("Bot token validation failed")
            return False

        # Flush old messages so we don't replay history on restart
        await self._flush_pending_updates()

        logger.info("Telegram poller started")

        # Start polling loop
        asyncio.create_task(self._polling_loop())
        return True

    async def _flush_pending_updates(self):
        """Consume all pending updates so we start fresh."""
        try:
            params = {"offset": -1, "limit": 1, "timeout": 0}
            response = await self.client.get(f"{self.base_url}/getUpdates", params=params)
            if response.status_code == 200:
                data = response.json()
                updates = data.get("result", [])
                if updates:
                    self.last_update_id = updates[-1]["update_id"]
                    logger.info(f"Flushed pending updates, starting from {self.last_update_id + 1}")
                else:
                    logger.info("No pending updates to flush")
        except Exception as e:
            logger.warning(f"Error flushing updates (non-fatal): {e}")

    async def stop(self):
        """Stop the Telegram poller."""
        self.is_running = False

        if self.client:
            await self.client.aclose()

        logger.info("Telegram poller stopped")

    async def _test_bot(self) -> bool:
        """Test bot token and get bot info."""
        try:
            response = await self.client.get(f"{self.base_url}/getMe")
            if response.status_code == 200:
                bot_info = response.json()
                if bot_info.get("ok"):
                    bot_data = bot_info["result"]
                    self.bot_username = bot_data.get('username', '')
                    self.bot_id = bot_data.get('id')
                    logger.info(f"Bot connected: {self.bot_username} ({self.bot_id})")
                    return True

            logger.error(f"Bot test failed: {response.status_code} - {response.text}")
            return False

        except Exception as e:
            logger.error(f"Error testing bot: {e}")
            return False

    async def _polling_loop(self):
        """Main polling loop for Telegram updates."""
        while self.is_running:
            try:
                await self._get_updates()
                await asyncio.sleep(1)  # Poll every second

            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)  # Wait before retry

    async def _get_updates(self):
        """Get updates from Telegram."""
        try:
            params = {
                "offset": self.last_update_id + 1,
                "limit": 100,
                "timeout": 30,
                "allowed_updates": ["message"]
            }

            response = await self.client.get(f"{self.base_url}/getUpdates", params=params)

            if response.status_code != 200:
                logger.error(f"getUpdates failed: {response.status_code}")
                return

            data = response.json()
            if not data.get("ok"):
                logger.error(f"Telegram API error: {data}")
                return

            updates = data.get("result", [])

            for update in updates:
                self.last_update_id = max(self.last_update_id, update["update_id"])
                await self._process_update(update)

        except Exception as e:
            logger.error(f"Error getting updates: {e}")

    async def _process_update(self, update: Dict[str, Any]):
        """Process a single Telegram update.
        
        Key design: commands (/stop, /model, etc.) are handled immediately.
        Regular messages are dispatched to Claude via a background task so
        the polling loop stays responsive and can receive /stop during work.
        
        Supports: text, photos, documents, voice, video, captions, replies.
        """
        try:
            message = update.get("message")
            if not message:
                return

            user_id = message.get("from", {}).get("id")
            chat_id = message.get("chat", {}).get("id")
            chat_type = message.get("chat", {}).get("type", "private")
            is_group = chat_type in ("group", "supergroup")

            # In DMs, only allow configured users. In groups, allow anyone (group filter handles it).
            if not is_group and user_id not in self.allowed_user_ids:
                logger.warning(f"Ignored message from unauthorized user: {user_id}")
                return

            # Extract sender info
            from_user = message.get("from", {})
            first_name = from_user.get("first_name", "")
            last_name = from_user.get("last_name", "")
            username = from_user.get("username", "")
            if username:
                sender_name = f"{first_name} @{username}".strip() if first_name else f"@{username}"
            elif first_name:
                sender_name = f"{first_name} {last_name}".strip()
            else:
                sender_name = str(user_id)

            # Extract text (could be text or caption on media)
            text = message.get("text") or message.get("caption") or ""

            # Handle commands IMMEDIATELY (never blocked by processing)
            if text.startswith("/"):
                await self._handle_command(chat_id, text)
                return

            # --- Group chat filtering ---
            # In groups, only respond to @mentions or direct replies to the bot
            if is_group:
                is_mentioned = self.bot_username and f"@{self.bot_username}" in text
                
                # Check if replying to one of our messages
                reply_to = message.get("reply_to_message", {})
                reply_from_id = reply_to.get("from", {}).get("id") if reply_to else None
                is_reply_to_bot = reply_from_id == self.bot_id
                
                if not is_mentioned and not is_reply_to_bot:
                    # Not addressed to us — ignore silently
                    return
                
                # Strip the @mention from the text so Claude sees clean input
                if is_mentioned and self.bot_username:
                    text = text.replace(f"@{self.bot_username}", "").strip()

            # --- Download media if present ---
            media_info = None
            if has_media(message):
                logger.info(f"Message has media, downloading...")
                media_info = await download_media(self.client, self.base_url, message)
                if media_info:
                    logger.info(f"Downloaded {media_info['label']}: {media_info['path']}")
                else:
                    logger.warning("Failed to download media")

            # --- Extract reply context ---
            reply_context = describe_reply_context(message)
            reply_media_path = None
            if reply_context and reply_context.get("has_media"):
                # Download media from the replied message too
                reply_msg = message.get("reply_to_message", {})
                reply_media = await download_media(self.client, self.base_url, reply_msg)
                if reply_media:
                    reply_media_path = reply_media["path"]
                    logger.info(f"Downloaded reply media: {reply_media_path}")

            # --- Build the formatted message for Claude ---
            formatted = format_message_for_claude(
                text=text,
                media_info=media_info,
                reply_context=reply_context,
                reply_media_path=reply_media_path,
                sender_name=sender_name,
            )

            # Reject truly empty messages
            if formatted == "(empty message)":
                logger.info("Empty message, ignoring")
                return

            # If already processing, interrupt current turn first
            if self._processing:
                logger.info(f"New message while processing — interrupting current turn")
                if self.stop_callback:
                    try:
                        await self.stop_callback()
                    except Exception as e:
                        logger.warning(f"Stop callback error: {e}")
                if self.command_callback:
                    self.command_callback("interrupt")
                for _ in range(30):  # up to 30s
                    await asyncio.sleep(1)
                    if not self._processing:
                        break
                if self._processing:
                    logger.warning("Interrupt didn't free processing lock, forcing")
                    self._processing = False

            self.last_message_time = time.time()

            logger.info(f"Processing message from {user_id}: {len(formatted)} chars")

            # Dispatch to Claude as background task so polling loop stays responsive
            self._claude_task = asyncio.create_task(self._handle_claude_message(chat_id, formatted))

        except Exception as e:
            logger.error(f"Error processing update: {e}")

    async def _handle_command(self, chat_id: int, text: str):
        """Handle bot commands."""
        parts = text.strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/model":
            if not arg:
                # Show current model and available aliases
                status = self.command_callback("get_status") if self.command_callback else {}
                current = status.get("model", "unknown")
                aliases = "\n".join(f"  {k} -> {v}" for k, v in self.MODEL_ALIASES.items())
                await self.send_message(chat_id,
                    f"Current model: {current}\n\nAliases:\n{aliases}\n\nUsage: /model <name or alias>")
            else:
                # Resolve alias
                model_name = self.MODEL_ALIASES.get(arg.lower(), arg)
                if self.command_callback:
                    result = self.command_callback("set_model", model_name)
                    await self.send_message(chat_id, f"✅ {result}")
                else:
                    await self.send_message(chat_id, "❌ Command handler not available")

        elif cmd == "/stop":
            if self._processing and self.stop_callback:
                result = await self.stop_callback()
                await self.send_message(chat_id, f"🛑 {result}")
            elif not self._processing:
                await self.send_message(chat_id, "Nothing running right now.")
            else:
                await self.send_message(chat_id, "❌ Stop not available")

        elif cmd == "/status":
            status = self.command_callback("get_status") if self.command_callback else {}
            lines = ["📊 Bridge Status:"]
            for k, v in status.items():
                lines.append(f"  {k}: {v}")
            await self.send_message(chat_id, "\n".join(lines))

        elif cmd == "/help":
            await self.send_message(chat_id,
                "Commands:\n"
                "/stop — Stop current task (abort tool calls)\n"
                "/model [name] — Show or change the AI model\n"
                "/status — Bridge status\n"
                "/help — This message\n"
                "\nAnything else is sent to Ares.")

        else:
            await self.send_message(chat_id, f"Unknown command: {cmd}\nTry /help")

    async def _handle_claude_message(self, chat_id: int, message: str):
        """Send message to Claude. Text blocks stream to Telegram as they arrive.
        
        Unlike the old approach (wait for full response, send once), this streams
        each text block to Telegram immediately — matching OpenClaw's pattern.
        Tool calls happen silently between blocks. No timeout.
        """
        # Cancel any leaked typing task from a previous message
        self._cancel_typing()
        
        self._processing = True
        self._streaming_chat_id = chat_id
        self._blocks_sent = 0
        try:
            # Send typing indicator
            await self._send_chat_action(chat_id, "typing")

            # Start typing keepalive (runs until cancelled)
            self._typing_task = asyncio.create_task(self._keep_typing(chat_id))

            # Get response from Claude — text blocks delivered via on_text_block callback
            if asyncio.iscoroutinefunction(self.claude_callback):
                full_response = await self.claude_callback(message)
            else:
                full_response = await asyncio.get_event_loop().run_in_executor(
                    None, self.claude_callback, message
                )

            # Stop typing
            self._cancel_typing()

            # If interrupted, full_response is None — skip sending
            if full_response is None:
                logger.info("Turn interrupted, not sending response")
                return

            # If no blocks were streamed (silent tool work), send the full response
            if self._blocks_sent == 0 and full_response:
                await self.send_message(chat_id, full_response)

        except Exception as e:
            logger.error(f"Error handling Claude message: {e}")
            await self.send_message(chat_id, f"❌ Error processing message: {str(e)}")
        finally:
            self._cancel_typing()
            self._processing = False
            self._streaming_chat_id = None

    def _cancel_typing(self):
        """Cancel the typing keepalive task if it exists."""
        task = getattr(self, '_typing_task', None)
        if task and not task.done():
            task.cancel()
        self._typing_task = None

    async def on_text_block(self, text: str):
        """Called by SessionManager when a text block arrives from Claude.
        Sends it to Telegram immediately."""
        if not self._streaming_chat_id or not text.strip():
            return
        
        # Cancel typing while we send real content
        self._cancel_typing()
        
        await self.send_message(self._streaming_chat_id, text)
        self._blocks_sent += 1
        
        # Restart typing for next tool-use gap (only if still processing)
        if self._processing and self._streaming_chat_id:
            self._typing_task = asyncio.create_task(self._keep_typing(self._streaming_chat_id))

    async def on_turn_start(self):
        """Called when a new turn begins."""
        pass  # Typing already started in _handle_claude_message

    async def on_turn_end(self):
        """Called when a turn completes."""
        self._cancel_typing()

    async def _keep_typing(self, chat_id: int):
        """Keep sending typing indicator every 5 seconds while processing."""
        try:
            while True:
                await asyncio.sleep(5)
                await self._send_chat_action(chat_id, "typing")
        except asyncio.CancelledError:
            pass

    async def send_message(self, chat_id: int, text: str):
        """Send message to Telegram chat."""
        try:
            # Split long messages
            max_length = 4096

            if len(text) <= max_length:
                await self._send_single_message(chat_id, text)
            else:
                # Split at reasonable boundaries
                parts = self._split_message(text, max_length)
                for i, part in enumerate(parts):
                    if i > 0:
                        await asyncio.sleep(0.5)  # Brief delay between parts
                    await self._send_single_message(chat_id, part)

        except Exception as e:
            logger.error(f"Error sending message: {e}")

    def _split_message(self, text: str, max_length: int) -> list[str]:
        """Split long message into parts at reasonable boundaries."""
        if len(text) <= max_length:
            return [text]

        parts = []
        current = ""

        # Split by lines first
        lines = text.split('\n')

        for line in lines:
            # If adding this line would exceed limit
            if len(current) + len(line) + 1 > max_length:
                if current:
                    parts.append(current.strip())
                    current = ""

                # If single line is too long, split it
                if len(line) > max_length:
                    words = line.split(' ')
                    for word in words:
                        if len(current) + len(word) + 1 > max_length:
                            if current:
                                parts.append(current.strip())
                                current = word
                            else:
                                # Single word too long, force split
                                parts.append(word[:max_length])
                                current = word[max_length:]
                        else:
                            current += " " + word if current else word
                else:
                    current = line
            else:
                current += "\n" + line if current else line

        if current:
            parts.append(current.strip())

        return parts

    async def _send_single_message(self, chat_id: int, text: str):
        """Send a single message to Telegram. Tries MarkdownV2, falls back to plain."""
        try:
            # Try MarkdownV2 first
            escaped = self._escape_markdownv2(text)
            data = {
                "chat_id": chat_id,
                "text": escaped,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True
            }

            response = await self.client.post(f"{self.base_url}/sendMessage", json=data)

            if response.status_code == 200:
                return

            # MarkdownV2 failed — fall back to plain text
            logger.warning(f"MarkdownV2 send failed ({response.status_code}), falling back to plain text")
            data_plain = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True
            }

            response = await self.client.post(f"{self.base_url}/sendMessage", json=data_plain)

            if response.status_code != 200:
                logger.error(f"Plain text send also failed: {response.status_code} - {response.text}")

        except Exception as e:
            logger.error(f"Error in _send_single_message: {e}")

    @staticmethod
    def _escape_markdownv2(text: str) -> str:
        """Escape text for Telegram MarkdownV2 while preserving intended formatting.

        Preserves: **bold**, _italic_, `code`, ```code blocks```, [links](url)
        Escapes everything else that MarkdownV2 treats as special.
        """
        import re

        # Characters that must be escaped in MarkdownV2
        # (outside of code spans/blocks and other formatting)
        # We take a simple approach: escape all special chars, then
        # un-escape the ones used for formatting patterns.

        # First, protect code blocks and inline code (they need no inner escaping)
        code_blocks = []
        def _save_code_block(m):
            code_blocks.append(m.group(0))
            return f"\x00CODEBLOCK{len(code_blocks)-1}\x00"

        # Save ```...``` blocks
        text = re.sub(r'```[\s\S]*?```', _save_code_block, text)
        # Save `...` inline code
        text = re.sub(r'`[^`]+`', _save_code_block, text)

        # Escape all MarkdownV2 special characters
        special = r'_*[]()~>#+-=|{}.!\\'
        escaped = []
        for ch in text:
            if ch in special:
                escaped.append(f'\\{ch}')
            elif ch == '\x00':
                escaped.append(ch)  # placeholder
            else:
                escaped.append(ch)
        text = ''.join(escaped)

        # Restore bold: \*\*text\*\* → *text*
        text = re.sub(r'\\\*\\\*(.+?)\\\*\\\*', r'*\1*', text)
        # Restore italic: \_text\_ → _text_
        text = re.sub(r'\\_(.+?)\\_', r'_\1_', text)

        # Restore code blocks/inline code
        for i, block in enumerate(code_blocks):
            text = text.replace(f'\x00CODEBLOCK{i}\x00', block)

        return text

    async def _send_chat_action(self, chat_id: int, action: str):
        """Send chat action (typing indicator)."""
        try:
            data = {
                "chat_id": chat_id,
                "action": action
            }

            await self.client.post(f"{self.base_url}/sendChatAction", json=data)

        except Exception as e:
            logger.debug(f"Error sending chat action: {e}")  # Non-critical error

    def send_notification(self, message: str):
        """Send notification message to the allowed user. Called by notification server."""
        try:
            if not self.client or not self.is_running:
                logger.warning("Cannot send notification — poller not ready")
                return
            asyncio.create_task(self.send_message(self.allowed_user_id, f"🔔 {message}"))
            logger.info(f"Queued notification: {len(message)} chars")

        except Exception as e:
            logger.error(f"Error queuing notification: {e}")

    def get_status(self) -> dict:
        """Get poller status."""
        return {
            "running": self.is_running,
            "allowed_user": self.allowed_user_id,
            "last_update_id": self.last_update_id,
            "last_message_time": self.last_message_time
        }