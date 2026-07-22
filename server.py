#!/usr/bin/env python3
"""
AI Proxy — Multi-provider HTTPS reverse proxy
      for Claude Code, Codex CLI & Gemini CLI on WSL2

Inspired by CC Switch (farion1231/cc-switch) — a Tauri desktop app for
managing AI coding client configurations and API proxies.

Features:
  - Multi-provider with automatic failover (circuit breaker)
  - Tiered model mapping (haiku/sonnet/opus/fable → custom models)
  - Request body sanitization (strip private underscore-prefixed params)
  - Multi-client support: Claude Code (Anthropic) + Codex (OpenAI Chat)
  - Health check and status/stats endpoints
  - Config-driven (JSON file, no hardcoded values)

Usage:
    python3 server.py                    # Start proxy
    python3 server.py --config custom.json  # Custom config
    python3 server.py --generate-certs     # Regenerate TLS certs
    python3 server.py --status             # Quick health check

Dependencies: Python 3.10+ (stdlib only, zero external packages)
"""

import argparse
import datetime
import http.server
import json
import logging
import os
import random
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# =============================================================================
# Constants
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
CERTS_DIR = SCRIPT_DIR / "certs"
CERT_FILE = CERTS_DIR / "cert.pem"
KEY_FILE = CERTS_DIR / "key.pem"
CA_FILE = CERTS_DIR / "ca.pem"
CA_KEY_FILE = CERTS_DIR / "ca.key"
LOG_FILE = SCRIPT_DIR / "proxy.log"

DEFAULT_LISTEN = "127.0.0.1:19443"
USER_AGENT = "claude-code/2.1.206"

# Standard Anthropic model tiers (lowercased for matching)
MODEL_TIERS = {
    "haiku":  ["haiku"],
    "sonnet": ["sonnet"],
    "opus":   ["opus"],
    "fable":  ["fable"],
}

# Fields that MUST be stripped when forwarding to OpenAI-compatible endpoints
ANTHROPIC_ONLY_FIELDS = {
    "anthropic_version", "anthropic-version",
}

# Fields that some upstreams don't support
STRIP_FIELDS_ANTHROPIC = {"context_management", "output_config"}

# =============================================================================
# Data Models
# =============================================================================

class CircuitState(Enum):
    CLOSED = "closed"          # Normal operation
    OPEN = "open"              # Failing, requests blocked
    HALF_OPEN = "half_open"    # Testing if recovered

@dataclass
class CircuitBreakerConfig:
    max_failures: int = 5
    recovery_seconds: int = 30
    half_open_max_requests: int = 3

@dataclass
class CircuitBreakerStats:
    state: CircuitState
    failure_count: int
    last_failure_time: Optional[float]
    total_failures: int
    total_successes: int
    consecutive_failures: int

class CircuitBreaker:
    """Per-provider circuit breaker to avoid hammering failing upstreams."""

    def __init__(self, config: CircuitBreakerConfig, provider_id: str):
        self.config = config
        self.provider_id = provider_id
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._consecutive_failures = 0
        self._total_failures = 0
        self._total_successes = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_requests = 0
        self._lock = threading.Lock()

    def record_success(self):
        with self._lock:
            self._total_successes += 1
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_requests += 1
                if self._half_open_requests >= self.config.half_open_max_requests:
                    log.info(f"[CB:{self.provider_id}] Recovered, closing circuit")
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._consecutive_failures = 0
                    self._half_open_requests = 0
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures = 0

    def record_failure(self) -> bool:
        """Returns True if circuit just transitioned to OPEN."""
        with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1
            self._failure_count += 1
            self._last_failure_time = time.time()
            tripped = False

            if self._state == CircuitState.HALF_OPEN:
                log.warning(f"[CB:{self.provider_id}] Failed in half-open, reopening circuit")
                self._state = CircuitState.OPEN
                self._half_open_requests = 0
                tripped = True
            elif (self._state == CircuitState.CLOSED
                  and self._failure_count >= self.config.max_failures):
                log.warning(f"[CB:{self.provider_id}] "
                            f"Tripped after {self._failure_count} failures, opening circuit")
                self._state = CircuitState.OPEN
                tripped = True

            return tripped

    def is_available(self) -> bool:
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                # Check if it's time to try half-open
                if (self._last_failure_time is not None
                        and time.time() - self._last_failure_time
                        >= self.config.recovery_seconds):
                    log.info(f"[CB:{self.provider_id}] Recovery timeout elapsed, "
                             f"transitioning to half-open")
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_requests = 0
                    return True
                return False
            # HALF_OPEN
            return self._half_open_requests < self.config.half_open_max_requests

    def get_stats(self) -> CircuitBreakerStats:
        with self._lock:
            return CircuitBreakerStats(
                state=self._state,
                failure_count=self._failure_count,
                last_failure_time=self._last_failure_time,
                total_failures=self._total_failures,
                total_successes=self._total_successes,
                consecutive_failures=self._consecutive_failures,
            )


@dataclass
class ProviderConfig:
    id: str
    name: str
    base_url: str
    api_key_env: str
    api_key: str = ""
    auth_type: str = "x-api-key"  # x-api-key, bearer, header
    api_format: str = "anthropic"  # anthropic, openai_chat, openai_responses
    max_tokens_limit: Optional[int] = None
    model_mapping: dict = field(default_factory=dict)
    extra_headers: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    weight: int = 1  # For weighted random selection among equals

    @classmethod
    def from_dict(cls, pid: str, d: dict) -> "ProviderConfig":
        api_key_env = d.get("api_key_env", "")
        api_key = d.get("api_key", "")
        # Resolve from environment if key_env is set
        if api_key_env and not api_key:
            api_key = os.environ.get(api_key_env, "")
        return cls(
            id=pid,
            name=d.get("name", pid),
            base_url=d.get("base_url", "").rstrip("/"),
            api_key_env=api_key_env,
            api_key=api_key or d.get("api_key", ""),
            auth_type=d.get("auth_type", "x-api-key"),
            api_format=d.get("api_format", "anthropic"),
            max_tokens_limit=d.get("max_tokens_limit"),
            model_mapping=d.get("model_mapping", {}),
            extra_headers=d.get("extra_headers", {}),
            tags=d.get("tags", []),
            weight=d.get("weight", 1),
        )


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class ProxyConfig:
    listen: str = DEFAULT_LISTEN
    default_provider: str = ""
    auto_failover: bool = True
    cert_dir: str = "certs"
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    body_filter: dict = field(default_factory=lambda: {"enabled": True, "whitelist": ["_metadata"]})
    providers: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ProxyConfig":
        if not path.exists():
            log.warning(f"Config {path} not found, using defaults")
            return cls()

        with open(path) as f:
            raw = json.load(f)

        cb_raw = raw.get("circuit_breaker", {})
        cb = CircuitBreakerConfig(
            max_failures=cb_raw.get("max_failures", 5),
            recovery_seconds=cb_raw.get("recovery_seconds", 30),
            half_open_max_requests=cb_raw.get("half_open_max_requests", 3),
        )

        providers = {}
        for pid, pd in raw.get("providers", {}).items():
            providers[pid] = ProviderConfig.from_dict(pid, pd)

        return cls(
            listen=raw.get("listen", DEFAULT_LISTEN),
            default_provider=raw.get("default_provider", ""),
            auto_failover=raw.get("auto_failover", True),
            cert_dir=raw.get("cert_dir", "certs"),
            circuit_breaker=cb,
            body_filter=raw.get("body_filter", {"enabled": True, "whitelist": ["_metadata"]}),
            providers=providers,
        )


# =============================================================================
# Model Mapper
# =============================================================================

class ModelMapper:
    """Tiered model name mapping (haiku/sonnet/opus/fable → custom models)."""

    @staticmethod
    def detect_tier(model: str) -> Optional[str]:
        """Detect which tier a model belongs to based on its name."""
        model_lower = model.lower()

        # Check for [1M] suffix first
        model_clean = model_lower.replace("[1m]", "").strip()

        for tier, keywords in MODEL_TIERS.items():
            if any(kw in model_clean for kw in keywords):
                return tier
        return None

    @staticmethod
    def map_model(model: str, mapping: dict, provider_name: str = "") -> Optional[str]:
        """
        Map a model name using tier-based mapping.

        Resolution order:
        1. Exact model name match in mapping
        2. Tier detection → tier mapping
        3. fable → opus fallback (if fable not mapped but opus is)
        4. Default mapping
        5. None (passthrough original)
        """
        if not mapping:
            return None

        # 1. Exact match
        if model in mapping:
            return mapping[model]

        # 2. Tier match
        tier = ModelMapper.detect_tier(model)
        if tier and tier in mapping:
            return mapping[tier]

        # 3. Fable → Opus fallback
        if tier == "fable" and "fable" not in mapping and "opus" in mapping:
            return mapping["opus"]

        # 4. Default
        if "default" in mapping:
            return mapping["default"]

        return None


# =============================================================================
# Body Filter
# =============================================================================

class BodyFilter:
    """Sanitize request bodies — strip private params and unsupported fields."""

    SCHEMA_KEYS = {"properties", "patternProperties", "definitions", "$defs"}

    @staticmethod
    def filter_private_params(body, whitelist: set = None):
        """
        Recursively remove fields starting with '_' (private/internal params)
        while preserving JSON Schema property names.
        """
        if whitelist is None:
            whitelist = set()

        if isinstance(body, dict):
            is_schema = any(k in body for k in BodyFilter.SCHEMA_KEYS)
            return {
                k: BodyFilter.filter_private_params(v, whitelist)
                for k, v in body.items()
                if not (k.startswith("_") and k not in whitelist and not is_schema)
            }
        elif isinstance(body, list):
            return [BodyFilter.filter_private_params(item, whitelist) for item in body]
        return body

    @staticmethod
    def strip_unsupported_fields(body: dict, api_format: str) -> list:
        """Remove fields not supported by the target API format. Returns list of action strings."""
        actions = []
        if api_format == "anthropic":
            for field in STRIP_FIELDS_ANTHROPIC:
                if field in body:
                    del body[field]
                    actions.append(f"strip {field}")
        return actions

    @staticmethod
    def cap_max_tokens(body: dict, limit: int) -> Optional[str]:
        """Cap max_tokens to limit. Returns action description or None."""
        current = body.get("max_tokens", 0)
        if isinstance(current, (int, float)) and current > limit:
            body["max_tokens"] = limit
            return f"cap max_tokens: {current} -> {limit}"
        return None

    @staticmethod
    def ensure_user_agent(headers: dict) -> str:
        """Ensure User-Agent is set to avoid Cloudflare blocks."""
        ua = headers.get("User-Agent", "")
        if not ua or "python" in ua.lower() or "curl" in ua.lower():
            headers["User-Agent"] = USER_AGENT
            return headers["User-Agent"]
        return ua


# =============================================================================
# Provider Manager (with Circuit Breaker + Failover)
# =============================================================================

class ProviderManager:
    """
    Manages multiple upstream providers with circuit breaker integration
    and automatic failover on failure.
    """

    def __init__(self, config: ProxyConfig):
        self.config = config
        self.providers: dict[str, ProviderConfig] = config.providers
        self._current_id: str = config.default_provider or ""
        self._breakers: dict[str, CircuitBreaker] = {}

        # Initialize circuit breakers
        for pid in self.providers:
            self._breakers[pid] = CircuitBreaker(config.circuit_breaker, pid)

        if not self._current_id and self.providers:
            self._current_id = next(iter(self.providers))
            log.warning(f"No default provider configured, using '{self._current_id}'")

    @property
    def current_id(self) -> str:
        return self._current_id

    def get_provider(self, provider_id: str = None) -> Optional[ProviderConfig]:
        pid = provider_id or self._current_id
        return self.providers.get(pid)

    def get_available_providers(self) -> list[tuple[str, ProviderConfig]]:
        """
        Get healthy (circuit closed) providers. If failover is enabled,
        returns all available ones. Otherwise just the current provider.
        """
        available = []
        if self.config.auto_failover:
            for pid, p in self.providers.items():
                cb = self._breakers.get(pid)
                if cb and cb.is_available():
                    available.append((pid, p))
        else:
            pid = self._current_id
            p = self.providers.get(pid)
            if p:
                cb = self._breakers.get(pid)
                if not cb or cb.is_available():
                    available.append((pid, p))
        return available

    def pick_best_provider(self, needs_tools: bool = False) -> Optional[tuple[str, ProviderConfig]]:
        """Pick the best available provider, preferring tool-capable ones if needed."""
        available = self.get_available_providers()
        if not available:
            return None

        if needs_tools:
            tool_capable = [(pid, p) for pid, p in available
                            if "tool_capable" in p.tags]
            if tool_capable:
                return random.choices(
                    tool_capable,
                    weights=[p.weight for _, p in tool_capable],
                    k=1
                )[0]

        return available[0]

    def record_success(self, provider_id: str):
        cb = self._breakers.get(provider_id)
        if cb:
            cb.record_success()

    def record_failure(self, provider_id: str) -> bool:
        """Returns True if the circuit just tripped (opened)."""
        cb = self._breakers.get(provider_id)
        if cb:
            tripped = cb.record_failure()
            if tripped and self.config.auto_failover:
                self._try_failover(provider_id)
            return tripped
        return False

    def _try_failover(self, failed_id: str):
        """Try to switch to the next available provider."""
        for pid in self.providers:
            if pid == failed_id:
                continue
            cb = self._breakers.get(pid)
            if cb and cb.is_available():
                log.info(f"[Failover] Switching from '{failed_id}' to '{pid}'")
                self._current_id = pid
                return
        log.warning(f"[Failover] No healthy providers available after '{failed_id}' failed")

    def force_switch(self, provider_id: str) -> bool:
        """Manually switch to a specific provider."""
        if provider_id in self.providers:
            self._current_id = provider_id
            log.info(f"[Provider] Manually switched to '{provider_id}'")
            return True
        return False

    def get_all_stats(self) -> dict:
        stats = {}
        for pid, p in self.providers.items():
            cb = self._breakers.get(pid)
            s = cb.get_stats() if cb else CircuitBreakerStats(
                CircuitState.CLOSED, 0, None, 0, 0, 0
            )
            stats[pid] = {
                "name": p.name,
                "api_format": p.api_format,
                "state": s.state.value,
                "failure_count": s.failure_count,
                "total_failures": s.total_failures,
                "total_successes": s.total_successes,
                "consecutive_failures": s.consecutive_failures,
                "last_failure_time": (datetime.datetime.fromtimestamp(s.last_failure_time)
                                      .isoformat() if s.last_failure_time else None),
                "is_current": pid == self._current_id,
            }
        return stats


# =============================================================================
# Request Format Detection
# =============================================================================

def detect_client_type(path: str, body: dict) -> str:
    """
    Detect which client sent the request based on path and body structure.
    Returns: "anthropic" (Claude Code), "openai_chat" (Codex), "unknown"
    """
    path_lower = path.lower()
    if "/v1/messages" in path_lower:
        return "anthropic"
    if "/chat/completions" in path_lower:
        return "openai_chat"
    if "/v1/chat/completions" in path_lower:
        return "openai_chat"
    if "/v1/completions" in path_lower:
        return "openai_chat"

    # Detect from body shape
    if "messages" in body and isinstance(body["messages"], list):
        if body.get("model", "").startswith("gpt-") or body.get("model", "").startswith("o"):
            return "openai_chat"
        if body.get("model", "").startswith("claude-") or body.get("model", "").startswith("anthropic"):
            return "anthropic"
        return "openai_chat"  # Codex-style

    return "unknown"


def build_upstream_path(client_type: str, path: str) -> str:
    """Map incoming path to upstream path based on API format."""
    if client_type == "openai_chat":
        # Codex path: /chat/completions -> /v1/chat/completions
        if path.startswith("/v1/"):
            return path
        return f"/v1{path}" if not path.startswith("/") else f"/v1{path}"

    # Anthropic (Claude Code)
    return path


# =============================================================================
# Request Forwarder
# =============================================================================

def forward_request(
    provider: ProviderConfig,
    client_type: str,
    path: str,
    body_bytes: bytes,
    original_headers: dict,
    timeout: int = 120,
) -> tuple[int, dict, bytes]:
    """
    Forward a transformed request to the upstream provider.

    Returns: (status_code, response_headers, response_body)
    """
    upstream_base = provider.base_url.rstrip("/")
    upstream_path = build_upstream_path(client_type, path)
    upstream_url = f"{upstream_base}{upstream_path}"

    # Build request
    req = urllib.request.Request(upstream_url, data=body_bytes, method="POST")

    # Forward relevant headers
    hop_by_hop = {"host", "content-length", "transfer-encoding",
                  "connection", "expect", "user-agent", "keep-alive"}
    for k, v in original_headers.items():
        if k.lower() not in hop_by_hop:
            req.add_header(k, v)

    # Set correct Host
    req.add_header("Host", upstream_url.split("/")[2].split(":")[0])
    req.add_header("Content-Length", str(len(body_bytes)))
    req.add_header("User-Agent", USER_AGENT)

    # Auth header — only override if provider config has an API key set.
    # Otherwise, the client's original auth header from Claude Code is forwarded as-is.
    if provider.api_key:
        if provider.auth_type == "x-api-key":
            if client_type == "anthropic":
                req.add_header("x-api-key", provider.api_key)
                req.add_header("anthropic-version", "2023-06-01")
            else:
                req.add_header("Authorization", f"Bearer {provider.api_key}")
        elif provider.auth_type == "bearer":
            req.add_header("Authorization", f"Bearer {provider.api_key}")
        elif provider.auth_type == "header":
            for k, v in provider.extra_headers.items():
                req.add_header(k, v)
            req.add_header("Authorization", f"Bearer {provider.api_key}")

    # Extra headers from config
    for k, v in provider.extra_headers.items():
        if k.lower() != "authorization":
            req.add_header(k, v)

    # Disable SSL verification for local testing — upstream should be HTTPS
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    resp_body = resp.read()

    # Build response headers
    resp_headers = {}
    for k, v in resp.headers.items():
        if k.lower() not in hop_by_hop:
            resp_headers[k] = v
    resp_headers["Content-Length"] = str(len(resp_body))

    return resp.status, resp_headers, resp_body


# =============================================================================
# Request Transform Pipeline
# =============================================================================

def transform_request(
    body_bytes: bytes,
    provider: ProviderConfig,
    client_type: str,
    filter_config: dict,
) -> tuple[bytes, list[str]]:
    """
    Full request transform pipeline:
    1. Parse JSON body
    2. Filter private params (underscore-prefixed fields)
    3. Strip unsupported fields
    4. Apply model mapping
    5. Cap max_tokens

    Returns: (transformed_bytes, action_log)
    """
    actions = []

    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return body_bytes, actions

    if not isinstance(body, dict):
        return body_bytes, actions

    # 1. Private param filtering
    if filter_config.get("enabled", True):
        whitelist = set(filter_config.get("whitelist", []))
        before_len = len(json.dumps(body))
        body = BodyFilter.filter_private_params(body, whitelist)
        after_len = len(json.dumps(body))
        if before_len != after_len:
            actions.append(f"filtered private params: {before_len} -> {after_len} bytes")

    # 2. Model mapping
    original_model = body.get("model", "")
    if original_model:
        mapped = ModelMapper.map_model(
            original_model, provider.model_mapping, provider.name
        )
        if mapped and mapped != original_model:
            actions.append(f"reroute model: {original_model} -> {mapped}")
            body["model"] = mapped

    # 3. Strip unsupported fields
    actions.extend(BodyFilter.strip_unsupported_fields(body, provider.api_format))

    # 4. Cap max_tokens
    if provider.max_tokens_limit:
        action = BodyFilter.cap_max_tokens(body, provider.max_tokens_limit)
        if action:
            actions.append(action)

    # 5. Handle `stream: True` -> inject stream_options for OpenAI-compatible
    if provider.api_format == "openai_chat" and body.get("stream"):
        body.setdefault("stream_options", {})["include_usage"] = True
        actions.append("injected stream_options.include_usage")

    return json.dumps(body).encode("utf-8"), actions


# =============================================================================
# Certificate Management
# =============================================================================

def ensure_certs(cert_dir: Path):
    """Generate self-signed CA + server certificate if they don't exist."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"
    ca_file = cert_dir / "ca.pem"
    ca_key_file = cert_dir / "ca.key"

    if cert_file.exists() and key_file.exists() and ca_file.exists():
        try:
            result = subprocess.run(
                ["openssl", "x509", "-in", str(cert_file), "-noout", "-enddate"],
                capture_output=True, text=True, check=True
            )
            end_str = result.stdout.strip().split("=", 1)[1]
            end_date = datetime.datetime.strptime(end_str, "%b %d %H:%M:%S %Y %Z")
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            if (end_date - now).days > 30:
                print(f"  Certificates valid until {end_date}")
                return
            print("  Certificates expiring soon, regenerating...")
        except Exception as e:
            print(f"  Could not check cert validity ({e}), regenerating...")

    print("  Generating root CA...")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(ca_key_file), "-out", str(ca_file),
        "-days", "365", "-nodes",
        "-subj", "/CN=OpenCode Proxy CA",
    ], check=True, capture_output=True)

    print("  Generating server CSR...")
    csr_file = cert_dir / "server.csr"
    server_key_file = cert_dir / "server.key"
    subprocess.run([
        "openssl", "req", "-new", "-newkey", "rsa:2048",
        "-keyout", str(server_key_file), "-out", str(csr_file),
        "-nodes", "-subj", "/CN=localhost",
        "-addext", "subjectAltName = IP:127.0.0.1,DNS:localhost",
    ], check=True, capture_output=True)

    print("  Signing server cert with CA...")
    san_file = cert_dir / "san.ext"
    san_file.write_text("subjectAltName = IP:127.0.0.1,DNS:localhost\n")
    try:
        subprocess.run([
            "openssl", "x509", "-req", "-in", str(csr_file),
            "-CA", str(ca_file), "-CAkey", str(ca_key_file),
            "-CAcreateserial", "-out", str(cert_file),
            "-days", "365", "-extfile", str(san_file),
        ], check=True, capture_output=True)
    finally:
        # Move server.key → key.pem before cleanup
        if server_key_file.exists():
            import shutil
            shutil.copy2(str(server_key_file), str(key_file))
        csr_file.unlink(missing_ok=True)
        san_file.unlink(missing_ok=True)
        server_key_file.unlink(missing_ok=True)
        (cert_dir / "ca.srl").unlink(missing_ok=True)

    os.chmod(ca_key_file, 0o600)
    os.chmod(key_file, 0o600)

    result = subprocess.run(
        ["openssl", "verify", "-CAfile", str(ca_file), str(cert_file)],
        capture_output=True, text=True
    )
    print(f"  Cert chain: {result.stdout.strip()}")

    print()
    print(f"  CA cert for Claude Code NODE_EXTRA_CA_CERTS:")
    print(f"    {ca_file}")


# =============================================================================
# Logging Setup
# =============================================================================

log = logging.getLogger("proxy")

def setup_logging(log_file: Path, verbose: bool = False):
    handlers = [logging.FileHandler(log_file)]
    if verbose or os.environ.get("PROXY_VERBOSE"):
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )

    # Also log to console for key events
    global _console_log
    _console_log = handlers


# =============================================================================
# HTTP Proxy Handler
# =============================================================================

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTPS reverse proxy with multi-provider support and failover."""

    # Suppress BaseHTTPRequestHandler's default access log
    def log_message(self, *args):
        pass

    def _get_provider_manager(self):
        return self.server.provider_manager  # type: ignore

    def _get_filter_config(self):
        return self.server.filter_config  # type: ignore

    def do_GET(self):
        if self.path == "/health":
            self._handle_health()
        elif self.path == "/status":
            self._handle_status()
        elif "/v1/models" in self.path:
            self._handle_models()
        else:
            self._send_error(404, "Not Found")

    def do_POST(self):
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl) if cl else b""

        # Parse client type
        try:
            body_json = json.loads(body) if body else {}
        except json.JSONDecodeError:
            body_json = {}

        client_type = detect_client_type(self.path, body_json)
        pm = self._get_provider_manager()

        # Determine if tools are needed (for selecting tool-capable provider)
        needs_tools = "tools" in body_json and body_json["tools"]

        # Pick provider
        pick = pm.pick_best_provider(needs_tools=needs_tools)
        if not pick:
            self._send_error(503, "No healthy providers available", {
                "error": "all_providers_unavailable",
                "message": "All providers are currently circuit-broken or not configured"
            })
            return

        pid, provider = pick
        log.info(f"POST {self.path}: using provider '{pid}' ({provider.name}), "
                 f"{len(body)} bytes")

        # Transform pipeline
        filter_config = self._get_filter_config()
        transformed, actions = transform_request(body, provider, client_type, filter_config)

        if actions:
            log.info(f"  [{pid}] {', '.join(actions)}")

        try:
            status, resp_headers, resp_body = forward_request(
                provider, client_type, self.path, transformed,
                dict(self.headers.items())
            )

            # Record success
            pm.record_success(pid)
            log.info(f"  -> {status} OK ({len(resp_body)} bytes) [{pid}]")

            self.send_response(status)
            for k, v in resp_headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp_body)

        except urllib.error.HTTPError as e:
            err_body = e.read()
            pm.record_failure(pid)
            log.warning(f"  -> {e.code} [{pid}]: {err_body[:200]}")

            # If auto-failover is on, retry with next provider
            if self.server.auto_failover and e.code in (429, 502, 503):
                # Try next available provider
                next_pick = pm.pick_best_provider(needs_tools)
                if next_pick and next_pick[0] != pid:
                    nid, nprovider = next_pick
                    log.info(f"  Retrying with '{nid}' ({nprovider.name})...")
                    try:
                        status2, h2, b2 = forward_request(
                            nprovider, client_type, self.path, transformed,
                            dict(self.headers.items())
                        )
                        pm.record_success(nid)
                        log.info(f"  -> {status2} OK ({len(b2)} bytes) [{nid}] (retry)")
                        self.send_response(status2)
                        for k, v in h2.items():
                            self.send_header(k, v)
                        self.end_headers()
                        self.wfile.write(b2)
                        return
                    except urllib.error.HTTPError:
                        pm.record_failure(nid)
                    except Exception:
                        pm.record_failure(nid)

            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)

        except Exception as e:
            pm.record_failure(pid)
            log.error(f"  -> 502 [{pid}]: {e}")
            self._send_error(502, str(e))

    def _handle_health(self):
        """Simple health check endpoint."""
        pm = self._get_provider_manager()
        available = pm.get_available_providers()
        status = "healthy" if available else "degraded"
        body = json.dumps({
            "status": status,
            "providers_available": len(available),
            "timestamp": datetime.datetime.now().isoformat(),
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_status(self):
        """Detailed proxy status with provider health stats."""
        pm = self._get_provider_manager()
        stats = pm.get_all_stats()

        body = json.dumps({
            "auto_failover": self.server.auto_failover,
            "current_provider": pm.current_id,
            "providers": stats,
            "uptime_seconds": int(time.time() - self.server.start_time),
            "timestamp": datetime.datetime.now().isoformat(),
        }, indent=2).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_models(self):
        """Return available models from configured providers."""
        pm = self._get_provider_manager()
        models = []
        for pid, p in pm.providers.items():
            for tier, model in p.model_mapping.items():
                models.append({
                    "id": model,
                    "provider": pid,
                    "provider_name": p.name,
                    "tier": tier,
                    "format": p.api_format,
                })

        body = json.dumps({"models": models}, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str, extra: dict = None):
        err = {"error": message}
        if extra:
            err.update(extra)
        body = json.dumps(err).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# =============================================================================
# Server
# =============================================================================

class ProxyServer(http.server.HTTPServer):
    """HTTPS proxy server with injected provider manager and config."""
    allow_reuse_address = True

    def __init__(self, provider_manager: ProviderManager, config: ProxyConfig):
        listen_host, listen_port_str = config.listen.split(":")
        listen_port = int(listen_port_str)

        super().__init__((listen_host, listen_port), ProxyHandler)
        self.provider_manager = provider_manager
        self.filter_config = config.body_filter
        self.auto_failover = config.auto_failover
        self.start_time = time.time()
        self.timeout = 120


def run_server(config: ProxyConfig, verbose: bool = False):
    """Start the proxy server."""
    # Setup
    cert_dir = SCRIPT_DIR / config.cert_dir
    ensure_certs(cert_dir)

    # Initialize provider manager
    pm = ProviderManager(config)

    if not pm.providers:
        log.error("No providers configured! Edit config.json or set environment variables.")
        print("\n  ERROR: No providers configured!")
        print("  Edit config.json or set the OPENCODE_API_KEY environment variable.")
        sys.exit(1)

    # Check API keys
    missing_keys = []
    for pid, p in pm.providers.items():
        if not p.api_key:
            missing_keys.append(f"    - {pid} ({p.name}): set env {p.api_key_env}")
    if missing_keys:
        log.warning("Missing API keys:\n" + "\n".join(missing_keys))

    # Create HTTPS server
    listen_host, listen_port_str = config.listen.split(":")
    listen_port = int(listen_port_str)

    srv = ProxyServer(pm, config)

    # Wrap with TLS
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_dir / "cert.pem"), str(cert_dir / "key.pem"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)

    print()
    print(f"  ┌─ OpenCode Proxy v2 ─────────────────────────────┐")
    print(f"  │  HTTPS: https://{config.listen}/                  │")
    print(f"  │  Log:   {LOG_FILE}            │")
    print(f"  │  CA:    {cert_dir}/ca.pem            │")
    print(f"  ├─ Providers ─────────────────────────────────────┤")
    for pid, p in pm.providers.items():
        status = "●" if p.api_key else "○"
        current = " ←" if pid == pm.current_id else ""
        tags_str = f" [{', '.join(p.tags)}]" if p.tags else ""
        print(f"  │  {status} {pid:<20} {p.name}{tags_str}{current}")
    print(f"  ├─ Features ──────────────────────────────────────┤")
    print(f"  │  Auto-failover: {'ON' if config.auto_failover else 'OFF':>15}         │")
    print(f"  │  Circuit breaker: {config.circuit_breaker.max_failures:>3} faults / "
          f"{config.circuit_breaker.recovery_seconds}s recovery        │")
    print(f"  │  Model mapping: tiered (haiku/sonnet/opus/fable)            │")
    print(f"  │  Body filter: {'ON' if config.body_filter.get('enabled') else 'OFF':>14}         │")
    print(f"  ├─ Endpoints ─────────────────────────────────────┤")
    print(f"  │  POST /v1/messages        → Anthropic (Claude)  │")
    print(f"  │  POST /chat/completions   → OpenAI Chat (Codex) │")
    print(f"  │  GET  /health             → Health check        │")
    print(f"  │  GET  /status             → Provider status     │")
    print(f"  │  GET  /v1/models          → Model listing       │")
    print(f"  └─────────────────────────────────────────────────┘")
    print()
    print(f"  Claude Code config (~/.claude/settings.json env):")
    print(f'    ANTHROPIC_BASE_URL = "https://{config.listen}/"')
    print(f'    NODE_EXTRA_CA_CERTS = "{cert_dir / "ca.pem"}"')
    print()
    print(f"  Press Ctrl+C to stop")
    print()

    log.info(f"Proxy listening on https://{config.listen}")
    log.info(f"Providers: {list(pm.providers.keys())}")
    log.info(f"Auto-failover: {config.auto_failover}")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        srv.shutdown()
        print("\n  Proxy stopped.")


# =============================================================================
# CLI Entry Point
# =============================================================================

def cmd_status(config: ProxyConfig):
    """Quick status check via the health endpoint."""
    listen_host, listen_port_str = config.listen.split(":")
    listen_port = int(listen_port_str)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        req = urllib.request.Request(f"https://{config.listen}/health")
        resp = urllib.request.urlopen(req, timeout=5, context=ctx)
        data = json.loads(resp.read())
        print(f"  Proxy status: {data['status']}")
        print(f"  Providers available: {data['providers_available']}")

        # Also get provider details
        req2 = urllib.request.Request(f"https://{config.listen}/status")
        resp2 = urllib.request.urlopen(req2, timeout=5, context=ctx)
        data2 = json.loads(resp2.read())
        print(f"  Current provider: {data2.get('current_provider', '?')}")
        print(f"  Uptime: {data2.get('uptime_seconds', 0)}s")
        print()
        for pid, info in data2.get("providers", {}).items():
            icon = "●" if info["state"] == "closed" else \
                   "◐" if info["state"] == "half_open" else "○"
            fmt = info.get("api_format", "?")
            cur = "  ←" if info.get("is_current") else ""
            print(f"  {icon} {pid:<20} [{fmt:>18}] {info['name']}{cur}")

    except urllib.error.URLError:
        print("  Proxy is not running.")
    except ConnectionRefusedError:
        print("  Proxy is not running.")


def main():
    parser = argparse.ArgumentParser(
        description="OpenCode Proxy v2 — Multi-provider HTTPS proxy "
                    "for Claude Code / Codex on WSL2"
    )
    parser.add_argument("--config", default=str(CONFIG_FILE),
                        help="Config file path (default: config.json)")
    parser.add_argument("--generate-certs", action="store_true",
                        help="Regenerate TLS certificates")
    parser.add_argument("--status", action="store_true",
                        help="Check proxy health status")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose logging (also to stdout)")
    parser.add_argument("--port", type=int,
                        help="Override listen port")
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    config = ProxyConfig.load(config_path)

    # Override port if specified
    if args.port:
        host = config.listen.split(":")[0]
        config.listen = f"{host}:{args.port}"

    # Setup logging
    setup_logging(LOG_FILE, verbose=args.verbose)

    if args.generate_certs:
        print("  Regenerating certificates...")
        cert_dir = SCRIPT_DIR / config.cert_dir
        # Force regeneration by removing existing
        for f in [cert_dir / "cert.pem", cert_dir / "key.pem",
                  cert_dir / "ca.pem", cert_dir / "ca.key"]:
            if f.exists():
                f.unlink()
        ensure_certs(cert_dir)
        return

    if args.status:
        cmd_status(config)
        return

    run_server(config, verbose=args.verbose)


if __name__ == "__main__":
    main()
