from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from assistant import (
    OpenRouterCompletion,
    _call_openrouter,
    check_openrouter_connection,
    clear_customer_chat,
    process_customer_query,
    record_simulated_outbound_message,
    update_order_financials,
    update_order_status,
)
from database import connect, get_settings, initialize_database
from rest_api_client import ApiServiceError


class NehaOpticalsDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test.sqlite3")
        initialize_database(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_customer_query_returns_scoped_order_status_and_logs_cost(self) -> None:
        settings = get_settings(self.db_path)
        result = process_customer_query(
            "C001",
            "Can I collect order NC-2401?",
            settings,
            db_path=self.db_path,
        )
        self.assertIn("Ready for Delivery", result.message)
        self.assertIn("NC-2401", result.message)
        with connect(self.db_path) as connection:
            event = connection.execute("SELECT * FROM query_events").fetchone()
            outbound = connection.execute(
                "SELECT * FROM messages WHERE direction = 'outbound' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event["answered"], 1)
        self.assertEqual(event["source"], "database")
        self.assertGreaterEqual(event["response_ms"], 0)
        self.assertGreater(event["cost_usd"], 0)
        self.assertEqual(outbound["cost_is_estimate"], 1)
        with connect(self.db_path) as connection:
            tool_call_count = connection.execute(
                "SELECT COUNT(*) AS count FROM tool_calls"
            ).fetchone()["count"]
        self.assertEqual(tool_call_count, 0)

    def test_customer_cannot_read_another_customers_order(self) -> None:
        result = process_customer_query(
            "C002",
            "What is the status of NC-2401?",
            get_settings(self.db_path),
            db_path=self.db_path,
        )
        self.assertIn("couldn’t find an order", result.message)

    def test_customer_pricing_query_uses_scoped_order_financials(self) -> None:
        result = process_customer_query(
            "C001",
            "What is my order total and balance pending?",
            get_settings(self.db_path),
            db_path=self.db_path,
        )
        self.assertIn("₹12,499", result.message)
        self.assertIn("₹2,500", result.message)
        self.assertEqual(result.intent, "order pricing")

    def test_customer_cannot_read_another_customers_order_financials(self) -> None:
        result = process_customer_query(
            "C002",
            "What is my balance for order NC-2401?",
            get_settings(self.db_path),
            db_path=self.db_path,
        )
        self.assertIn("couldn’t find an active order", result.message)
        self.assertNotIn("₹12,499", result.message)

    def test_order_financials_can_be_updated_and_validate_balance(self) -> None:
        update_order_financials("NC-2401", 15000, 3000, self.db_path)
        with connect(self.db_path) as connection:
            order = connection.execute(
                "SELECT total_amount_inr, balance_due_inr FROM orders WHERE id = 'NC-2401'"
            ).fetchone()
        self.assertEqual(order["total_amount_inr"], 15000)
        self.assertEqual(order["balance_due_inr"], 3000)

        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            update_order_financials("NC-2401", 1000, 1001, self.db_path)

    def test_initialization_migrates_existing_order_financials(self) -> None:
        legacy_path = str(Path(self.temp_dir.name) / "legacy.sqlite3")
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE customers (
                    id TEXT PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    phone TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE orders (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    frame_name TEXT NOT NULL,
                    prescription_type TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    promised_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO customers VALUES
                    ('C001', 'Demo Customer', '+91 90000 00001', '2026-01-01');
                INSERT INTO orders VALUES
                    ('NC-2401', 'C001', 'Demo frame', 'Single vision',
                     '2026-01-01', '2026-01-03', 'Ready for Delivery', '2026-01-01');
                """
            )
        finally:
            connection.close()

        initialize_database(legacy_path)

        with connect(legacy_path) as connection:
            order = connection.execute(
                "SELECT total_amount_inr, balance_due_inr FROM orders WHERE id = 'NC-2401'"
            ).fetchone()
        self.assertEqual(order["total_amount_inr"], 12499)
        self.assertEqual(order["balance_due_inr"], 2500)

    def test_ready_status_change_falls_back_to_one_simulated_whatsapp_notice(self) -> None:
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": ""}):
            notice = update_order_status("NC-2402", "Ready for Delivery", self.db_path)
        self.assertIsNotNone(notice)
        self.assertIn("Dunzo", notice or "")
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": ""}):
            self.assertIsNone(
                update_order_status("NC-2402", "Ready for Delivery", self.db_path)
            )
        with connect(self.db_path) as connection:
            messages = connection.execute(
                """
                SELECT COUNT(*) AS count FROM messages
                WHERE order_id = 'NC-2402' AND direction = 'outbound'
                """
            ).fetchone()
            history = connection.execute(
                "SELECT COUNT(*) AS count FROM order_status_history WHERE order_id = 'NC-2402'"
            ).fetchone()
        self.assertEqual(messages["count"], 1)
        self.assertEqual(history["count"], 2)

    def test_ready_status_change_sends_cloud_message_when_configured(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "WHATSAPP_ACCESS_TOKEN": "test-token",
                    "WHATSAPP_PHONE_NUMBER_ID": "test-phone-number-id",
                },
            ),
            patch("assistant.send_whatsapp_message", return_value=True) as send,
        ):
            notice = update_order_status("NC-2402", "Ready for Delivery", self.db_path)

        self.assertIsNotNone(notice)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "+91 98201 23456")
        self.assertIn("NC-2402", send.call_args.args[1])
        with connect(self.db_path) as connection:
            message = connection.execute(
                """
                SELECT direction, content FROM messages
                WHERE order_id = 'NC-2402' AND direction = 'outbound'
                """
            ).fetchone()
        self.assertEqual(message["direction"], "outbound")
        self.assertEqual(message["content"], notice)

    def test_initialization_refreshes_legacy_seeded_replies(self) -> None:
        placeholder = "This is a seeded demo response. Start a new conversation to see a live demo reply."
        with connect(self.db_path) as connection:
            reply_id = connection.execute(
                "SELECT id FROM messages WHERE direction = 'outbound' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            connection.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (placeholder, reply_id),
            )

        initialize_database(self.db_path)

        with connect(self.db_path) as connection:
            updated_reply = connection.execute(
                "SELECT content FROM messages WHERE id = ?",
                (reply_id,),
            ).fetchone()["content"]
        self.assertNotEqual(updated_reply, placeholder)
        self.assertIn("ready for collection", updated_reply)

    def test_initialization_migrates_seeded_store_info_and_preserves_custom_values(self) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO store_info (key, value) VALUES ('hours_weekday', ?)",
                ("Monday to Saturday, 10:00 AM to 8:00 PM",),
            )
            connection.execute(
                "INSERT INTO store_info (key, value) VALUES ('hours_sunday', ?)",
                ("Sunday, 11:00 AM to 5:00 PM",),
            )
            connection.execute(
                "INSERT INTO store_info (key, value) VALUES ('hours_source', ?)",
                ("https://drsandhyaseyeclinic.setmore.com/",),
            )
            connection.execute(
                "UPDATE store_info SET value = ? WHERE key = 'address'",
                ("Shop 12, Lake View Road, Powai, Mumbai 400076",),
            )

        initialize_database(self.db_path)

        with connect(self.db_path) as connection:
            removed_hours = connection.execute(
                "SELECT COUNT(*) AS count FROM store_info WHERE key LIKE 'hours_%'"
            ).fetchone()["count"]
            migrated_address = connection.execute(
                "SELECT value FROM store_info WHERE key = 'address'"
            ).fetchone()["value"]
            connection.execute(
                "INSERT INTO store_info (key, value) VALUES ('hours_weekday', ?)",
                ("9:00 AM–5:00 PM",),
            )
            connection.execute(
                "UPDATE store_info SET value = ? WHERE key = 'address'",
                ("Custom branch address",),
            )

        self.assertEqual(removed_hours, 0)
        self.assertIn("JP Nagar", migrated_address)
        initialize_database(self.db_path)
        with connect(self.db_path) as connection:
            custom_hours = connection.execute(
                "SELECT value FROM store_info WHERE key = 'hours_weekday'"
            ).fetchone()["value"]
            custom_address = connection.execute(
                "SELECT value FROM store_info WHERE key = 'address'"
            ).fetchone()["value"]
        self.assertEqual(custom_hours, "9:00 AM–5:00 PM")
        self.assertEqual(custom_address, "Custom branch address")

    def test_openrouter_connection_check_sends_no_customer_context(self) -> None:
        with patch(
            "assistant._call_openrouter",
            return_value=OpenRouterCompletion("Hello, I am ready.", 18, 7),
        ) as call:
            result = check_openrouter_connection("test-key", "openai/gpt-4o-mini")

        self.assertEqual(result, ("Hello, I am ready.", 18, 7))
        query, context, model, api_key = call.call_args.args
        self.assertIn("connection test", query)
        self.assertEqual(context, {"purpose": "Provider connectivity test only. No customer data."})
        self.assertEqual(model, "openai/gpt-4o-mini")
        self.assertEqual(api_key, "test-key")

    def test_openrouter_web_search_uses_responses_api_and_approved_domains(self) -> None:
        fake_response = MagicMock()
        fake_response.__enter__.return_value.read.return_value = json.dumps(
            {
                "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Hours are on [Neha Opticals](https://nehaopticals.com/).",
                            }
                        ],
                    },
                ],
                "usage": {"input_tokens": 70, "output_tokens": 15},
            }
        ).encode("utf-8")
        with patch("assistant.urllib.request.urlopen", return_value=fake_response) as open_url:
            response = _call_openrouter(
                "What time do you open?",
                {},
                "openai/gpt-4o-mini",
                "test-key",
                use_web_search=True,
            )

        request = open_url.call_args.args[0]
        request_body = json.loads(request.data)
        self.assertTrue(request.full_url.endswith("/api/v1/responses"))
        self.assertEqual(response.message, "Hours are on [Neha Opticals](https://nehaopticals.com/).")
        self.assertEqual(response.input_tokens, 70)
        self.assertEqual(response.output_tokens, 15)
        self.assertTrue(response.web_search_used)
        self.assertEqual(
            request_body["tools"][0]["parameters"]["allowed_domains"],
            [
                "nehaopticals.com",
                "drsandhyaseyeclinic.setmore.com",
                "drsandhyaseyeclinic.com",
            ],
        )
        self.assertEqual(request_body["tools"][0]["parameters"]["max_uses"], 2)

    def test_simulated_outbound_message_is_recorded_without_query_telemetry(self) -> None:
        with connect(self.db_path) as connection:
            query_count_before = connection.execute(
                "SELECT COUNT(*) AS count FROM query_events"
            ).fetchone()["count"]
        message_id = record_simulated_outbound_message(
            "C001",
            "Hi Aarav, this is a simulated store message.",
            self.db_path,
        )
        with connect(self.db_path) as connection:
            message = connection.execute(
                "SELECT customer_id, order_id, direction, content FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            query_count = connection.execute(
                "SELECT COUNT(*) AS count FROM query_events"
            ).fetchone()["count"]
        self.assertEqual(message["customer_id"], "C001")
        self.assertEqual(message["order_id"], "NC-2401")
        self.assertEqual(message["direction"], "outbound")
        self.assertIn("simulated store message", message["content"])
        self.assertEqual(query_count, query_count_before)

    def test_clear_customer_chat_removes_messages_and_related_telemetry_only(self) -> None:
        process_customer_query(
            "C001",
            "Can I collect order NC-2401?",
            get_settings(self.db_path),
            db_path=self.db_path,
        )
        with connect(self.db_path) as connection:
            event_id = connection.execute(
                "SELECT id FROM query_events WHERE customer_id = 'C001' LIMIT 1"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO tool_calls
                    (query_event_id, endpoint, source, latency_ms, succeeded, created_at)
                VALUES (?, 'GET /test', 'rest_api', 10, 1, '2026-11-17T00:00:00+00:00')
                """,
                (event_id,),
            )
            messages_before = connection.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE customer_id = 'C002'"
            ).fetchone()["count"]
            orders_before = connection.execute(
                "SELECT COUNT(*) AS count FROM orders WHERE customer_id = 'C001'"
            ).fetchone()["count"]
            history_before = connection.execute(
                """
                SELECT COUNT(*) AS count FROM order_status_history
                WHERE order_id IN (SELECT id FROM orders WHERE customer_id = 'C001')
                """
            ).fetchone()["count"]

        deleted_count = clear_customer_chat("C001", self.db_path)

        with connect(self.db_path) as connection:
            remaining_messages = connection.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE customer_id = 'C001'"
            ).fetchone()["count"]
            remaining_events = connection.execute(
                "SELECT COUNT(*) AS count FROM query_events WHERE customer_id = 'C001'"
            ).fetchone()["count"]
            remaining_calls = connection.execute(
                """
                SELECT COUNT(*) AS count FROM tool_calls
                WHERE query_event_id IN (
                    SELECT id FROM query_events WHERE customer_id = 'C001'
                )
                """
            ).fetchone()["count"]
            messages_after = connection.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE customer_id = 'C002'"
            ).fetchone()["count"]
            orders_after = connection.execute(
                "SELECT COUNT(*) AS count FROM orders WHERE customer_id = 'C001'"
            ).fetchone()["count"]
            history_after = connection.execute(
                """
                SELECT COUNT(*) AS count FROM order_status_history
                WHERE order_id IN (SELECT id FROM orders WHERE customer_id = 'C001')
                """
            ).fetchone()["count"]

        self.assertGreater(deleted_count, 0)
        self.assertEqual(remaining_messages, 0)
        self.assertEqual(remaining_events, 0)
        self.assertEqual(remaining_calls, 0)
        self.assertEqual(messages_after, messages_before)
        self.assertEqual(orders_after, orders_before)
        self.assertEqual(history_after, history_before)

    def test_rest_service_failure_is_reported_and_logged_as_unanswered(self) -> None:
        with patch(
            "assistant.get_delivery_quote",
            side_effect=ApiServiceError("REST service unavailable", "/delivery-quote"),
        ):
            result = process_customer_query(
                "C002",
                "Can you deliver my glasses with Dunzo?",
                get_settings(self.db_path),
                db_path=self.db_path,
            )
        self.assertFalse(result.answered)
        self.assertIn("can’t reach the delivery quote service", result.message)
        with connect(self.db_path) as connection:
            event = connection.execute(
                "SELECT * FROM query_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            tool_call = connection.execute(
                "SELECT * FROM tool_calls ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event["source"], "rest_api")
        self.assertEqual(event["answered"], 0)
        self.assertEqual(tool_call["source"], "rest_api")
        self.assertEqual(tool_call["succeeded"], 0)


if __name__ == "__main__":
    unittest.main()
