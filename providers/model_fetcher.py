"""
Model fetcher — test API connectivity and discover available models.

Supports both Anthropic-format and OpenAI-format endpoints.
"""

import json
import ssl
import urllib.error
import urllib.request
from typing import Optional


def fetch_models(
    base_url: str,
    api_key: str,
    api_format: str,
    timeout: int = 10,
) -> Optional[list[str]]:
    """Fetch available model IDs from the provider's /v1/models endpoint.

    Returns a list of model ID strings, or None if the request fails entirely.
    On auth errors (401) raises an exception; on other recoverable errors
    returns None so the caller can fall back to known_models.
    """
    url = base_url.rstrip("/") + "/v1/models"

    req = urllib.request.Request(url, method="GET")

    if api_format == "anthropic":
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
    else:
        req.add_header("Authorization", f"Bearer {api_key}")

    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "opencode-proxy-wizard/1.0")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AuthError("API Key 无效 (401)", str(e.code))
        if e.code == 403:
            raise AuthError("API Key 无权限 (403)", str(e.code))
        # 404 / other — endpoint may not support model listing
        return None
    except urllib.error.URLError as e:
        raise ConnectionError(f"无法连接到 {url}: {e.reason}")
    except json.JSONDecodeError:
        return None
    except Exception as e:
        raise ConnectionError(f"连接失败: {e}")

    # Parse response based on format
    models = _parse_models_response(data, api_format)
    return models if models else None


def test_connection(
    base_url: str,
    api_key: str,
    api_format: str,
    timeout: int = 10,
) -> tuple[bool, Optional[str], Optional[list[str]]]:
    """Test API connectivity.

    Returns: (success, error_message, model_list_or_None)
    """
    try:
        models = fetch_models(base_url, api_key, api_format, timeout)
        return (True, None, models)
    except AuthError as e:
        return (False, str(e), None)
    except ConnectionError as e:
        return (False, str(e), None)
    except Exception as e:
        return (False, f"未知错误: {e}", None)


def _parse_models_response(data: dict, api_format: str) -> list[str]:
    """Extract model IDs from API response."""
    models = []

    if api_format == "anthropic":
        # Anthropic: {"data": [{"type": "model", "id": "xxx"}, ...]}
        raw = data.get("data", [])
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    mid = item.get("id")
                    if mid:
                        models.append(mid)
    else:
        # OpenAI: {"data": [{"id": "xxx", "object": "model", ...}, ...]}
        raw = data.get("data", [])
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    mid = item.get("id")
                    if mid:
                        models.append(mid)

    return sorted(set(models))


class AuthError(Exception):
    """Authentication failure (401/403)."""
    pass


class ConnectionError(Exception):
    """Network or DNS failure."""
    pass
