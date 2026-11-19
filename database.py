from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "OPTICALS_DB_PATH",
        str(ROOT_DIR / "data" / "neha_opticals.sqlite3"),
    )
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "model": "openai/gpt-4o-mini",
    "use_llm": False,
    "use_web_search": True,
    "input_price_per_million": 0.15,
    "output_price_per_million": 0.60,
}

DEFAULT_STORE_ADDRESS = (
    "638, 1st Floor (Above MedPlus), 18 Main, 7th Phase, JP Nagar, "
    "Bengaluru, Karnataka 560078"
)

SEEDED_ANSWER_BY_QUERY = {
    "NC-2401 ka status kya hai?": "Order NC-2401 is ready for collection at Neha Opticals. We can also arrange Dunzo delivery for an extra fee.",
    "Are blue-cut lenses ready yet?": "Your blue-cut glasses for order NC-2402 are in lens processing. The current estimate is 2 days.",
    "How much is Dunzo delivery?": "Order NC-2403 is still in quality check, so it is not ready to dispatch yet. We can check delivery once it is ready.",
    "What frame materials do you have?": "Our demo catalog includes Everyday acetate from ₹2,499 and Flex titanium from ₹4,999.",
    "What are your store hours?": "I can check current published hours on the approved business websites when web search is enabled.",
    "What is the warranty on frames?": "Frames include a 6-month manufacturing-defect warranty. Accidental damage and prescription changes are not covered.",
    "Can I get a frame adjustment?": "You can visit us for a complimentary frame fit adjustment. Please bring your glasses.",
    "How do I care for my lenses?": "Rinse lenses before wiping, use a clean microfiber cloth, and keep your glasses in their case.",
    "Can you check NC-2404?": "Order NC-2404 has been received. The current estimated date is shown in your order details.",
    "Show me light titanium frames": "Flex titanium is a lightweight option in our demo catalog, priced at ₹4,999.",
    "Where is the store?": f"Neha Opticals is at {DEFAULT_STORE_ADDRESS}.",
    "Do you deliver glasses?": "Dunzo delivery for order NC-2401 is available for ₹79 in this demo, with an estimated 2–4 hours after dispatch.",
    "How can I exchange my glasses?": "Please message us with your order ID so the store team can review the applicable exchange options.",
    "When can I collect my order?": "Order NC-2402 is in lens processing. The current estimated date is shown in your order details.",
    "Tell me about your hours": "I can check current published hours on the approved business websites when web search is enabled.",
    "Is order NC-2401 ready?": "Order NC-2401 is ready for collection. Ask us if you would like a Dunzo delivery quote.",
    "What are your frame prices?": "Our demo frames range from ₹1,799 for Junior comfort to ₹4,999 for Flex titanium.",
}


@contextmanager
def connect(db_path: str | Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customers(id),
                frame_name TEXT NOT NULL,
                prescription_type TEXT NOT NULL,
                order_date TEXT NOT NULL,
                promised_date TEXT NOT NULL,
                total_amount_inr INTEGER
                    CHECK (total_amount_inr IS NULL OR total_amount_inr >= 0),
                balance_due_inr INTEGER
                    CHECK (
                        balance_due_inr IS NULL
                        OR (
                            balance_due_inr >= 0
                            AND total_amount_inr IS NOT NULL
                            AND balance_due_inr <= total_amount_inr
                        )
                    ),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'Order received',
                        'Lens processing',
                        'Quality check',
                        'Ready for Delivery',
                        'Out for delivery',
                        'Collected'
                    )
                ),
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_customer
                ON orders(customer_id, order_date DESC);
            CREATE INDEX IF NOT EXISTS idx_orders_status
                ON orders(status);

            CREATE TABLE IF NOT EXISTS order_status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL REFERENCES orders(id),
                old_status TEXT,
                new_status TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT NOT NULL REFERENCES customers(id),
                order_id TEXT REFERENCES orders(id),
                direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                channel TEXT NOT NULL DEFAULT 'whatsapp',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                response_ms REAL,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                cost_is_estimate INTEGER NOT NULL DEFAULT 0,
                delivery_status TEXT NOT NULL DEFAULT 'delivered'
            );

            CREATE INDEX IF NOT EXISTS idx_messages_customer_time
                ON messages(customer_id, created_at);

            CREATE TABLE IF NOT EXISTS query_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inbound_message_id INTEGER NOT NULL REFERENCES messages(id),
                customer_id TEXT NOT NULL REFERENCES customers(id),
                intent TEXT NOT NULL,
                source TEXT NOT NULL,
                answered INTEGER NOT NULL,
                response_ms REAL NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                cost_is_estimate INTEGER NOT NULL,
                provider_error TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_query_events_created
                ON query_events(created_at);

            CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_event_id INTEGER REFERENCES query_events(id),
                endpoint TEXT NOT NULL,
                source TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                succeeded INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS frame_catalog (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                material TEXT NOT NULL,
                price_inr INTEGER NOT NULL,
                description TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS store_info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS faqs (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                answer TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        _ensure_order_financial_columns(connection)
        _seed_if_empty(connection)
        _migrate_demo_order_financials(connection)
        _refresh_seeded_responses(connection)
        _migrate_seeded_store_info(connection)


def _seed_if_empty(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT 1 FROM customers LIMIT 1").fetchone():
        return

    now = datetime.now(timezone.utc)
    customers = [
        ("C001", "Aarav Mehta", "+91 98765 43210"),
        ("C002", "Priya Shah", "+91 98201 23456"),
        ("C003", "Kabir Rao", "+91 98190 76543"),
        ("C004", "Ananya Iyer", "+91 98920 11223"),
    ]
    connection.executemany(
        "INSERT INTO customers (id, full_name, phone, created_at) VALUES (?, ?, ?, ?)",
        [(customer_id, name, phone, now.isoformat()) for customer_id, name, phone in customers],
    )

    order_specs = [
        ("NC-2401", "C001", "Ray-Ban RX5228", "Single vision", 8, 1, "Ready for Delivery", 12499, 2500),
        ("NC-2402", "C002", "Lenskart Air Flex", "Blue-cut single vision", 5, 2, "Lens processing", 8999, 2000),
        ("NC-2403", "C003", "Titan Eye+ TE1092", "Progressive", 11, 3, "Quality check", 16999, 5000),
        ("NC-2404", "C004", "Vogue VO5361", "Single vision", 2, 4, "Order received", 7499, 7499),
        ("NC-2405", "C001", "John Jacobs JJ E134", "Computer lens", 18, 8, "Collected", 9999, 0),
    ]
    for (
        order_id, customer_id, frame, prescription, age, due_in, status,
        total_amount, balance_due,
    ) in order_specs:
        order_date = (now - timedelta(days=age)).date().isoformat()
        promised_date = (now + timedelta(days=due_in)).date().isoformat()
        changed_at = (now - timedelta(days=max(0, age - 1))).isoformat()
        connection.execute(
            """
            INSERT INTO orders
                (id, customer_id, frame_name, prescription_type, order_date,
                 promised_date, total_amount_inr, balance_due_inr, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                customer_id,
                frame,
                prescription,
                order_date,
                promised_date,
                total_amount,
                balance_due,
                status,
                changed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO order_status_history
                (order_id, old_status, new_status, changed_at, source, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (order_id, None, status, changed_at, "demo seed", "Initial demo status"),
        )

    connection.executemany(
        """
        INSERT INTO frame_catalog (id, name, material, price_inr, description)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("FR-101", "Everyday acetate", "Lightweight acetate", 2499, "Classic styles for daily wear"),
            ("FR-102", "Flex titanium", "Titanium alloy", 4999, "Flexible, lightweight premium frame"),
            ("FR-103", "Urban metal", "Stainless steel", 3299, "Slim metal frame in multiple finishes"),
            ("FR-104", "Junior comfort", "Flexible TR90", 1799, "Durable frame designed for children"),
        ],
    )
    connection.executemany(
        "INSERT INTO store_info (key, value) VALUES (?, ?)",
        [
            ("name", "Neha Opticals"),
            ("address", DEFAULT_STORE_ADDRESS),
            ("phone", "+91 98443 08150"),
            ("dunzo_note", "Dunzo delivery is available in supported areas for a small extra fee."),
        ],
    )
    connection.executemany(
        "INSERT INTO faqs (id, topic, answer) VALUES (?, ?, ?)",
        [
            (
                "warranty",
                "warranty",
                "Frames include a 6-month manufacturing-defect warranty. Accidental damage and prescription changes are not covered.",
            ),
            (
                "adjustment",
                "adjustment",
                "You can visit the store for a complimentary frame fit adjustment. Please bring your glasses with you.",
            ),
            (
                "care",
                "care",
                "Rinse lenses before wiping, use a clean microfiber cloth, and keep your glasses in their case when not in use.",
            ),
            (
                "returns",
                "returns",
                "If there is a concern with your order, message us with your order ID so our store team can review the applicable exchange options.",
            ),
        ],
    )
    connection.executemany(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)",
        [(key, str(value).lower() if isinstance(value, bool) else str(value)) for key, value in DEFAULT_SETTINGS.items()],
    )

    # Seed recent demo conversations so the dashboard has meaningful time windows.
    sample_queries = [
        ("C001", "NC-2401 ka status kya hai?", "order status", "database", 184),
        ("C002", "Are blue-cut lenses ready yet?", "order status", "database", 211),
        ("C003", "How much is Dunzo delivery?", "delivery quote", "rest_api", 312),
        ("C001", "What frame materials do you have?", "frame catalog", "rest_api", 279),
        ("C004", "What are your store hours?", "store information", "rest_api", 163),
        ("C002", "What is the warranty on frames?", "warranty", "knowledge base", 145),
        ("C003", "Can I get a frame adjustment?", "adjustment", "knowledge base", 153),
        ("C001", "How do I care for my lenses?", "lens care", "knowledge base", 167),
        ("C004", "Can you check NC-2404?", "order status", "database", 195),
        ("C002", "Show me light titanium frames", "frame catalog", "rest_api", 298),
        ("C003", "Where is the store?", "store information", "rest_api", 142),
        ("C001", "Do you deliver glasses?", "delivery quote", "rest_api", 310),
        ("C004", "How can I exchange my glasses?", "returns", "knowledge base", 171),
        ("C002", "When can I collect my order?", "order status", "database", 201),
        ("C003", "Tell me about your hours", "store information", "rest_api", 149),
        ("C001", "Is order NC-2401 ready?", "order status", "database", 189),
        ("C004", "What are your frame prices?", "frame catalog", "rest_api", 282),
    ]
    offsets_hours = [1, 1, 2, 4, 8, 12, 21, 30, 42, 55, 75, 96, 130, 170, 240, 360, 600]
    for index, (customer_id, question, intent, source, response_ms) in enumerate(sample_queries):
        created_at = (now - timedelta(hours=offsets_hours[index])).isoformat()
        inbound_cursor = connection.execute(
            """
            INSERT INTO messages (customer_id, direction, content, created_at)
            VALUES (?, 'inbound', ?, ?)
            """,
            (customer_id, question, created_at),
        )
        answer = SEEDED_ANSWER_BY_QUERY[question]
        output_at = (now - timedelta(hours=max(0, offsets_hours[index] - 0.001))).isoformat()
        connection.execute(
            """
            INSERT INTO messages (customer_id, direction, content, created_at, response_ms, model,
                                  input_tokens, output_tokens, cost_usd, cost_is_estimate)
            VALUES (?, 'outbound', ?, ?, ?, 'rule-based demo', 28, 24, 0.000018, 1)
            """,
            (customer_id, answer, output_at, response_ms),
        )
        connection.execute(
            """
            INSERT INTO query_events
                (inbound_message_id, customer_id, intent, source, answered, response_ms,
                 model, input_tokens, output_tokens, cost_usd, cost_is_estimate, created_at)
            VALUES (?, ?, ?, ?, 1, ?, 'rule-based demo', 28, 24, 0.000018, 1, ?)
            """,
            (inbound_cursor.lastrowid, customer_id, intent, source, response_ms, created_at),
        )


def _refresh_seeded_responses(connection: sqlite3.Connection) -> None:
    placeholder = "This is a seeded demo response. Start a new conversation to see a live demo reply."
    old_replies = connection.execute(
        """
        SELECT id, customer_id
        FROM messages
        WHERE direction = 'outbound' AND content = ?
        """,
        (placeholder,),
    ).fetchall()
    for reply in old_replies:
        question = connection.execute(
            """
            SELECT content FROM messages
            WHERE customer_id = ? AND direction = 'inbound' AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (reply["customer_id"], reply["id"]),
        ).fetchone()
        if question and question["content"] in SEEDED_ANSWER_BY_QUERY:
            connection.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (SEEDED_ANSWER_BY_QUERY[question["content"]], reply["id"]),
            )


def _migrate_seeded_store_info(connection: sqlite3.Connection) -> None:
    seeded_values = (
        "Monday to Saturday, 10:00 AM to 8:00 PM",
        "Monday to Saturday, 10:00 AM–1:00 PM and 5:30 PM–8:00 PM",
        "Sunday, 11:00 AM to 5:00 PM",
        "10:00 AM–1:00 PM and 5:30 PM–8:00 PM",
        "Not listed on the provided booking page",
        "https://drsandhyaseyeclinic.setmore.com/",
    )
    connection.execute(
        "DELETE FROM store_info WHERE key LIKE 'hours_%' AND value IN (?, ?, ?, ?, ?, ?)",
        seeded_values,
    )
    connection.execute(
        """
        UPDATE store_info SET value = ?
        WHERE key = 'address'
          AND value = 'Shop 12, Lake View Road, Powai, Mumbai 400076'
        """,
        (DEFAULT_STORE_ADDRESS,),
    )


def _ensure_order_financial_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(orders)").fetchall()
    }
    if "total_amount_inr" not in columns:
        connection.execute(
            """
            ALTER TABLE orders
            ADD COLUMN total_amount_inr INTEGER
                CHECK (total_amount_inr IS NULL OR total_amount_inr >= 0)
            """
        )
    if "balance_due_inr" not in columns:
        connection.execute(
            """
            ALTER TABLE orders
            ADD COLUMN balance_due_inr INTEGER
                CHECK (
                    balance_due_inr IS NULL
                    OR (
                        balance_due_inr >= 0
                        AND total_amount_inr IS NOT NULL
                        AND balance_due_inr <= total_amount_inr
                    )
                )
            """
        )


def _migrate_demo_order_financials(connection: sqlite3.Connection) -> None:
    demo_amounts = (
        ("NC-2401", 12499, 2500),
        ("NC-2402", 8999, 2000),
        ("NC-2403", 16999, 5000),
        ("NC-2404", 7499, 7499),
        ("NC-2405", 9999, 0),
    )
    connection.executemany(
        """
        UPDATE orders
        SET total_amount_inr = ?, balance_due_inr = ?
        WHERE id = ? AND total_amount_inr IS NULL AND balance_due_inr IS NULL
        """,
        [(total, balance, order_id) for order_id, total, balance in demo_amounts],
    )


def get_settings(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    with connect(db_path) as connection:
        rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    settings: dict[str, Any] = {
        "model": values.get("model", DEFAULT_SETTINGS["model"]),
        "use_llm": values.get("use_llm", "false").lower() == "true",
        "use_web_search": values.get(
            "use_web_search",
            str(DEFAULT_SETTINGS["use_web_search"]).lower(),
        ).lower() == "true",
        "input_price_per_million": float(
            values.get("input_price_per_million", DEFAULT_SETTINGS["input_price_per_million"])
        ),
        "output_price_per_million": float(
            values.get("output_price_per_million", DEFAULT_SETTINGS["output_price_per_million"])
        ),
    }
    return settings


def save_settings(settings: dict[str, Any], db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            [
                (key, str(value).lower() if isinstance(value, bool) else str(value))
                for key, value in settings.items()
            ],
        )
