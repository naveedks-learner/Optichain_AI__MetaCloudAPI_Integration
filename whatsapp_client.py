from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


GRAPH_API_VERSION = "v18.0"


class WhatsAppApiError(RuntimeError):
    pass


def normalize_phone_number(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def send_whatsapp_message(phone: str, message: str) -> bool:
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not access_token:
        return False

    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not phone_number_id:
        raise WhatsAppApiError(
            "WHATSAPP_PHONE_NUMBER_ID must be set when WhatsApp sending is enabled."
        )

    recipient = normalize_phone_number(phone)
    if not recipient:
        raise ValueError("The WhatsApp recipient phone number is invalid.")
    if not message.strip():
        raise ValueError("The WhatsApp message cannot be empty.")

    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{phone_number_id}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise WhatsAppApiError(
            f"WhatsApp Cloud API returned HTTP {error.code}: {detail}"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise WhatsAppApiError(
            f"Unable to reach the WhatsApp Cloud API: {error}"
        ) from error

    return True
