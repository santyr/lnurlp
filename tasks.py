import asyncio
import json

import httpx
import websockets
from lnbits.core.crud import get_payment, update_payment
from lnbits.core.models import Payment
from lnbits.tasks import register_invoice_listener
from loguru import logger
from pynostr.event import Event

from .crud import get_or_create_lnurlp_settings, get_pay_link
from .models import PayLink


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_lnurlp")

    while True:
        payment = await invoice_queue.get()
        await on_invoice_paid(payment)


async def on_invoice_paid(payment: Payment):
    if not payment.extra or payment.extra.get("tag") != "lnurlp":
        return

    if payment.extra.get("wh_status"):
        # this webhook has already been sent
        return

    pay_link_id = payment.extra.get("link")
    if not pay_link_id:
        logger.error("Invoice paid. But no pay link id found.")
        return

    pay_link = await get_pay_link(pay_link_id)
    if not pay_link:
        logger.error(f"Invoice paid. But Pay link `{pay_link_id}` not found.")
        return

    zap_receipt = None
    if pay_link.zaps:
        try:
            zap_receipt = await send_zap(payment)
        except Exception as exc:
            logger.error(f"Failed to create/send zap receipt: {exc}")
            # Continue to send webhook even if zap receipt fails

    await send_webhook(
        payment, pay_link, zap_receipt.to_message() if zap_receipt else None
    )


async def send_webhook(payment: Payment, pay_link: PayLink, zap_receipt=None):
    if not pay_link.webhook_url:
        return

    async with httpx.AsyncClient() as client:
        try:
            r: httpx.Response = await client.post(
                pay_link.webhook_url,
                json={
                    "payment_hash": payment.payment_hash,
                    "payment_request": payment.bolt11,
                    "amount": payment.amount,
                    "comment": payment.extra.get("comment") if payment.extra else None,
                    "webhook_data": (
                        payment.extra.get("webhook_data") if payment.extra else None
                    ),
                    "lnurlp": pay_link.id,
                    "body": (
                        json.loads(pay_link.webhook_body)
                        if pay_link.webhook_body
                        else ""
                    ),
                    "zap_receipt": zap_receipt or "",
                },
                headers=(
                    json.loads(pay_link.webhook_headers)
                    if pay_link.webhook_headers
                    else None
                ),
                timeout=6,
            )
            await mark_webhook_sent(
                payment.checking_id,
                r.status_code,
                r.is_success,
                r.reason_phrase,
                r.text,
            )
        except Exception as exc:
            logger.error(exc)
            await mark_webhook_sent(
                payment.checking_id, -1, False, "Unexpected Error", str(exc)
            )


async def mark_webhook_sent(
    checking_id: str, status: int, is_success: bool, reason_phrase="", text=""
) -> None:
    payment = await get_payment(checking_id)
    extra = payment.extra or {}
    extra["wh_status"] = status  # keep for backwards compability
    extra["wh_success"] = is_success
    extra["wh_message"] = reason_phrase
    extra["wh_response"] = text
    payment.extra = extra
    await update_payment(payment)


# NIP-57 - load the zap request
async def send_zap(payment: Payment):
    nostr = payment.extra.get("nostr") if payment.extra else None
    if not nostr:
        return None

    # Parse the nostr zap request event
    try:
        event_json = json.loads(nostr)
    except json.JSONDecodeError as exc:
        logger.error(f"Failed to parse nostr zap request JSON: {exc}")
        return None

    # Validate basic event structure
    if not isinstance(event_json, dict):
        logger.error("Nostr zap request is not a valid JSON object")
        return None

    if "tags" not in event_json or not isinstance(event_json["tags"], list):
        logger.error("Nostr zap request missing 'tags' array")
        return None

    def get_tag(event: dict, tag_name: str):
        """Extract tag values from nostr event, returning None if not found."""
        try:
            res = [
                event_tag[1:]
                for event_tag in event["tags"]
                if isinstance(event_tag, list)
                and len(event_tag) >= 2
                and event_tag[0] == tag_name
            ]
            return res[0] if res else None
        except (KeyError, TypeError, IndexError) as exc:
            logger.warning(f"Error extracting tag '{tag_name}': {exc}")
            return None

    tags = []
    for t in ["p", "e", "a"]:
        tag = get_tag(event_json, t)
        if tag and len(tag) > 0:
            tags.append([t, tag[0]])
    tags.append(["bolt11", payment.bolt11])
    tags.append(["description", nostr])

    pubkey = next((pk[1] for pk in tags if pk[0] == "p"), None)
    if not pubkey:
        logger.error("Cannot create zap receipt: recipient pubkey ('p' tag) is missing")
        return None

    zap_receipt = Event(
        kind=9735,
        tags=tags,
        content="",
    )

    settings = await get_or_create_lnurlp_settings()
    zap_receipt.sign(settings.private_key.hex())

    async def send_to_relay(relay_url: str, event_message: str):
        """Helper function to send an event to a single relay."""
        # Validate relay URL format
        if not isinstance(relay_url, str) or not relay_url.startswith(("ws://", "wss://")):
            logger.warning(f"Invalid relay URL, skipping: {relay_url}")
            return
        try:
            async with websockets.connect(relay_url, open_timeout=5) as websocket:
                logger.debug(f"Sending zap to {relay_url}")
                await websocket.send(event_message)
        except Exception as e:
            logger.warning(f"Failed to send zap to {relay_url}: {e}")

    # Get relays from the zap request, with a reasonable limit
    relays = get_tag(event_json, "relays")
    if not relays:
        return zap_receipt

    # Filter to valid relays only and limit count
    valid_relays = [
        r for r in relays
        if isinstance(r, str) and r.startswith(("ws://", "wss://"))
    ][:50]

    if not valid_relays:
        logger.warning("No valid relay URLs found in zap request")
        return zap_receipt

    # Run all tasks concurrently. This is a "fire-and-forget" approach.
    # We don't need to wait for all of them to complete here.
    _ = [
        asyncio.create_task(send_to_relay(relay, zap_receipt.to_message()))
        for relay in valid_relays
    ]

    return zap_receipt
