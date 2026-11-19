from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import uvicorn

from assistant import OpenRouterCompletion, process_customer_query
from database import connect, get_settings, initialize_database
from service_api import create_app


class RestApiAssistantIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "rest-api-test.sqlite3")
        initialize_database(self.db_path)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.port = listener.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_app(self.db_path),
                log_level="critical",
                lifespan="on",
            )
        )
        self.server_thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [listener]},
            daemon=True,
        )
        self.server_thread.start()
        self.api_env = patch.dict(
            os.environ,
            {"NEHA_API_BASE_URL": f"http://127.0.0.1:{self.port}"},
        )
        self.api_env.start()
        self.addCleanup(self.api_env.stop)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.server.started:
            time.sleep(0.02)
        self.assertTrue(self.server.started, "REST API server did not start.")
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.should_exit = True
        self.server_thread.join(timeout=5)
        self.temp_dir.cleanup()

    def test_api_query_uses_http_json_result_and_logs_rest_source(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/v1/catalog/frames",
            timeout=3,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Flex titanium", response.read())

        result = process_customer_query(
            "C001",
            "What frame materials and prices do you have?",
            get_settings(self.db_path),
            db_path=self.db_path,
        )
        self.assertIn("Flex titanium", result.message)
        self.assertEqual(result.source, "rest_api")
        with connect(self.db_path) as connection:
            event = connection.execute(
                "SELECT * FROM query_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            tool_call = connection.execute(
                "SELECT * FROM tool_calls ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event["source"], "rest_api")
        self.assertEqual(event["answered"], 1)
        self.assertEqual(tool_call["source"], "rest_api")
        self.assertEqual(tool_call["succeeded"], 1)
        self.assertEqual(
            tool_call["endpoint"],
            "GET /api/v1/catalog/frames",
        )

    def test_delivery_quote_from_api_checks_readiness(self) -> None:
        result = process_customer_query(
            "C002",
            "Can Dunzo deliver order NC-2402?",
            get_settings(self.db_path),
            db_path=self.db_path,
        )
        self.assertIn("not ready for dispatch", result.message)
        self.assertEqual(result.source, "rest_api")

    def test_delivery_api_scopes_order_to_the_customer(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/v1/customers/C002/"
            "delivery-quote?order_id=NC-2401",
            timeout=3,
        ) as response:
            payload = response.read().decode("utf-8")
        self.assertIn('"order":null', payload)
        self.assertNotIn("Ready for Delivery", payload)

        result = process_customer_query(
            "C001",
            "Can Dunzo deliver order NC-2401?",
            get_settings(self.db_path),
            db_path=self.db_path,
        )
        self.assertIn("₹79", result.message)
        self.assertEqual(result.source, "rest_api")

    def test_compound_collection_and_hours_query_answers_both_parts(self) -> None:
        result = process_customer_query(
            "C002",
            "Can I come tomorrow and collect it, and also tell me what time you open?",
            get_settings(self.db_path),
            db_path=self.db_path,
        )

        self.assertIn("NC-2402", result.message)
        self.assertIn("not marked ready for collection", result.message)
        self.assertIn("can’t confirm it will be ready", result.message)
        self.assertIn("can’t verify", result.message)
        self.assertEqual(result.source, "database")

    def test_ready_order_compound_question_uses_web_search_and_cites_source(self) -> None:
        settings = get_settings(self.db_path)
        settings.update({"use_llm": True, "use_web_search": True})
        with patch(
            "assistant._call_openrouter",
            return_value=OpenRouterCompletion(
                "Order NC-2401 is ready. Tomorrow's hours are listed on "
                "[Neha Opticals](https://nehaopticals.com/stores/jp-nagar-bangalore/).",
                100,
                35,
                web_search_used=True,
            ),
        ) as call:
            result = process_customer_query(
                "C001",
                "Can I collect order NC-2401 tomorrow and what time do you open?",
                settings,
                api_key="test-key",
                db_path=self.db_path,
            )

        self.assertIn("NC-2401", result.message)
        self.assertIn("nehaopticals.com", result.message)
        self.assertEqual(result.source, "database+web_search")
        self.assertTrue(call.call_args.kwargs["use_web_search"])

    def test_webhook_verification_returns_challenge_for_matching_token(self) -> None:
        with patch.dict(os.environ, {"WHATSAPP_VERIFY_TOKEN": "test-verify-token"}):
            url = (
                f"http://127.0.0.1:{self.port}/webhook?hub.mode=subscribe"
                "&hub.verify_token=test-verify-token&hub.challenge=challenge-value"
            )
            with urllib.request.urlopen(url, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"challenge-value")

    def test_webhook_processes_known_customer_text_and_sends_reply(self) -> None:
        app_secret = "test-app-secret"
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "test-phone-number-id"},
                                "messages": [
                                    {
                                        "from": "919876543210",
                                        "id": "wamid.test-message-id",
                                        "type": "text",
                                        "text": {"body": "What is the status of NC-2401?"},
                                    }
                                ],
                            },
                        }
                    ]
                }
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        signature = "sha256=" + hmac.new(
            app_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": signature,
            },
            method="POST",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "WHATSAPP_APP_SECRET": app_secret,
                    "WHATSAPP_ACCESS_TOKEN": "test-token",
                    "WHATSAPP_PHONE_NUMBER_ID": "test-phone-number-id",
                },
            ),
            patch("service_api.send_whatsapp_message", return_value=True) as send,
            urllib.request.urlopen(request, timeout=3) as response,
        ):
            result = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(result, {"status": "ok", "processed": 1, "ignored": 0})
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "919876543210")
        self.assertIn("NC-2401", send.call_args.args[1])

    def test_webhook_rejects_invalid_signature(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/webhook",
            data=b'{"entry":[]}',
            headers={"X-Hub-Signature-256": "sha256=invalid"},
            method="POST",
        )
        with patch.dict(os.environ, {"WHATSAPP_APP_SECRET": "test-app-secret"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
