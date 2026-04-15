"""
Media Handler for Telegram Bridge

Downloads photos, documents, and other media from Telegram messages.
Saves them locally so Claude can read/analyze them via tools.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Where to save downloaded media
MEDIA_DIR = Path(__file__).parent / "media"
MEDIA_DIR.mkdir(exist_ok=True)

# Mime type mapping for Telegram photo (always JPEG)
PHOTO_MIME = "image/jpeg"

# Max file size we'll download (20MB — Telegram Bot API limit)
MAX_FILE_SIZE = 20 * 1024 * 1024


def resolve_file_id(message: Dict[str, Any]) -> Optional[str]:
    """Extract the best file_id from a Telegram message.
    
    For photos, uses the largest resolution (last in array).
    Matches OpenClaw's resolveInboundMediaFileId logic.
    """
    # Photo — array of PhotoSize, pick largest
    photos = message.get("photo")
    if photos and isinstance(photos, list) and len(photos) > 0:
        return photos[-1].get("file_id")
    
    # Other media types
    for media_type in ["video", "video_note", "document", "audio", "voice", "animation"]:
        media = message.get(media_type)
        if media and isinstance(media, dict):
            return media.get("file_id")
    
    # Sticker
    sticker = message.get("sticker")
    if sticker and isinstance(sticker, dict):
        # Skip animated/video stickers
        if sticker.get("is_animated") or sticker.get("is_video"):
            return None
        return sticker.get("file_id")
    
    return None


def resolve_mime_type(message: Dict[str, Any]) -> Optional[str]:
    """Get mime type from message media."""
    if message.get("photo"):
        return PHOTO_MIME
    
    for media_type in ["audio", "voice", "video", "document", "animation"]:
        media = message.get(media_type)
        if media and isinstance(media, dict):
            return media.get("mime_type")
    
    if message.get("sticker"):
        return "image/webp"
    
    return None


def resolve_file_name(message: Dict[str, Any]) -> Optional[str]:
    """Get original filename from message media."""
    for media_type in ["document", "audio", "video", "animation"]:
        media = message.get(media_type)
        if media and isinstance(media, dict) and media.get("file_name"):
            return media["file_name"]
    return None


def has_media(message: Dict[str, Any]) -> bool:
    """Check if a message contains downloadable media."""
    return resolve_file_id(message) is not None


def get_media_type_label(message: Dict[str, Any]) -> str:
    """Get a human-readable label for the media type."""
    if message.get("photo"):
        return "photo"
    if message.get("video"):
        return "video"
    if message.get("video_note"):
        return "video_note"
    if message.get("document"):
        return "document"
    if message.get("audio"):
        return "audio"
    if message.get("voice"):
        return "voice"
    if message.get("animation"):
        return "animation"
    if message.get("sticker"):
        return "sticker"
    return "unknown"


async def download_media(client, base_url: str, message: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Download media from a Telegram message.
    
    Returns dict with:
        - path: local file path
        - content_type: mime type
        - label: media type (photo, document, etc.)
        - file_name: original filename if available
    
    Or None if no media or download fails.
    """
    file_id = resolve_file_id(message)
    if not file_id:
        return None
    
    try:
        # Step 1: Get file info from Telegram
        response = await client.get(f"{base_url}/getFile", params={"file_id": file_id})
        if response.status_code != 200:
            logger.error(f"getFile failed: {response.status_code}")
            return None
        
        data = response.json()
        if not data.get("ok"):
            logger.error(f"getFile error: {data}")
            return None
        
        file_info = data["result"]
        file_path = file_info.get("file_path")
        file_size = file_info.get("file_size", 0)
        
        if not file_path:
            logger.error("getFile returned no file_path")
            return None
        
        if file_size > MAX_FILE_SIZE:
            logger.warning(f"File too large: {file_size} bytes (max {MAX_FILE_SIZE})")
            return None
        
        # Step 2: Download the file
        # Telegram Bot API file URL format
        token = base_url.split("/bot")[1]
        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        
        dl_response = await client.get(download_url)
        if dl_response.status_code != 200:
            logger.error(f"File download failed: {dl_response.status_code}")
            return None
        
        # Step 3: Save locally
        mime_type = resolve_mime_type(message) or "application/octet-stream"
        original_name = resolve_file_name(message)
        label = get_media_type_label(message)
        
        # Generate filename
        ext = _ext_from_mime(mime_type) or _ext_from_path(file_path) or ""
        if original_name:
            save_name = original_name
        else:
            msg_id = message.get("message_id", "unknown")
            save_name = f"{label}_{msg_id}{ext}"
        
        save_path = MEDIA_DIR / save_name
        save_path.write_bytes(dl_response.content)
        
        logger.info(f"Downloaded {label}: {save_path} ({len(dl_response.content)} bytes)")
        
        return {
            "path": str(save_path),
            "content_type": mime_type,
            "label": label,
            "file_name": original_name or save_name,
        }
    
    except Exception as e:
        logger.error(f"Error downloading media: {e}")
        return None


def _ext_from_mime(mime_type: str) -> str:
    """Get file extension from mime type."""
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "application/pdf": ".pdf",
    }
    return mapping.get(mime_type, "")


def _ext_from_path(file_path: str) -> str:
    """Get extension from Telegram file_path."""
    if "." in file_path:
        return "." + file_path.rsplit(".", 1)[1]
    return ""


def describe_reply_context(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract reply context from a message, matching OpenClaw's describeReplyTarget.
    
    Returns dict with:
        - id: message_id of replied message
        - sender: sender name/label
        - body: text content of replied message
        - has_media: whether the replied message has media
    
    Or None if not a reply.
    """
    reply = message.get("reply_to_message")
    if not reply:
        return None
    
    # Extract sender info
    reply_from = reply.get("from", {})
    sender_name = reply_from.get("first_name", "")
    if reply_from.get("last_name"):
        sender_name += f" {reply_from['last_name']}"
    if not sender_name:
        sender_name = reply_from.get("username", "unknown")
    
    # Extract body text
    body = reply.get("text") or reply.get("caption") or ""
    
    # Check for media in replied message
    reply_has_media = has_media(reply)
    media_label = get_media_type_label(reply) if reply_has_media else None
    
    if not body and media_label:
        body = f"<{media_label}>"
    
    return {
        "id": str(reply.get("message_id", "")),
        "sender": sender_name,
        "body": body.strip() if body else "",
        "has_media": reply_has_media,
        "media_label": media_label,
    }


def format_message_for_claude(
    text: str,
    media_info: Optional[Dict] = None,
    reply_context: Optional[Dict] = None,
    reply_media_path: Optional[str] = None,
    sender_name: Optional[str] = None,
) -> str:
    """Format a Telegram message with context for Claude.

    Builds a rich message that includes:
    - Sender identification (name/username)
    - Reply context (who they're replying to + content)
    - Media references (file paths Claude can read)
    - The actual message text
    """
    parts = []

    # Sender identification
    if sender_name:
        parts.append(f"[From: {sender_name}]")
    
    # Reply context
    if reply_context:
        reply_lines = []
        reply_lines.append(f"[Replying to {reply_context['sender']}]")
        if reply_context.get("body"):
            # Truncate very long reply bodies
            body = reply_context["body"]
            if len(body) > 500:
                body = body[:500] + "..."
            reply_lines.append(f"> {body}")
        if reply_media_path:
            reply_lines.append(f"[Replied message has media: {reply_media_path}]")
        elif reply_context.get("has_media"):
            reply_lines.append(f"[Replied message has {reply_context.get('media_label', 'media')} — not downloaded]")
        parts.append("\n".join(reply_lines))
    
    # Media attachment
    if media_info:
        label = media_info.get("label", "media")
        path = media_info["path"]
        if label == "photo":
            parts.append(f"[User sent a photo: {path}]")
        elif label == "document":
            fname = media_info.get("file_name", "file")
            parts.append(f"[User sent a document ({fname}): {path}]")
        elif label == "voice":
            parts.append(f"[User sent a voice message: {path}]")
        elif label == "video":
            parts.append(f"[User sent a video: {path}]")
        else:
            parts.append(f"[User sent {label}: {path}]")
    
    # Message text (or caption)
    if text:
        parts.append(text)
    elif media_info and not text:
        # No caption on media — just the media reference
        pass
    
    return "\n\n".join(parts) if parts else "(empty message)"
