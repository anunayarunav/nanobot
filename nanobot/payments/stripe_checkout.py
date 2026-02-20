"""Stripe Checkout integration for credit purchases."""

import stripe
from loguru import logger

from nanobot.config.schema import CreditPack, PaymentsConfig


class StripeCheckout:
    """Creates Stripe Checkout Sessions for credit pack purchases."""

    def __init__(self, config: PaymentsConfig) -> None:
        self._config = config
        stripe.api_key = config.stripe_api_key

    def create_checkout_url(
        self,
        pack: CreditPack,
        chat_id: str,
        channel: str = "telegram",
    ) -> str:
        """Create a Stripe Checkout Session and return the URL.

        The session metadata carries chat_id/channel/credits so the webhook
        handler knows which user to credit after payment.
        """
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pack.price_cents,
                    "product_data": {
                        "name": f"{pack.credits} Answer Credits",
                        "description": pack.label or f"{pack.credits} credits",
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=self._config.success_url or "https://t.me/",
            cancel_url=self._config.cancel_url or "https://t.me/",
            metadata={
                "chat_id": chat_id,
                "channel": channel,
                "credits": str(pack.credits),
            },
        )
        logger.debug(f"Created Stripe checkout session {session.id} for {channel}:{chat_id}")
        return session.url
