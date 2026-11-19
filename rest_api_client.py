from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"


class ApiServiceError(RuntimeError):
    def __init__(self, message: str, endpoint: str) -> None:
        super().__init__(message)
        self.endpoint = endpoint


def get_api_base_url() -> str:
    return os.environ.get("NEHA_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")


def _get_json(path: str) -> Any:
    url = f"{get_api_base_url()}{path}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
            detail = payload.get("detail", str(error))
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = str(error)
        raise ApiServiceError(
            f"REST API returned HTTP {error.code}: {detail}",
            path,
        ) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ApiServiceError(
            f"Unable to reach the Neha Opticals REST API at {url}: {error}",
            path,
        ) from error


def get_frame_catalog() -> list[dict[str, Any]]:
    result = _get_json("/api/v1/catalog/frames")
    if not isinstance(result, list):
        raise ApiServiceError("REST API returned an invalid frame catalog.", "/api/v1/catalog/frames")
    return result


def get_store_info() -> dict[str, str]:
    result = _get_json("/api/v1/store")
    if not isinstance(result, dict):
        raise ApiServiceError("REST API returned invalid store information.", "/api/v1/store")
    return result


def get_delivery_quote(
    customer_id: str,
    order_id: str | None = None,
) -> dict[str, Any]:
    path = f"/api/v1/customers/{urllib.parse.quote(customer_id, safe='')}/delivery-quote"
    if order_id:
        path += f"?{urllib.parse.urlencode({'order_id': order_id})}"
    result = _get_json(path)
    if not isinstance(result, dict) or "order" not in result or "quote" not in result:
        raise ApiServiceError("REST API returned an invalid delivery quote.", path)
    return result
