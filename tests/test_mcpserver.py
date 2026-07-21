"""Unit tests for toinflux.mcpserver (embedded MCP server: OAuth provider,
state persistence, login throttling, and the HTTP surface)."""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import anyio
import pytest
from starlette.testclient import TestClient

from toinflux.mcpserver import (
    ACCESS_TOKEN_TTL_SECONDS,
    LOGIN_FAILURE_LIMIT,
    LOGIN_TXN_TTL_SECONDS,
    LoginThrottle,
    OAuthStateStore,
    SendToInfluxOAuthProvider,
    build_mcp_server,
    resolve_state_path,
)

MCP_PUBLIC_URL = "https://mcp.example.org"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
MCP_USER = "gavin"
MCP_PASSWORD = "correct-horse"


@pytest.fixture
def mcp_settings(tmp_path):
    """Settings dict with a fully-enabled mcp block and a temp state file."""
    return {
        "mcp": {
            "bind_address": "127.0.0.1:8420",
            "public_url": MCP_PUBLIC_URL,
            "user": MCP_USER,
            "password": MCP_PASSWORD,
            "state_file": str(tmp_path / "mcp-oauth-state.json"),
        },
    }


@pytest.fixture
def http_client(mcp_settings):
    """TestClient against the built app, with the lifespan running (required by
    the /mcp session manager) and the public hostname as the Host header (so the
    DNS-rebinding allowlist sees what a reverse-proxied request would carry)."""
    server = build_mcp_server(mcp_settings)
    with TestClient(server.streamable_http_app(), base_url=MCP_PUBLIC_URL) as client:
        yield client


def _pkce_pair():
    verifier = secrets.token_urlsafe(43)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _register_client(client):
    response = client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def _authorize_to_login_txn(client, client_id, challenge, state="st4te"):
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 307), response.text
    location = response.headers["location"]
    assert location.startswith(f"{MCP_PUBLIC_URL}/login?txn=")
    return parse_qs(urlparse(location).query)["txn"][0]


def _login_for_code(client, txn, state="st4te"):
    response = client.post(
        "/login",
        data={"txn": txn, "username": MCP_USER, "password": MCP_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["state"] == [state]
    return query["code"][0]


def _full_token_flow(client):
    """Run register -> authorize -> login -> token; returns (client_id, tokens)."""
    client_id = _register_client(client)
    verifier, challenge = _pkce_pair()
    txn = _authorize_to_login_txn(client, client_id, challenge)
    code = _login_for_code(client, txn)
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
        },
    )
    assert response.status_code == 200, response.text
    return client_id, response.json()


class TestOAuthStateStore:
    """Tests for the persisted client/refresh-token store."""

    def test_missing_file_starts_empty(self, tmp_path):
        store = OAuthStateStore(str(tmp_path / "absent.json"))
        assert store.clients == {} and store.refresh_tokens == {}

    def test_save_and_reload_round_trip(self, tmp_path):
        path = str(tmp_path / "state.json")
        store = OAuthStateStore(path)
        store.clients["abc"] = {"client_id": "abc"}
        store.refresh_tokens["hash"] = {"client_id": "abc", "expires_at": None}
        store.save()
        reloaded = OAuthStateStore(path)
        assert reloaded.clients == store.clients
        assert reloaded.refresh_tokens == store.refresh_tokens

    def test_state_file_is_owner_only(self, tmp_path):
        path = str(tmp_path / "state.json")
        store = OAuthStateStore(path)
        store.save()
        assert os.stat(path).st_mode & 0o777 == 0o600

    def test_corrupt_file_warns_and_starts_empty(self, tmp_path, caplog):
        path = str(tmp_path / "state.json")
        with open(path, "w", encoding="utf8") as f:
            f.write("{not json")
        with caplog.at_level(logging.WARNING):
            store = OAuthStateStore(path)
        assert store.clients == {}
        assert any("could not be read" in record.message for record in caplog.records)

    def test_prune_expired_refresh_tokens(self, tmp_path):
        store = OAuthStateStore(str(tmp_path / "state.json"))
        store.refresh_tokens["old"] = {"client_id": "a", "expires_at": time.time() - 1}
        store.refresh_tokens["live"] = {"client_id": "a", "expires_at": time.time() + 1000}
        assert store.prune_expired_refresh_tokens() is True
        assert list(store.refresh_tokens) == ["live"]


class TestLoginThrottle:
    """Tests for the login brute-force lockout."""

    def test_not_locked_below_limit(self):
        throttle = LoginThrottle(limit=3, lockout_seconds=100)
        throttle.record_failure("1.2.3.4")
        throttle.record_failure("1.2.3.4")
        assert throttle.locked_out("1.2.3.4") == 0

    def test_locked_at_limit(self):
        throttle = LoginThrottle(limit=2, lockout_seconds=100)
        throttle.record_failure("1.2.3.4")
        throttle.record_failure("1.2.3.4")
        assert throttle.locked_out("1.2.3.4") > 0
        # A different address is unaffected
        assert throttle.locked_out("5.6.7.8") == 0

    def test_success_resets(self):
        throttle = LoginThrottle(limit=2, lockout_seconds=100)
        throttle.record_failure("1.2.3.4")
        throttle.record_success("1.2.3.4")
        throttle.record_failure("1.2.3.4")
        assert throttle.locked_out("1.2.3.4") == 0

    def test_lockout_expires(self):
        throttle = LoginThrottle(limit=1, lockout_seconds=100)
        throttle.record_failure("1.2.3.4")
        assert throttle.locked_out("1.2.3.4") > 0
        with patch("toinflux.mcpserver.time.time", return_value=time.time() + 101):
            assert throttle.locked_out("1.2.3.4") == 0


class TestResolveStatePath:
    """Tests for the state-file path default and override."""

    def test_explicit_setting_wins(self):
        settings = {"mcp": {"state_file": "/var/lib/x/state.json"}}
        assert resolve_state_path(settings) == "/var/lib/x/state.json"

    def test_defaults_next_to_settings_file(self):
        path = resolve_state_path({"mcp": {}}, "/etc/send-to-influx/settings.yaml")
        assert path == "/etc/send-to-influx/mcp-oauth-state.json"


class TestHttpSurface:
    """Tests against the real Starlette app the SDK builds."""

    def test_mcp_endpoint_requires_auth(self, http_client):
        response = http_client.post("/mcp", json={})
        assert response.status_code == 401
        assert "Bearer" in response.headers.get("WWW-Authenticate", "")

    def test_metadata_advertises_public_url(self, http_client):
        response = http_client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        metadata = response.json()
        assert metadata["issuer"].rstrip("/") == MCP_PUBLIC_URL
        for endpoint in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            assert metadata[endpoint].startswith(MCP_PUBLIC_URL)

    def test_full_flow_reaches_authenticated_endpoint(self, http_client):
        _client_id, tokens = _full_token_flow(http_client)
        response = http_client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert response.status_code == 200, response.text

    def test_login_with_unknown_txn_rejected(self, http_client):
        assert http_client.get("/login", params={"txn": "nope"}).status_code == 400
        response = http_client.post("/login", data={"txn": "nope", "username": MCP_USER, "password": MCP_PASSWORD})
        assert response.status_code == 400

    def test_wrong_password_rejected_and_logged(self, http_client, caplog):
        client_id = _register_client(http_client)
        _verifier, challenge = _pkce_pair()
        txn = _authorize_to_login_txn(http_client, client_id, challenge)
        with caplog.at_level(logging.WARNING):
            response = http_client.post("/login", data={"txn": txn, "username": MCP_USER, "password": "wrong"})
        assert response.status_code == 401
        assert any("Failed MCP login attempt" in record.message for record in caplog.records)

    def test_lockout_after_repeated_failures(self, http_client):
        client_id = _register_client(http_client)
        _verifier, challenge = _pkce_pair()
        txn = _authorize_to_login_txn(http_client, client_id, challenge)
        for _ in range(LOGIN_FAILURE_LIMIT):
            response = http_client.post("/login", data={"txn": txn, "username": MCP_USER, "password": "wrong"})
            assert response.status_code == 401
        response = http_client.post("/login", data={"txn": txn, "username": MCP_USER, "password": MCP_PASSWORD})
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    def test_refresh_token_rotation(self, http_client):
        client_id, tokens = _full_token_flow(http_client)
        response = http_client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        assert response.status_code == 200, response.text
        rotated = response.json()
        assert rotated["refresh_token"] != tokens["refresh_token"]
        # The consumed refresh token is dead
        response = http_client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        assert response.status_code == 400

    def test_auth_code_is_single_use(self, http_client):
        client_id = _register_client(http_client)
        verifier, challenge = _pkce_pair()
        txn = _authorize_to_login_txn(http_client, client_id, challenge)
        code = _login_for_code(http_client, txn)
        token_request = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
        }
        assert http_client.post("/token", data=token_request).status_code == 200
        assert http_client.post("/token", data=token_request).status_code == 400

    def test_state_file_never_contains_raw_refresh_token(self, http_client, mcp_settings):
        client_id, tokens = _full_token_flow(http_client)
        with open(mcp_settings["mcp"]["state_file"], encoding="utf8") as f:
            raw = f.read()
        assert tokens["refresh_token"] not in raw
        assert hashlib.sha256(tokens["refresh_token"].encode()).hexdigest() in raw
        assert client_id in raw

    def test_state_survives_server_restart(self, http_client, mcp_settings):
        """The core persistence AC: a refresh token issued by one server instance
        is honoured by a fresh instance built over the same state file - i.e. the
        Claude connector survives a service restart without re-authenticating."""
        client_id, tokens = _full_token_flow(http_client)
        restarted = build_mcp_server(mcp_settings)
        with TestClient(restarted.streamable_http_app(), base_url=MCP_PUBLIC_URL) as second:
            response = second.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": client_id,
                },
            )
            assert response.status_code == 200, response.text


class TestProviderInternals:
    """Direct provider tests for behaviour the HTTP layer can't easily reach."""

    @pytest.fixture
    def provider(self, tmp_path):
        return SendToInfluxOAuthProvider(
            public_url=MCP_PUBLIC_URL,
            expected_user=MCP_USER,
            expected_password=MCP_PASSWORD,
            state_store=OAuthStateStore(str(tmp_path / "state.json")),
        )

    def test_expired_access_token_rejected(self, provider):
        from mcp.server.auth.provider import AccessToken

        provider._access_tokens["tok"] = AccessToken(
            token="tok", client_id="c", scopes=[], expires_at=int(time.time() - 1)
        )
        assert anyio.run(provider.load_access_token, "tok") is None
        assert "tok" not in provider._access_tokens

    def test_live_access_token_expiry_is_bounded(self, provider):
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(client_id="c", redirect_uris=[REDIRECT_URI])
        tokens = provider._issue_tokens(client, [], subject=MCP_USER)
        access = anyio.run(provider.load_access_token, tokens.access_token)
        assert access is not None
        assert access.expires_at <= time.time() + ACCESS_TOKEN_TTL_SECONDS + 1

    def test_login_transaction_expires(self, provider):
        provider._transactions["txn"] = {
            "client_id": "c",
            "params": None,
            "created_at": time.time() - LOGIN_TXN_TTL_SECONDS - 1,
        }
        assert provider.transaction_valid("txn") is False
        assert "txn" not in provider._transactions

    def test_revoke_refresh_token_persists_removal(self, provider, tmp_path):
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(client_id="c", redirect_uris=[REDIRECT_URI])
        tokens = provider._issue_tokens(client, [], subject=MCP_USER)
        refresh = anyio.run(provider.load_refresh_token, client, tokens.refresh_token)
        assert refresh is not None
        anyio.run(provider.revoke_token, refresh)
        assert anyio.run(provider.load_refresh_token, client, tokens.refresh_token) is None
        with open(str(tmp_path / "state.json"), encoding="utf8") as f:
            assert json.load(f)["refresh_tokens"] == {}

    def test_check_credentials_requires_both_halves(self, provider):
        assert provider.check_credentials(MCP_USER, MCP_PASSWORD) is True
        assert provider.check_credentials(MCP_USER, "wrong") is False
        assert provider.check_credentials("wrong", MCP_PASSWORD) is False

    def test_malformed_client_entry_is_dropped_not_raised(self, provider, caplog):
        provider.state.clients["bad"] = {"redirect_uris": "not-a-list"}
        with caplog.at_level(logging.WARNING):
            assert anyio.run(provider.get_client, "bad") is None
        assert "bad" not in provider.state.clients
        assert any("malformed MCP OAuth client entry" in record.message for record in caplog.records)

    def test_malformed_refresh_entry_is_dropped_not_raised(self, provider, caplog):
        from toinflux.mcpserver import _hash_token

        provider.state.refresh_tokens[_hash_token("tok1")] = {"scopes": []}  # no client_id
        provider.state.refresh_tokens[_hash_token("tok2")] = "not-a-dict"
        with caplog.at_level(logging.WARNING):
            assert anyio.run(provider.load_refresh_token, None, "tok1") is None
            assert anyio.run(provider.load_refresh_token, None, "tok2") is None
        assert provider.state.refresh_tokens == {}


class TestPublicUrlWithPort:
    """A non-443 public_url must produce well-formed Host/Origin allowlist
    entries (netloc for the exact match, port-less hostname for the wildcard -
    never a malformed \"host:port:*\")."""

    def test_allowlists_are_well_formed(self, tmp_path):
        settings = {
            "mcp": {
                "bind_address": "127.0.0.1:8420",
                "public_url": "https://mcp.example.org:8443",
                "user": MCP_USER,
                "password": MCP_PASSWORD,
                "state_file": str(tmp_path / "state.json"),
            },
        }
        server = build_mcp_server(settings)
        security = server.settings.transport_security
        assert "mcp.example.org:8443" in security.allowed_hosts
        assert "mcp.example.org:*" in security.allowed_hosts
        assert not any(entry.count(":") > 1 for entry in security.allowed_hosts if "[" not in entry)
        assert "https://mcp.example.org:8443" in security.allowed_origins

    def test_ipv6_public_url_entries_keep_their_brackets(self, tmp_path):
        settings = {
            "mcp": {
                "bind_address": "127.0.0.1:8420",
                "public_url": "https://[2001:db8::5]:8443",
                "user": MCP_USER,
                "password": MCP_PASSWORD,
                "state_file": str(tmp_path / "state.json"),
            },
        }
        server = build_mcp_server(settings)
        security = server.settings.transport_security
        assert "[2001:db8::5]:8443" in security.allowed_hosts
        assert "[2001:db8::5]:*" in security.allowed_hosts
        assert not any(entry.startswith("2001") for entry in security.allowed_hosts)
        assert "https://[2001:db8::5]:8443" in security.allowed_origins
        assert "https://[2001:db8::5]" in security.allowed_origins

    def test_proxied_request_with_ported_host_header_is_served(self, tmp_path):
        settings = {
            "mcp": {
                "bind_address": "127.0.0.1:8420",
                "public_url": "https://mcp.example.org:8443",
                "user": MCP_USER,
                "password": MCP_PASSWORD,
                "state_file": str(tmp_path / "state.json"),
            },
        }
        server = build_mcp_server(settings)
        with TestClient(server.streamable_http_app(), base_url="https://mcp.example.org:8443") as client:
            response = client.get("/.well-known/oauth-authorization-server")
            assert response.status_code == 200
            assert response.json()["issuer"].rstrip("/") == "https://mcp.example.org:8443"
