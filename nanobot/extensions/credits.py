"""Credit gating extension: manages user credits and purchase flows."""

from typing import Any

from loguru import logger

from nanobot.extensions.base import Extension, ExtensionContext


class CreditExtension(Extension):
    """Extension that gates LLM access behind a credit system.

    Hooks:
        pre_process: blocks zero-credit users with a purchase prompt.
        transform_messages: injects credit balance into system prompt.
        transform_response: deducts 1 credit per successful answer.

    Config injection: The gateway startup injects ``_payments_config`` and
    ``_credit_store`` into the extension options dict so this extension shares
    the same CreditStore instance as the webhook server.
    """

    name = "credits"

    def __init__(self) -> None:
        self._store: Any = None  # CreditStore (lazy import to avoid hard dep)
        self._checkout: Any = None  # StripeCheckout
        self._config: Any = None  # PaymentsConfig
        self._enabled = False

    async def on_load(self, config: dict[str, Any]) -> None:
        """Initialize store and Stripe client from injected config."""
        from nanobot.config.schema import PaymentsConfig

        self._config = config.get("_payments_config")
        self._store = config.get("_credit_store")

        if not isinstance(self._config, PaymentsConfig) or not self._config.enabled:
            logger.info("Credit extension: payments not enabled, running in passthrough mode")
            return

        # If no injected store (standalone use), create our own
        if self._store is None:
            from nanobot.store.credits import CreditStore
            self._store = CreditStore()
            await self._store.initialize()

        # Initialize Stripe checkout if API key is set
        if self._config.stripe_api_key:
            from nanobot.payments.stripe_checkout import StripeCheckout
            self._checkout = StripeCheckout(self._config)

        self._enabled = True
        logger.info(
            f"Credit extension loaded: {self._config.free_credits} free credits, "
            f"{len(self._config.credit_packs)} packs configured"
        )

    async def pre_process(
        self, msg: Any, session: Any, ctx: ExtensionContext,
    ) -> str | None:
        """Check credits before LLM processing. Return purchase message if 0 credits."""
        if not self._enabled:
            return None

        # Don't gate /start command — let the welcome flow through
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content.strip().startswith("/start"):
            # Still ensure user exists (for free credit grant)
            await self._store.get_or_create_user(
                chat_id=ctx.chat_id,
                channel=ctx.channel,
                free_credits=self._config.free_credits,
            )
            return None

        # Get or create user (grants free credits to new users)
        credits, is_new = await self._store.get_or_create_user(
            chat_id=ctx.chat_id,
            channel=ctx.channel,
            free_credits=self._config.free_credits,
        )

        if is_new:
            logger.info(
                f"New user {ctx.channel}:{ctx.chat_id} granted "
                f"{self._config.free_credits} free credits"
            )

        if credits > 0:
            return None  # Proceed to LLM

        # No credits — return purchase message
        return self._build_purchase_message(ctx.chat_id, ctx.channel)

    async def transform_messages(
        self, messages: list[dict[str, Any]], ctx: ExtensionContext,
    ) -> list[dict[str, Any]]:
        """Inject credit balance into system prompt."""
        if not self._enabled:
            return messages

        credits = await self._store.get_credits(ctx.chat_id, ctx.channel)

        # Find the system message and append credit info
        for msg in messages:
            if msg.get("role") == "system":
                low_warning = ""
                if credits <= 3:
                    low_warning = (
                        " Credits running low — remind the user they can "
                        "purchase more after this answer."
                    )
                msg["content"] += (
                    f"\n\n## Credits\n"
                    f"User has {credits} credits remaining. "
                    f"Each answer costs 1 credit.{low_warning}"
                )
                break

        return messages

    async def transform_response(self, content: str, ctx: ExtensionContext) -> str:
        """Deduct 1 credit after successful LLM response."""
        if not self._enabled:
            return content

        success = await self._store.deduct_credit(ctx.chat_id, ctx.channel)
        if success:
            new_balance = await self._store.get_credits(ctx.chat_id, ctx.channel)
            logger.debug(
                f"Deducted 1 credit from {ctx.channel}:{ctx.chat_id} "
                f"(balance: {new_balance})"
            )

            if 0 < new_balance <= 3:
                content += f"\n\n---\n_{new_balance} credits remaining_"
            elif new_balance == 0:
                content += (
                    "\n\n---\n_This was your last credit! "
                    "Send any message to see purchase options._"
                )
        else:
            logger.warning(f"Credit deduction failed for {ctx.channel}:{ctx.chat_id}")

        return content

    def _build_purchase_message(self, chat_id: str, channel: str) -> str:
        """Build the purchase prompt with Stripe checkout links."""
        if not self._checkout or not self._config:
            return (
                "You've used all your free answers!\n\n"
                "Payment is not currently configured. "
                "Please contact the bot administrator."
            )

        lines = [
            "You've used all your credits!\n",
            "Purchase more to continue getting answers:\n",
        ]

        for pack in self._config.credit_packs:
            try:
                url = self._checkout.create_checkout_url(
                    pack=pack,
                    chat_id=chat_id,
                    channel=channel,
                )
                lines.append(f"  {pack.label} — [Buy Now]({url})")
            except Exception as e:
                logger.error(f"Failed to create checkout URL for {pack.label}: {e}")
                lines.append(f"  {pack.label} — (temporarily unavailable)")

        lines.append("\nAfter payment, your credits will be added automatically!")
        return "\n".join(lines)
