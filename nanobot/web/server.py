"""Minimal HTTP server for Stripe webhooks."""

from collections.abc import Awaitable, Callable
from typing import Any

import stripe
from aiohttp import web
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.config.schema import PaymentsConfig
from nanobot.store.credits import CreditStore


class WebhookServer:
    """aiohttp server hosting Stripe webhook endpoint."""

    def __init__(
        self,
        config: PaymentsConfig,
        credit_store: CreditStore,
        send_callback: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        self._config = config
        self._store = credit_store
        self._send = send_callback
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """Start the HTTP server."""
        base = self._config.webhook_base_path.rstrip("/")
        app = web.Application()
        app.router.add_post(f"{base}/webhook/stripe", self._handle_stripe_webhook)
        app.router.add_get(f"{base}/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._config.webhook_port)
        await site.start()
        logger.info(
            f"Webhook server listening on port {self._config.webhook_port} "
            f"(path: {base}/webhook/stripe)"
        )

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Webhook server stopped")

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({"status": "ok"})

    async def _handle_stripe_webhook(self, request: web.Request) -> web.Response:
        """Handle Stripe webhook events."""
        payload = await request.read()
        sig_header = request.headers.get("Stripe-Signature", "")

        if not sig_header:
            return web.Response(status=400, text="Missing Stripe-Signature header")

        # Verify signature
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, self._config.stripe_webhook_secret,
            )
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"Webhook signature verification failed: {e}")
            return web.Response(status=400, text="Invalid signature")
        except Exception as e:
            logger.error(f"Webhook verification error: {e}")
            return web.Response(status=400, text="Verification error")

        # Route event
        if event["type"] == "checkout.session.completed":
            await self._handle_checkout_completed(event["data"]["object"])
        else:
            logger.debug(f"Ignoring webhook event type: {event['type']}")

        return web.Response(status=200, text="ok")

    async def _handle_checkout_completed(self, session_data: dict[str, Any]) -> None:
        """Process a completed checkout session."""
        metadata = session_data.get("metadata", {})
        chat_id = metadata.get("chat_id")
        channel = metadata.get("channel", "telegram")
        credits_str = metadata.get("credits", "0")
        stripe_session_id = session_data.get("id", "")

        if not chat_id:
            logger.error(f"Webhook missing chat_id in metadata: {stripe_session_id}")
            return

        credits = int(credits_str)
        if credits <= 0:
            logger.error(f"Invalid credits amount: {credits_str}")
            return

        # Idempotency check
        if await self._store.has_processed_session(stripe_session_id):
            logger.info(f"Duplicate webhook for session {stripe_session_id}, skipping")
            return

        # Add credits
        new_balance = await self._store.add_credits(
            chat_id=chat_id,
            amount=credits,
            channel=channel,
            stripe_session_id=stripe_session_id,
        )

        amount_cents = session_data.get("amount_total", 0)
        amount_display = f"${amount_cents / 100:.2f}" if amount_cents else ""

        logger.info(
            f"Payment processed: {credits} credits for {channel}:{chat_id} "
            f"(balance: {new_balance}, amount: {amount_display}, session: {stripe_session_id})"
        )

        # Notify user via channel
        await self._send(OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=(
                f"Payment received! {amount_display}\n\n"
                f"Added **{credits}** credits to your account.\n"
                f"Remaining balance: **{new_balance}** credits.\n\n"
                f"Send me your interview question to get started!"
            ),
        ))
