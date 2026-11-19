from __future__ import annotations

import os
import sqlite3
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import streamlit as st

import env_config  # Load the shared local .env before modules read environment settings.
from assistant import (
    clear_customer_chat,
    get_customer_list,
    get_customer_messages,
    get_customer_orders,
    process_customer_query,
    record_tool_call,
    record_simulated_outbound_message,
    check_openrouter_connection,
    update_order_financials,
    update_order_status,
)
from database import DEFAULT_DB_PATH, connect, get_settings, initialize_database, save_settings
from rest_api_client import (
    ApiServiceError,
    get_api_base_url,
    get_delivery_quote,
    get_frame_catalog,
    get_store_info,
)


st.set_page_config(
    page_title="Neha Opticals | Customer care",
    page_icon=":material/visibility:",
    layout="wide",
)

DB_PATH = str(DEFAULT_DB_PATH)
initialize_database(DB_PATH)

if st.session_state.pop("reset_orders_editor", False):
    st.session_state.pop("orders_status_editor", None)

pending_customer_id = st.session_state.pop("pending_conversation_customer", None)
if pending_customer_id:
    customers_for_navigation = get_customer_list(DB_PATH)
    selected_customer = next(
        customer
        for customer in customers_for_navigation
        if customer["id"] == pending_customer_id
    )
    st.session_state["main_navigation"] = "Conversations"
    st.session_state["conversation_customer"] = (
        f"{selected_customer['full_name']} · {selected_customer['phone']}"
    )

NAV_ITEMS = ["Overview", "Conversations", "Orders", "REST API playground", "Settings"]
ORDER_STATUSES = [
    "Order received",
    "Lens processing",
    "Quality check",
    "Ready for Delivery",
    "Out for delivery",
    "Collected",
]
DATABASE_TABLES = {
    "orders": "Orders",
    "customers": "Customers",
    "order_status_history": "Order status history",
    "messages": "Messages",
    "query_events": "Query events",
    "tool_calls": "REST API calls",
    "frame_catalog": "Frame catalog",
    "faqs": "FAQs",
    "store_info": "Store information",
}


def get_openrouter_key() -> str | None:
    return st.session_state.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY")


def display_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d %b, %I:%M %p")
    except ValueError:
        return value


def _query_rows(query: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with connect(DB_PATH) as connection:
        return connection.execute(query, parameters).fetchall()


def _show_database_table(table_name: str) -> pd.DataFrame:
    if table_name not in DATABASE_TABLES:
        raise ValueError(f"Unknown database table: {table_name}")
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            f'SELECT * FROM "{table_name}" LIMIT 100'
        ).fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def render_overview() -> None:
    st.title("Operations overview", icon=":material/dashboard:")
    st.caption("Customer conversations, order readiness, response speed, and model spend in one place.")
    st.badge("Demo environment", icon=":material/visibility:", color="violet")

    now = datetime.now(timezone.utc)
    windows = [
        ("Last 2 hours", now - timedelta(hours=2)),
        ("Last 2 days", now - timedelta(days=2)),
        ("Last 1 week", now - timedelta(days=7)),
        ("Last 1 month", now - timedelta(days=30)),
    ]
    window_counts: list[int] = []
    for _, start in windows:
        row = _query_rows(
            """
            SELECT COUNT(*) AS count FROM query_events
            WHERE created_at >= ? AND answered = 1
            """,
            (start.isoformat(),),
        )[0]
        window_counts.append(int(row["count"]))

    total_row = _query_rows(
        """
        SELECT COALESCE(SUM(answered), 0) AS total,
               COALESCE(AVG(CASE WHEN answered = 1 THEN response_ms END), 0) AS avg_ms,
               COALESCE(SUM(cost_usd), 0) AS total_cost,
               COALESCE(SUM(CASE WHEN answered = 1 AND cost_is_estimate = 1 THEN 1 ELSE 0 END), 0) AS estimated_count
        FROM query_events
        """
    )[0]
    with st.container(horizontal=True):
        st.metric("Queries · 2 hours", f"{window_counts[0]:,}", border=True)
        st.metric("Queries · 2 days", f"{window_counts[1]:,}", border=True)
        st.metric("Queries · 1 week", f"{window_counts[2]:,}", border=True)
        st.metric("Queries · 1 month", f"{window_counts[3]:,}", border=True)

    with st.container(horizontal=True):
        st.metric("Total answered", f"{int(total_row['total']):,}", border=True)
        st.metric("Average response", f"{float(total_row['avg_ms']):,.0f} ms", border=True)
        st.metric("Tracked model cost", f"${float(total_row['total_cost']):.5f}", border=True)
        st.metric("Estimated costs", f"{int(total_row['estimated_count']):,}", border=True)

    chart_rows = _query_rows(
        """
        SELECT date(created_at) AS day, COUNT(*) AS queries,
               ROUND(AVG(response_ms), 1) AS avg_response_ms,
               ROUND(SUM(cost_usd), 6) AS cost_usd
        FROM query_events
        WHERE created_at >= ? AND answered = 1
        GROUP BY date(created_at)
        ORDER BY day
        """,
        ((now - timedelta(days=7)).isoformat(),),
    )
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.subheader("Query volume", icon=":material/chat:")
            chart_data = pd.DataFrame(
                [{"day": row["day"], "queries": row["queries"]} for row in chart_rows],
                columns=["day", "queries"],
            )
            st.bar_chart(chart_data, x="day", y="queries", alt="Queries answered per day over the last week")
    with right:
        with st.container(border=True):
            st.subheader("Daily performance", icon=":material/query_stats:")
            if chart_rows:
                detail_data = pd.DataFrame(
                    [
                        {
                            "day": row["day"],
                            "Average response (ms)": row["avg_response_ms"],
                            "Cost (USD)": row["cost_usd"],
                        }
                        for row in chart_rows
                    ]
                )
                st.dataframe(detail_data, hide_index=True, alt="Daily average response time and tracked cost")
            else:
                st.caption("No query history yet.")

    recent = _query_rows(
        """
        SELECT q.created_at, c.full_name, q.intent, q.source, q.answered, q.response_ms,
               q.cost_usd, q.cost_is_estimate, q.model
        FROM query_events q JOIN customers c ON c.id = q.customer_id
        ORDER BY q.created_at DESC LIMIT 8
        """
    )
    with st.container(border=True):
        st.subheader("Recent customer queries")
        recent_data = pd.DataFrame(
            [
                {
                    "Time": display_timestamp(row["created_at"]),
                    "Customer": row["full_name"],
                    "Intent": row["intent"],
                    "Data source": row["source"],
                    "Outcome": "Answered" if row["answered"] else "Failed",
                    "Response": f"{row['response_ms']:.0f} ms",
                    "Cost": f"${row['cost_usd']:.6f}" + (" (est.)" if row["cost_is_estimate"] else ""),
                    "Model": row["model"],
                }
                for row in recent
            ]
        )
        st.dataframe(recent_data, hide_index=True, alt="Recent customer queries and response metrics")


def render_conversations() -> None:
    st.title("Customer inbox", icon=":material/forum:")
    st.caption(
        "A WhatsApp-style support workspace. Replies use customer-scoped SQL, the "
        "REST service, and optional web search restricted to approved business sites."
    )
    customers = get_customer_list(DB_PATH)
    customer_by_label = {
        f"{customer['full_name']} · {customer['phone']}": customer
        for customer in customers
    }
    inbox, transcript = st.columns([0.85, 2.15], gap="large")
    with inbox:
        with st.container(border=True):
            st.subheader("Inbox")
            st.caption(f"{len(customers)} demo customers")
            selected_label = st.selectbox(
                "Choose a conversation",
                list(customer_by_label),
                key="conversation_customer",
            )
    customer = customer_by_label[selected_label]
    orders = [
        order
        for order in get_customer_orders(DB_PATH)
        if order["phone"] == customer["phone"]
    ]
    with inbox:
        with st.container(border=True):
            st.markdown("**Customer orders**")
            if orders:
                for order in orders:
                    st.markdown(f"**{order['id']}** · {order['frame_name']}")
                    status_color = (
                        "green"
                        if order["status"] in {"Ready for Delivery", "Collected"}
                        else "orange"
                    )
                    st.badge(order["status"], color=status_color)
            else:
                st.caption("No orders on file.")

    provider_error = st.session_state.pop("last_provider_error", None)
    retrieval_error = st.session_state.pop("last_retrieval_error", None)
    order_notification = st.session_state.pop("pending_order_notification", None)
    if order_notification:
        st.success(order_notification, icon=":material/mark_chat_read:")
    if provider_error:
        st.warning(
            f"{provider_error} The grounded demo response was sent instead.",
            title="Model provider unavailable",
        )
    if retrieval_error:
        st.warning(
            retrieval_error,
            title="REST data service unavailable",
        )

    with transcript:
        with st.container(border=True):
            st.subheader(customer["full_name"])
            st.caption(f"{customer['phone']} · WhatsApp simulation · Neha Opticals")
            st.space("small")
            messages = get_customer_messages(customer["id"], DB_PATH)
            if not messages:
                st.info("No messages yet. Send a message below to start this conversation.")
            for message in messages:
                is_customer = message["direction"] == "inbound"
                if is_customer:
                    _, bubble = st.columns([0.28, 0.72], gap="small")
                else:
                    bubble, _ = st.columns([0.72, 0.28], gap="small")
                with bubble:
                    with st.container(border=True):
                        speaker = "Customer" if is_customer else "Neha Opticals"
                        st.caption(f"{speaker} · {display_timestamp(message['created_at'])}")
                        st.markdown(message["content"])
                        if not is_customer and message["response_ms"] is not None:
                            estimate = " · estimated" if message["cost_is_estimate"] else ""
                            st.caption(
                                f"{message['model']} · {message['response_ms']:.0f} ms · "
                                f"${message['cost_usd'] or 0:.6f}{estimate}"
                            )

        latest_order_id = orders[0]["id"] if orders else None
        default_outbound = f"Hi {customer['full_name'].split()[0]}, this is Neha Opticals."
        if latest_order_id:
            default_outbound += f" We’re checking in about your order {latest_order_id}."
        default_outbound += " Reply here if you have any questions."
        with st.expander("Start or send a simulated business message", expanded=not messages):
            st.caption(
                "Records a local demo message in this chat only. It does not send anything "
                "to WhatsApp or the phone number."
            )
            with st.form("simulated_outbound_message"):
                outbound_message = st.text_area(
                    "Message to customer",
                    value=default_outbound,
                    height=100,
                )
                send_outbound = st.form_submit_button(
                    "Record simulated WhatsApp message",
                    type="primary",
                    icon=":material/send:",
                )
            if send_outbound:
                record_simulated_outbound_message(
                    customer["id"],
                    outbound_message,
                    DB_PATH,
                )
                st.session_state["simulated_outbound_notice"] = (
                    "Simulated business message recorded in the conversation. "
                    "No WhatsApp message was sent."
                )
                st.rerun()

        with st.expander("Chat history actions"):
            st.caption(
                "Clearing removes this customer's messages and chat-linked query/API "
                "telemetry. Orders and order-status history are kept."
            )
            if st.button(
                "Clear this chat",
                key=f"clear_chat_{customer['id']}",
                icon=":material/delete:",
            ):
                st.session_state["confirm_clear_chat_customer"] = customer["id"]
                st.rerun()

            if st.session_state.get("confirm_clear_chat_customer") == customer["id"]:
                st.warning(
                    f"Clear all messages for {customer['full_name']}? This cannot be undone."
                )
                confirm_column, cancel_column = st.columns(2)
                if confirm_column.button(
                    "Confirm clear",
                    type="primary",
                    key=f"confirm_clear_chat_{customer['id']}",
                ):
                    deleted_count = clear_customer_chat(customer["id"], DB_PATH)
                    st.session_state.pop("confirm_clear_chat_customer", None)
                    st.session_state["chat_cleared_notice"] = (
                        f"Cleared {deleted_count} messages for {customer['full_name']}. "
                        "Their orders and order history were kept."
                    )
                    st.rerun()
                if cancel_column.button(
                    "Cancel",
                    key=f"cancel_clear_chat_{customer['id']}",
                ):
                    st.session_state.pop("confirm_clear_chat_customer", None)
                    st.rerun()

        cleared_notice = st.session_state.pop("chat_cleared_notice", None)
        if cleared_notice:
            st.success(cleared_notice)

        outbound_notice = st.session_state.pop("simulated_outbound_notice", None)
        if outbound_notice:
            st.success(outbound_notice)

        if prompt := st.chat_input(
            "Message Neha Opticals",
            key="conversation_chat_input",
            submit_mode="disable",
        ):
            result = process_customer_query(
                customer["id"],
                prompt,
                get_settings(DB_PATH),
                get_openrouter_key(),
                DB_PATH,
            )
            st.session_state["last_provider_error"] = result.provider_error
            st.session_state["last_retrieval_error"] = result.retrieval_error
            st.rerun()


def render_orders() -> None:
    st.title("Orders and readiness", icon=":material/inventory_2:")
    st.caption("Edit the live orders table. Saving a transition to Ready for Delivery records and opens the customer's simulated WhatsApp chat.")
    update_summary = st.session_state.pop("order_update_summary", None)
    if update_summary:
        st.success(update_summary)

    orders = get_customer_orders(DB_PATH)
    order_data = pd.DataFrame(
        [
            {
                "Order ID": order["id"],
                "Customer": order["customer"],
                "Phone": order["phone"],
                "Frame": order["frame_name"],
                "Prescription": order["prescription_type"],
                "Order date": order["order_date"],
                "Promised date": order["promised_date"],
                "Total amount (₹)": order["total_amount_inr"],
                "Balance pending (₹)": order["balance_due_inr"],
                "Status": order["status"],
                "Customer ID": order["customer_id"],
            }
            for order in orders
        ]
    )
    with st.container(border=True):
        st.subheader("Orders database", icon=":material/table_view:")
        st.caption("Live SQLite rows · edit amounts or status, then apply your updates.")
        editor_data = order_data.drop(columns=["Customer ID"])
        edited_orders = st.data_editor(
            editor_data,
            key="orders_status_editor",
            hide_index=True,
            num_rows="fixed",
            height=310,
            disabled=[
                "Order ID",
                "Customer",
                "Phone",
                "Frame",
                "Prescription",
                "Order date",
                "Promised date",
            ],
            column_config={
                "Order ID": st.column_config.TextColumn("Order ID", pinned=True),
                "Total amount (₹)": st.column_config.NumberColumn(
                    "Total amount (₹)",
                    min_value=0,
                    step=1,
                    format="₹%.0f",
                    required=False,
                ),
                "Balance pending (₹)": st.column_config.NumberColumn(
                    "Balance pending (₹)",
                    min_value=0,
                    step=1,
                    format="₹%.0f",
                    required=False,
                    help="Cannot exceed the total amount.",
                ),
                "Status": st.column_config.SelectboxColumn(
                    "Status",
                    options=ORDER_STATUSES,
                    required=True,
                    help="Selecting Ready for Delivery sends a simulated WhatsApp message.",
                ),
            },
            alt="Editable Neha Opticals orders database table",
        )
        action, download = st.columns([1, 1], vertical_alignment="center")
        with action:
            apply_changes = st.button(
                "Apply order updates",
                type="primary",
                icon=":material/save:",
                key="apply_order_status_updates",
            )
        with download:
            st.download_button(
                "Download orders as CSV",
                order_data.drop(columns=["Customer ID"]).to_csv(index=False),
                file_name="neha_opticals_demo_orders.csv",
                mime="text/csv",
                icon=":material/download:",
            )

        if apply_changes:
            original_by_id = {
                order["id"]: order
                for order in orders
            }
            planned_updates: list[tuple[str, str, int, int, dict[str, object]]] = []
            try:
                for _, edited_order in edited_orders.iterrows():
                    order_id = str(edited_order["Order ID"])
                    total_value = edited_order["Total amount (₹)"]
                    balance_value = edited_order["Balance pending (₹)"]
                    total_missing = pd.isna(total_value)
                    balance_missing = pd.isna(balance_value)
                    if total_missing != balance_missing:
                        raise ValueError(
                            "Enter both order amounts, or clear both to mark them unknown."
                        )
                    if total_missing:
                        total_amount = None
                        balance_due = None
                    else:
                        total_amount = int(total_value)
                        balance_due = int(balance_value)
                        if total_amount != total_value or balance_due != balance_value:
                            raise ValueError("Order amounts must be whole numbers of rupees.")
                        if total_amount < 0 or balance_due < 0:
                            raise ValueError("Order amounts cannot be negative.")
                        if balance_due > total_amount:
                            raise ValueError(
                                f"Balance pending for {order_id} cannot exceed its total amount."
                            )
                    planned_updates.append(
                        (
                            order_id,
                            str(edited_order["Status"]),
                            total_amount,
                            balance_due,
                            original_by_id[order_id],
                        )
                    )
            except (TypeError, ValueError, OverflowError) as error:
                st.error(f"Could not save order changes: {error}")
                return

            notifications: list[tuple[str, str]] = []
            updated_count = 0
            for order_id, new_status, total_amount, balance_due, original_order in planned_updates:
                status_changed = new_status != original_order["status"]
                financials_changed = (
                    total_amount != original_order["total_amount_inr"]
                    or balance_due != original_order["balance_due_inr"]
                )
                if not status_changed and not financials_changed:
                    continue
                notification = (
                    update_order_status(order_id, new_status, DB_PATH)
                    if status_changed
                    else None
                )
                if financials_changed:
                    update_order_financials(
                        order_id,
                        total_amount,
                        balance_due,
                        DB_PATH,
                    )
                updated_count += 1
                if notification:
                    notifications.append((original_order["customer_id"], notification))

            if updated_count == 0:
                st.info("No order changes to save.")
            else:
                notification_count = len(notifications)
                st.session_state["order_update_summary"] = (
                    f"Saved {updated_count} order "
                    f"{'update' if updated_count == 1 else 'updates'}."
                    + (
                        f" {notification_count} simulated WhatsApp "
                        f"{'message was' if notification_count == 1 else 'messages were'} recorded."
                        if notification_count
                        else ""
                    )
                )
                if notifications:
                    customer_id, _ = notifications[0]
                    customers = get_customer_list(DB_PATH)
                    customer = next(
                        customer for customer in customers if customer["id"] == customer_id
                    )
                    st.session_state["pending_conversation_customer"] = customer_id
                    st.session_state["pending_order_notification"] = (
                        st.session_state["order_update_summary"]
                        + f" Opened {customer['full_name']}'s conversation to show the recorded message."
                    )
                else:
                    st.session_state["reset_orders_editor"] = True
                st.rerun()

    history = _query_rows(
        """
        SELECT h.changed_at, h.order_id, c.full_name, h.old_status, h.new_status, h.source
        FROM order_status_history h
        JOIN orders o ON o.id = h.order_id
        JOIN customers c ON c.id = o.customer_id
        ORDER BY h.changed_at DESC LIMIT 12
        """
    )
    with st.container(border=True):
        st.subheader("Recent status history")
        history_data = pd.DataFrame(
            [
                {
                    "Time": display_timestamp(row["changed_at"]),
                    "Order": row["order_id"],
                    "Customer": row["full_name"],
                    "From": row["old_status"] or "—",
                    "To": row["new_status"],
                    "Source": row["source"],
                }
                for row in history
            ]
        )
        st.dataframe(history_data, hide_index=True, alt="Order status change audit history")

    with st.expander("Explore database tables", icon=":material/database:"):
        table_name = st.selectbox(
            "Database table",
            list(DATABASE_TABLES),
            format_func=lambda value: DATABASE_TABLES[value],
            key="database_table_choice",
        )
        table_data = _show_database_table(table_name)
        st.caption(f"{len(table_data)} rows displayed · maximum 100")
        st.dataframe(
            table_data,
            hide_index=True,
            alt=f"Rows from the {table_name} database table",
        )

def render_api_playground() -> None:
    st.title("REST API playground", icon=":material/api:")
    st.caption(
        "Call the separate Neha Opticals REST service. It queries the demo SQLite database "
        "and returns JSON; the assistant uses this same HTTP client."
    )
    st.caption(f"Service base URL: `{get_api_base_url()}`")
    st.caption("Start the API service in a separate terminal; see README.md for the command.")
    orders = get_customer_orders(DB_PATH)
    endpoint = st.selectbox(
        "Choose a REST endpoint",
        [
            "GET /api/v1/catalog/frames",
            "GET /api/v1/store",
            "GET /api/v1/customers/{customer_id}/delivery-quote",
        ],
        key="api_endpoint",
    )
    selected_order: dict[str, Any] | None = None
    if endpoint.endswith("/delivery-quote"):
        order_by_label = {
            f"{order['id']} · {order['customer']}": order
            for order in orders
        }
        order_label = st.selectbox(
            "Customer order",
            list(order_by_label),
            key="api_order_id",
        )
        selected_order = order_by_label[order_label]

    if st.button("Send HTTP request", type="primary", icon=":material/play_arrow:"):
        started = datetime.now(timezone.utc)
        start_time = time.perf_counter()
        try:
            if endpoint.endswith("/catalog/frames"):
                response: Any = get_frame_catalog()
            elif endpoint.endswith("/store"):
                response = get_store_info()
            else:
                if selected_order is None:
                    raise ValueError("Select a customer order to request a delivery quote.")
                response = get_delivery_quote(
                    selected_order["customer_id"],
                    selected_order["id"],
                )
            latency = (time.perf_counter() - start_time) * 1000
            record_tool_call(endpoint, "rest_api", latency, True, DB_PATH)
            st.success(f"200 OK · {latency:.2f} ms")
            st.json(response)
        except (ApiServiceError, ValueError) as error:
            latency = (time.perf_counter() - start_time) * 1000
            record_tool_call(endpoint, "rest_api", latency, False, DB_PATH)
            st.error(f"REST request failed: {error}")
        st.caption(f"Called at {started.astimezone().strftime('%d %b %Y, %I:%M:%S %p')}")

    with st.container(border=True):
        st.subheader("Recent API tool calls")
        rows = _query_rows(
            """
            SELECT endpoint, source, latency_ms, succeeded, created_at
            FROM tool_calls ORDER BY created_at DESC LIMIT 10
            """
        )
        if rows:
            api_data = pd.DataFrame(
                [
                    {
                        "Endpoint": row["endpoint"],
                        "Source": row["source"],
                        "Latency": f"{row['latency_ms']:.2f} ms",
                        "Result": "200 OK" if row["succeeded"] else "Error",
                        "Time": display_timestamp(row["created_at"]),
                    }
                    for row in rows
                ]
            )
            st.dataframe(api_data, hide_index=True, alt="Recent REST API calls and latency")
        else:
            st.caption("No API calls yet.")


def render_settings() -> None:
    st.title("Assistant settings", icon=":material/settings:")
    settings = get_settings(DB_PATH)
    session_key = st.session_state.get("openrouter_api_key")
    environment_key = os.environ.get("OPENROUTER_API_KEY")
    api_key_configured = bool(get_openrouter_key())

    st.subheader("OpenRouter API key")
    if session_key:
        st.success("A session-only key is configured and overrides the environment key.")
    elif environment_key:
        st.success("An OPENROUTER_API_KEY environment variable is configured.")
    else:
        st.info("No OpenRouter key configured. AI replies are unavailable until you add one.")
    with st.form("openrouter_key_form", clear_on_submit=True):
        key_input = st.text_input(
            "API key",
            type="password",
            placeholder="Paste your OpenRouter key",
            help="Kept in this Streamlit session's memory only; never saved to SQLite.",
        )
        save_key = st.form_submit_button("Set or update key", type="primary")
    if save_key:
        if key_input.strip():
            st.session_state["openrouter_api_key"] = key_input.strip()
            st.rerun()
        else:
            st.error("Enter an OpenRouter API key before saving.")

    if session_key:
        if st.button("Clear session key", key="clear_openrouter_session_key"):
            del st.session_state["openrouter_api_key"]
            st.rerun()

    st.caption(
        "For persistent configuration across app restarts, set OPENROUTER_API_KEY in the "
        "process environment. Do not paste API keys into chat or commit them."
    )

    models = [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
        "google/gemini-2.0-flash-001",
    ]
    if settings["model"] not in models:
        models.append(settings["model"])
    with st.form("assistant_settings"):
        selected_model = st.selectbox(
            "OpenRouter model",
            models,
            index=models.index(settings["model"]),
        )
        use_llm = st.toggle(
            "Use OpenRouter for response phrasing",
            value=settings["use_llm"],
            disabled=not api_key_configured,
        )
        use_web_search = st.toggle(
            "Allow web search for public business questions",
            value=settings.get("use_web_search", True),
            disabled=not api_key_configured,
            help=(
                "The model can decide when to search. Results are restricted to "
                "nehaopticals.com, drsandhyaseyeclinic.setmore.com, and "
                "drsandhyaseyeclinic.com."
            ),
        )
        st.caption(
            "Order records remain database-grounded. Web search is restricted to "
            "approved domains and sources should be linked in the reply."
        )
        price_left, price_right = st.columns(2)
        input_price = price_left.number_input(
            "Input price (USD per 1M tokens)",
            min_value=0.0,
            value=float(settings["input_price_per_million"]),
            step=0.01,
            format="%.4f",
        )
        output_price = price_right.number_input(
            "Output price (USD per 1M tokens)",
            min_value=0.0,
            value=float(settings["output_price_per_million"]),
            step=0.01,
            format="%.4f",
        )
        st.caption(
            "Default GPT-4o mini rates are a configurable demo benchmark (USD $0.15 input / "
            "$0.60 output per million tokens). Verify the current OpenRouter rate before production."
        )
        st.caption(f"Data REST API: `{get_api_base_url()}`")
        if st.form_submit_button("Save settings", type="primary"):
            save_settings(
                {
                    "model": selected_model,
                    "use_llm": use_llm,
                    "use_web_search": use_web_search,
                    "input_price_per_million": input_price,
                    "output_price_per_million": output_price,
                },
                DB_PATH,
            )
            st.success("Assistant settings saved.")

    if settings["use_llm"] and not api_key_configured:
        st.warning("AI response phrasing is enabled, but no key is available. Replies will use the deterministic fallback.")

    st.subheader("Test the model")
    st.caption(
        "Sends a short connection-check prompt to OpenRouter without customer data. "
        "The provider may charge a small amount for this request."
    )
    if st.button(
        "Test OpenRouter connection",
        disabled=not api_key_configured,
        key="test_openrouter_connection",
    ):
        with st.spinner("Checking the OpenRouter connection..."):
            try:
                test_reply, input_tokens, output_tokens = check_openrouter_connection(
                    get_openrouter_key() or "",
                    settings["model"],
                )
            except urllib.error.HTTPError as error:
                st.error(
                    f"OpenRouter returned HTTP {error.code} ({error.reason}). "
                    "Check the key, model access, and account credits."
                )
            except urllib.error.URLError as error:
                st.error(f"Could not reach OpenRouter: {error.reason}")
            except (TimeoutError, ValueError, KeyError) as error:
                st.error(f"OpenRouter connection test failed: {error}")
            else:
                st.success(f"Connection successful using {settings['model']}.")
                st.write(test_reply)
                st.caption(
                    f"Test usage: {input_tokens} input · {output_tokens} output tokens. "
                    "This test is not logged as a customer query."
                )

    with st.container(border=True):
        st.subheader("Data and privacy")
        st.markdown(
            "- Order lookups are parameterized and scoped to the selected demo customer.\n"
            "- Frame catalog and delivery quotes are fetched over HTTP from the separate REST service; the API playground can inspect stored store/contact data.\n"
            "- The model receives only the current question and retrieved context, not database access. Optional web search is limited to the approved business domains.\n"
            "- Order, delivery, price, and policy records remain grounded in the local database/API; current public business details can be checked against approved websites.\n"
            "- WhatsApp messages and status notifications are simulated; no message leaves this app.\n"
            "- Session-entered API keys stay in the current Streamlit session and are not written to the database."
        )


with st.sidebar:
    st.title("Neha Opticals", anchor=False)
    st.caption("CUSTOMER EXPERIENCE")
    st.space("small")
    st.caption("WORKSPACE")
    page = st.radio(
        "Workspace",
        NAV_ITEMS,
        index=0,
        label_visibility="collapsed",
        key="main_navigation",
    )
    st.space("large")
    st.badge("Demo environment", icon=":material/science:", color="violet")
    st.caption("WhatsApp channel · simulated")

if page == "Overview":
    render_overview()
elif page == "Conversations":
    render_conversations()
elif page == "Orders":
    render_orders()
elif page == "REST API playground":
    render_api_playground()
elif page == "Settings":
    render_settings()
