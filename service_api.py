from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

import env_config  # Load the shared local .env before modules read environment settings.
from assistant import get_customer_list, process_customer_query
from database import DEFAULT_DB_PATH, connect, get_settings, initialize_database
from whatsapp_client import (
    WhatsAppApiError,
    normalize_phone_number,
    send_whatsapp_message,
)


logger = logging.getLogger(__name__)


def create_app(db_path: str | Path = DEFAULT_DB_PATH) -> FastAPI:
    database_path = str(db_path)
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        initialize_database(database_path)
        yield

    api = FastAPI(
        title="Neha Opticals demo data API",
        description=(
            "REST endpoints backed by the Neha Opticals demo SQLite database, "
            "including WhatsApp Cloud API webhook handling."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    @api.get("/health", tags=["service"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/webhook", tags=["whatsapp"])
    def verify_whatsapp_webhook(
        mode: str | None = Query(default=None, alias="hub.mode"),
        verify_token: str | None = Query(default=None, alias="hub.verify_token"),
        challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> PlainTextResponse:
        expected_token = os.environ.get("WHATSAPP_VERIFY_TOKEN")
        if not expected_token:
            raise HTTPException(
                status_code=503,
                detail="WHATSAPP_VERIFY_TOKEN is not configured.",
            )
        if (
            mode != "subscribe"
            or verify_token is None
            or not hmac.compare_digest(verify_token, expected_token)
            or challenge is None
        ):
            raise HTTPException(status_code=403, detail="Webhook verification failed.")
        return PlainTextResponse(challenge)

    @api.post("/webhook", tags=["whatsapp"])
    async def receive_whatsapp_webhook(request: Request) -> dict[str, Any]:
        app_secret = os.environ.get("WHATSAPP_APP_SECRET")
        if not app_secret:
            raise HTTPException(
                status_code=503,
                detail="WHATSAPP_APP_SECRET is required to validate webhook requests.",
            )

        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected_signature = "sha256=" + hmac.new(
            app_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise HTTPException(status_code=403, detail="Webhook signature is invalid.")

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise HTTPException(
                status_code=400,
                detail="Webhook body must be valid JSON.",
            ) from error
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail="Webhook body must be a JSON object.",
            )
        entries = payload.get("entry", [])
        if not isinstance(entries, list):
            raise HTTPException(
                status_code=400,
                detail="Webhook entry must be a JSON array.",
            )

        processed = 0
        ignored = 0
        configured_phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
        customer_by_phone: dict[str, dict[str, str]] | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                ignored += 1
                continue
            changes = entry.get("changes", [])
            if not isinstance(changes, list):
                ignored += 1
                continue
            for change in changes:
                if not isinstance(change, dict):
                    ignored += 1
                    continue
                value = change.get("value", {})
                if not isinstance(value, dict):
                    ignored += 1
                    continue

                metadata = value.get("metadata", {})
                incoming_phone_number_id = (
                    metadata.get("phone_number_id")
                    if isinstance(metadata, dict)
                    else None
                )
                messages = value.get("messages", [])
                if not isinstance(messages, list):
                    ignored += 1
                    continue
                if (
                    configured_phone_number_id
                    and incoming_phone_number_id != configured_phone_number_id
                ):
                    ignored += len(messages)
                    continue

                if customer_by_phone is None:
                    customer_by_phone = {
                        normalize_phone_number(customer["phone"]): customer
                        for customer in get_customer_list(database_path)
                    }
                for message in messages:
                    if not isinstance(message, dict) or message.get("type") != "text":
                        ignored += 1
                        continue
                    sender = message.get("from")
                    text = message.get("text", {})
                    message_text = text.get("body") if isinstance(text, dict) else None
                    if (
                        not isinstance(sender, str)
                        or not isinstance(message_text, str)
                        or not message_text.strip()
                    ):
                        ignored += 1
                        continue

                    customer = customer_by_phone.get(normalize_phone_number(sender))
                    if customer is None:
                        ignored += 1
                        continue

                    result = process_customer_query(
                        customer["id"],
                        message_text,
                        get_settings(database_path),
                        api_key=os.environ.get("OPENROUTER_API_KEY"),
                        db_path=database_path,
                    )
                    try:
                        send_whatsapp_message(sender, result.message)
                    except WhatsAppApiError as error:
                        logger.exception("Could not send a WhatsApp webhook reply.")
                        raise HTTPException(
                            status_code=502,
                            detail=f"Could not send the WhatsApp reply: {error}",
                        ) from error
                    processed += 1

        return {"status": "ok", "processed": processed, "ignored": ignored}

    @api.get("/api/v1/catalog/frames", tags=["catalog"])
    def list_frames() -> list[dict[str, Any]]:
        with connect(database_path) as connection:
            rows = connection.execute(
                """
                SELECT id, name, material, price_inr, description
                FROM frame_catalog ORDER BY price_inr
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @api.get("/api/v1/store", tags=["store"])
    def get_store() -> dict[str, str]:
        with connect(database_path) as connection:
            rows = connection.execute(
                "SELECT key, value FROM store_info ORDER BY key"
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    @api.get(
        "/api/v1/customers/{customer_id}/delivery-quote",
        tags=["delivery"],
    )
    def get_customer_delivery_quote(
        customer_id: str,
        order_id: str | None = Query(default=None, pattern=r"^NC-\d{4}$"),
    ) -> dict[str, Any]:
        with connect(database_path) as connection:
            if order_id:
                order = connection.execute(
                    """
                    SELECT id, status
                    FROM orders
                    WHERE id = ? AND customer_id = ?
                    """,
                    (order_id, customer_id),
                ).fetchone()
            else:
                order = connection.execute(
                    """
                    SELECT id, status
                    FROM orders
                    WHERE customer_id = ? AND status != 'Collected'
                    ORDER BY order_date DESC
                    LIMIT 1
                    """,
                    (customer_id,),
                ).fetchone()

        if order is None:
            return {"order": None, "quote": None}

        order_data = dict(order)
        quote = {
            "order_id": order_data["id"],
            "status": order_data["status"],
            "available": order_data["status"] == "Ready for Delivery",
            "fee_inr": 79,
            "eta": "2–4 hours after dispatch",
            "service": "Dunzo (demo quote)",
        }
        return {"order": order_data, "quote": quote}

    return api


app = create_app(
    os.environ.get("OPTICALS_DB_PATH", str(DEFAULT_DB_PATH))
)
