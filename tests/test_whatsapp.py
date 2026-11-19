from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from whatsapp_client import WhatsAppApiError, send_whatsapp_message


class WhatsAppClientTests(unittest.TestCase):
    def test_sends_cloud_api_text_message(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"messages":[{"id":"wamid.test"}]}'
        with (
            patch.dict(
                os.environ,
                {
                    "WHATSAPP_ACCESS_TOKEN": "test-token",
                    "WHATSAPP_PHONE_NUMBER_ID": "12345",
                },
            ),
            patch("whatsapp_client.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            sent = send_whatsapp_message(
                "+1 (555) 172-1459",
                "Your order is ready.",
            )

        self.assertTrue(sent)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://graph.facebook.com/v18.0/12345/messages",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(
            json.loads(request.data),
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": "15551721459",
                "type": "text",
                "text": {
                    "preview_url": False,
                    "body": "Your order is ready.",
                },
            },
        )
        urlopen.assert_called_once()

    def test_without_access_token_uses_simulated_path(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "WHATSAPP_ACCESS_TOKEN": "",
                    "WHATSAPP_PHONE_NUMBER_ID": "",
                },
            ),
            patch("whatsapp_client.urllib.request.urlopen") as urlopen,
        ):
            sent = send_whatsapp_message("+15551721459", "A test message.")

        self.assertFalse(sent)
        urlopen.assert_not_called()

    def test_access_token_requires_phone_number_id(self) -> None:
        with patch.dict(
            os.environ,
            {
                "WHATSAPP_ACCESS_TOKEN": "test-token",
                "WHATSAPP_PHONE_NUMBER_ID": "",
            },
        ):
            with self.assertRaisesRegex(WhatsAppApiError, "WHATSAPP_PHONE_NUMBER_ID"):
                send_whatsapp_message("+15551721459", "A test message.")


if __name__ == "__main__":
    unittest.main()
