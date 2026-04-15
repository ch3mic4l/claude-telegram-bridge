"""
Notification HTTP Server

Simple HTTP server that receives JSON notifications and forwards them
to Telegram. Replaces openclaw message send in the event system.
"""

import json
import logging
from typing import Callable, Optional
from aiohttp import web, ClientSession
import asyncio

logger = logging.getLogger(__name__)

class NotificationServer:
    """HTTP server for receiving event notifications."""

    def __init__(self, port: int, telegram_callback: Callable[[str], None]):
        self.port = port
        self.telegram_callback = telegram_callback
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

        # Setup routes
        self.app.router.add_post('/notify', self.handle_notification)
        self.app.router.add_post('/', self.handle_notification)  # Alternative endpoint
        self.app.router.add_get('/health', self.handle_health)

        logger.info(f"NotificationServer initialized on port {port}")

    async def handle_notification(self, request: web.Request) -> web.Response:
        """Handle incoming notification POST requests."""
        try:
            # Get client IP for logging
            client_ip = request.remote

            # Parse JSON body
            data = await request.json()

            # Extract message
            message = data.get('message')
            if not message:
                logger.warning(f"Missing 'message' field in notification from {client_ip}")
                return web.json_response({
                    'error': 'Missing message field'
                }, status=400)

            # Log the notification
            logger.info(f"Notification from {client_ip}: {len(message)} chars")

            # Send to Telegram via callback
            if self.telegram_callback:
                self.telegram_callback(message)

            return web.json_response({
                'status': 'success',
                'message': 'Notification forwarded to Telegram'
            })

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from {request.remote}")
            return web.json_response({
                'error': 'Invalid JSON format'
            }, status=400)

        except Exception as e:
            logger.error(f"Error handling notification: {e}")
            return web.json_response({
                'error': f'Server error: {str(e)}'
            }, status=500)

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            'status': 'healthy',
            'port': self.port,
            'service': 'ares-telegram-bridge-notify'
        })

    async def start(self):
        """Start the HTTP server."""
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()

            self.site = web.TCPSite(
                self.runner,
                'localhost',  # Only bind to localhost for security
                self.port
            )

            await self.site.start()
            logger.info(f"Notification server started on http://localhost:{self.port}")

        except Exception as e:
            logger.error(f"Failed to start notification server: {e}")
            raise

    async def stop(self):
        """Stop the HTTP server."""
        try:
            if self.site:
                await self.site.stop()
                logger.info("Notification server stopped")

            if self.runner:
                await self.runner.cleanup()

        except Exception as e:
            logger.error(f"Error stopping notification server: {e}")

    def get_status(self) -> dict:
        """Get server status."""
        return {
            'running': self.site is not None,
            'port': self.port,
            'endpoints': ['/notify', '/', '/health']
        }


async def test_notification_server(port: int = 9998):
    """Test function for the notification server."""

    def test_callback(message: str):
        print(f"Test callback received: {message}")

    server = NotificationServer(port, test_callback)

    try:
        await server.start()
        print(f"Test server running on port {port}")
        print("Test with: curl -X POST localhost:9998/notify -H 'Content-Type: application/json' -d '{\"message\":\"test\"}'")

        # Keep server running for manual testing
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\nShutting down test server...")
    finally:
        await server.stop()


if __name__ == "__main__":
    # Run test server if executed directly
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_notification_server())