"""Remote MCP server embedded in the send-to-influx process.

Runs a Streamable-HTTP MCP server (via the official ``mcp`` SDK) inside its own
daemon thread, so Claude Desktop/Mobile can ask about collected device data. The
server binds a private interface only - TLS termination is the deploying user's
reverse proxy, whose external address is ``mcp.public_url``. Authentication is
OAuth 2.1 using the SDK's built-in authorization server: dynamic client
registration, PKCE, and token issuance are handled by the SDK; this module
supplies the storage/provider logic, the resource-owner login page (gated on
``mcp.user``/``mcp.password``), and its brute-force throttling.

OAuth client registrations and refresh tokens persist across service restarts in
a JSON state file (``mcp.state_file``, defaulting to alongside the settings
file) - the packaged install restarts the service on every upgrade, and losing
them would break the Claude connector until a human re-authenticated. Refresh
tokens are stored as SHA-256 hashes, so the file yields no replayable token;
access tokens are short-lived and kept in memory only (a restart invalidates
them, and the client recovers silently via its refresh token).

The ``mcp`` SDK is imported only here (like ``paho-mqtt`` in toinflux/mqtt.py),
keeping every other execution path importable without it.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlparse

from toinflux.exceptions import ConfigError
from toinflux.general import parse_mcp_bind_address

ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 90 * 24 * 3600
AUTH_CODE_TTL_SECONDS = 300
LOGIN_TXN_TTL_SECONDS = 600
# After LOGIN_FAILURE_LIMIT consecutive failures from one client address, the
# login page refuses further attempts for LOGIN_LOCKOUT_SECONDS. Behind a
# reverse proxy every request carries the proxy's address, so this is
# effectively a global lockout - deliberately so for a single-user login page:
# it can't be sidestepped by rotating source addresses, and locking out the one
# real user alongside the attacker is an acceptable cost at this scale.
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCKOUT_SECONDS = 300

# Restart delay for the server thread after an unexpected crash. A flat delay
# rather than the collectors' exponential backoff: there is no remote service to
# avoid hammering, only local re-binds.
SERVER_RESTART_SECONDS = 30

_LOGIN_FORM_TEMPLATE = """<!DOCTYPE html>
<html><head><title>send-to-influx MCP login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center; margin-top: 10vh; }}
form {{ display: flex; flex-direction: column; gap: 0.6em; min-width: 16em; }}
input {{ padding: 0.4em; }}
.error {{ color: #b00020; }}
</style></head><body>
<form method="post" action="/login" autocomplete="off">
<h2>send-to-influx</h2>
{error}
<input type="hidden" name="txn" value="{txn}">
<label>Username <input type="text" name="username" autofocus></label>
<label>Password <input type="password" name="password"></label>
<button type="submit">Authorise</button>
</form></body></html>"""


def _hash_token(token):
    """Return the hex SHA-256 of a token string - the only form a refresh token
    is ever persisted in, so the state file never contains a replayable value.

    :param token: token string
    :type token: str
    :rtype: str
    """
    return hashlib.sha256(token.encode("utf8")).hexdigest()


def _refresh_entry_expired(entry, now):
    """Return True if a persisted refresh-token entry is expired or malformed.

    A non-mapping entry, or one whose ``expires_at`` is present but not a real
    number (hand-edited state, schema drift), counts as expired - the state
    file's contract is that bad state is recoverable, and a naive
    ``entry["expires_at"] < now`` would otherwise raise ``TypeError`` on a
    string/None and break token issuance. A missing/``None`` ``expires_at`` means
    "never expires" and is honoured. ``bool`` is excluded explicitly because it
    is an ``int`` subclass and a stray ``true`` should not read as epoch 1.

    :param entry: the persisted entry (any JSON-decoded value)
    :param now: current unix time
    :type now: float
    :rtype: bool
    """
    if not isinstance(entry, dict):
        return True
    expires_at = entry.get("expires_at")
    if expires_at is None:
        return False
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return True
    return expires_at < now


class OAuthStateStore:
    """Persistent OAuth state: dynamic client registrations and refresh-token
    hashes, saved atomically to a 0600 JSON file on every mutation.

    Only the server thread's event loop touches an instance, but a lock guards
    save() anyway so a future second caller can't interleave partial writes.
    """

    def __init__(self, state_path):
        """
        :param state_path: path of the JSON state file (created on first save)
        :type state_path: str
        """
        self.state_path = state_path
        self._lock = threading.Lock()
        self.clients = {}
        self.refresh_tokens = {}
        self._load()

    def _tighten_permissions(self):
        """Best-effort: force an existing state file to owner-only (0600).

        save() writes 0600, but a file laid down out of band - a manual
        copy/restore, a backup tool - can be group/other-readable, and save()
        only corrects perms when it next mutates the file, which may be never if
        no client ever registers. The file holds client registrations and
        refresh-token hashes, so tighten on load too rather than trusting the
        next write. A chmod failure (not the owner, odd filesystem) is logged,
        not fatal."""
        try:
            mode = os.stat(self.state_path).st_mode
        except OSError:
            return
        if mode & 0o077:
            try:
                os.chmod(self.state_path, 0o600)
            except OSError as exc:
                logging.warning(
                    "MCP OAuth state file %s is group/other accessible and could not be " "tightened to 0600: %s",
                    self.state_path,
                    exc,
                )

    def _load(self):
        """Load existing state; a missing file is a normal first run, and a
        corrupt one is logged and treated as empty (the connector re-registers
        and the user logs in again - annoying, recoverable) rather than
        preventing the whole service from starting."""
        self._tighten_permissions()
        try:
            with open(self.state_path, encoding="utf8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logging.warning(
                "MCP OAuth state file %s could not be read (%s) - starting with empty state; "
                "connected clients will need to re-authenticate",
                self.state_path,
                exc,
            )
            return
        if isinstance(raw, dict):
            clients = raw.get("clients")
            refresh = raw.get("refresh_tokens")
            self.clients = clients if isinstance(clients, dict) else {}
            self.refresh_tokens = refresh if isinstance(refresh, dict) else {}

    def save(self):
        """Write state atomically (temp file + rename, 0600 before content)."""
        payload = json.dumps({"clients": self.clients, "refresh_tokens": self.refresh_tokens}, indent=2)
        tmp_path = f"{self.state_path}.tmp"
        with self._lock:
            try:
                fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                # The 0o600 mode above only applies when os.open *creates* the file
                # (and is further masked by umask); a leftover tmp file from a
                # previous crash is reused with its existing, possibly broader
                # permissions, which os.replace() would then carry onto the real
                # state file. fchmod the actual fd so owner-only is guaranteed
                # regardless of how the tmp file came to exist.
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf8") as f:
                    f.write(payload)
                os.replace(tmp_path, self.state_path)
            except OSError as exc:
                # A failed save degrades to pre-persistence behaviour (state lost on
                # restart) - report it once per call, don't take the server down.
                logging.error(
                    "Could not persist MCP OAuth state to %s: %s - client registrations and "
                    "refresh tokens will not survive a restart",
                    self.state_path,
                    exc,
                )

    def prune_expired_refresh_tokens(self):
        """Drop expired refresh tokens; returns True if anything was removed.

        A malformed entry (non-mapping, or a non-numeric ``expires_at``) counts
        as expired - same recoverable-state contract as the provider's load
        paths, and pruning is the one place every entry gets touched, so a bad
        one must not be able to break token issuance (see _refresh_entry_expired)."""
        now = time.time()
        expired = [key for key, entry in self.refresh_tokens.items() if _refresh_entry_expired(entry, now)]
        for key in expired:
            del self.refresh_tokens[key]
        return bool(expired)


class LoginThrottle:
    """Tracks consecutive login failures per client address and enforces a
    lockout window once the limit is hit. See the constants above for why
    per-address is effectively global behind a reverse proxy - and why that's
    the intended behaviour, not a limitation."""

    def __init__(self, limit=LOGIN_FAILURE_LIMIT, lockout_seconds=LOGIN_LOCKOUT_SECONDS):
        self.limit = limit
        self.lockout_seconds = lockout_seconds
        self._failures = {}

    def locked_out(self, address):
        """Return the remaining lockout seconds for an address (0 if allowed)."""
        entry = self._failures.get(address)
        if not entry:
            return 0
        count, last_failure = entry
        if count < self.limit:
            return 0
        remaining = self.lockout_seconds - (time.time() - last_failure)
        if remaining <= 0:
            del self._failures[address]
            return 0
        return remaining

    def record_failure(self, address):
        """Record a failed attempt; returns the new consecutive-failure count."""
        count = self._failures.get(address, (0, 0.0))[0] + 1
        self._failures[address] = (count, time.time())
        return count

    def record_success(self, address):
        """Clear the failure history for an address after a successful login."""
        self._failures.pop(address, None)


def resolve_state_path(settings, settings_file=None):
    """Return the OAuth state file path: ``mcp.state_file`` if set, otherwise
    ``mcp-oauth-state.json`` next to the settings file (the one location the
    packaged service's sandbox guarantees writable).

    :param settings: parsed settings dictionary
    :type settings: dict
    :param settings_file: the settings path the process was started with, used
        to anchor the default; None means the project-root default
    :type settings_file: str or None
    :rtype: str
    """
    configured = (settings.get("mcp") or {}).get("state_file")
    if isinstance(configured, str) and configured.strip():
        return configured
    base_dir = os.path.abspath(os.path.dirname(__file__) + "/..")
    settings_dir = os.path.dirname(os.path.join(base_dir, settings_file or "settings.yaml"))
    return os.path.join(settings_dir, "mcp-oauth-state.json")


def build_mcp_server(settings, settings_file=None):
    """Construct the FastMCP application for the given settings.

    Everything SDK-related is imported here, not at module level - see the
    module docstring. Raises ConfigError for anything that makes the server
    unbuildable (missing SDK, incoherent settings), which the caller treats as
    fatal-not-retryable, same contract as every other ConfigError.

    :param settings: parsed settings dictionary (validated, post-substitution)
    :type settings: dict
    :param settings_file: settings path, for anchoring the default state file
    :type settings_file: str or None
    :return: a configured FastMCP instance
    :raises ConfigError: if the mcp SDK is unavailable or settings are unusable
    """
    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.requests import Request
        from starlette.responses import HTMLResponse, RedirectResponse, Response
    except ImportError as exc:
        raise ConfigError(
            f"The MCP server is enabled but the 'mcp' package could not be imported ({exc}). "
            "On a source checkout, run: .venv/bin/pip install -r requirements.txt"
        ) from exc

    mcp_settings = settings["mcp"]
    public_url = mcp_settings["public_url"].strip().rstrip("/")
    host, port = parse_mcp_bind_address(mcp_settings.get("bind_address"))
    state_path = resolve_state_path(settings, settings_file)

    provider = SendToInfluxOAuthProvider(
        public_url=public_url,
        expected_user=mcp_settings["user"],
        expected_password=mcp_settings["password"],
        state_store=OAuthStateStore(state_path),
    )

    # netloc is the exact Host header a reverse-proxied request carries (including
    # any explicit port); hostname is the port-less form used for the any-port
    # wildcard entries - conflating the two would produce a malformed
    # "host:port:*" allowlist entry whenever public_url carries a port. hostname
    # also strips the brackets from an IPv6 literal, which Host/Origin syntax
    # requires - re-add them, or the derived entries would be malformed again.
    parsed_public = urlparse(public_url)
    public_host = parsed_public.netloc
    public_hostname = parsed_public.hostname or public_host
    if ":" in public_hostname:
        public_hostname = f"[{public_hostname}]"
    server = FastMCP(
        name="send-to-influx",
        instructions=(
            "Query the current and historical state of the smart-home and energy devices "
            "this send-to-influx installation collects data from."
        ),
        auth_server_provider=provider,
        host=host,
        port=port,
        # The streamable-http endpoint lives at /mcp (the SDK default, made explicit
        # because public_url + this path is what gets configured in Claude).
        streamable_http_path="/mcp",
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=f"{public_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
        # Keep the SDK's DNS-rebinding protection enabled, but allowlist the public
        # hostname: behind the reverse proxy every request arrives with the public
        # Host header, which the SDK's localhost-only default would reject.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host, f"{public_hostname}:*", "127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                f"https://{public_host}",
                f"https://{public_hostname}",
                "http://127.0.0.1:*",
                "http://localhost:*",
            ],
        ),
    )

    @server.custom_route("/login", methods=["GET"])
    async def login_form(request: Request) -> Response:
        txn_id = request.query_params.get("txn", "")
        if not provider.transaction_valid(txn_id):
            return HTMLResponse("<h1>Login link expired</h1><p>Restart the connection from Claude.</p>", 400)
        return HTMLResponse(_LOGIN_FORM_TEMPLATE.format(txn=html.escape(txn_id), error=""))

    @server.custom_route("/login", methods=["POST"])
    async def login_submit(request: Request) -> Response:
        address = request.client.host if request.client else "unknown"
        remaining = provider.throttle.locked_out(address)
        if remaining:
            logging.warning("MCP login attempt from %s rejected: locked out for %.0fs more", address, remaining)
            return HTMLResponse(
                "<h1>Too many failed attempts</h1><p>Try again later.</p>",
                429,
                headers={"Retry-After": str(int(remaining) + 1)},
            )
        form = await request.form()
        txn_id = str(form.get("txn", ""))
        if not provider.transaction_valid(txn_id):
            return HTMLResponse("<h1>Login link expired</h1><p>Restart the connection from Claude.</p>", 400)
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        if provider.check_credentials(username, password):
            provider.throttle.record_success(address)
            redirect_url = provider.complete_authorization(txn_id, subject=username)
            return RedirectResponse(redirect_url, status_code=302)
        failures = provider.throttle.record_failure(address)
        # The submitted username is attacker-controlled, so it stays out of the
        # WARNING line (log-injection/noise, and it's a would-be credential):
        # address + failure count are what matter for alerting on an attack. The
        # username goes to DEBUG via %r, which escapes control characters, for
        # the rare case an operator is debugging their own failed login.
        logging.warning(
            "Failed MCP login attempt from %s (consecutive failure %d of %d before lockout)",
            address,
            failures,
            LOGIN_FAILURE_LIMIT,
        )
        logging.debug("Failed MCP login username: %r", username)
        error_html = '<p class="error">Wrong username or password.</p>'
        return HTMLResponse(_LOGIN_FORM_TEMPLATE.format(txn=html.escape(txn_id), error=error_html), 401)

    # Read-only tools (list sources / list fields / query history). Registered
    # here so every enabled server exposes them; write tools are added per
    # collector in a later slice.
    from toinflux.mcp_read import register_read_tools

    register_read_tools(server, settings, settings_file)

    return server


class SendToInfluxOAuthProvider:
    """OAuthAuthorizationServerProvider implementation for the single-user
    resource-owner model: one configured user/password, clients registered
    dynamically by Claude, tokens issued by this process.

    The SDK's token endpoint performs PKCE verification, expiry checks, and
    client/redirect binding itself - this class only stores, loads, and issues.
    """

    def __init__(self, public_url, expected_user, expected_password, state_store):
        """
        :param public_url: external https URL (no trailing slash) the login page
            and redirects are built against
        :type public_url: str
        :param expected_user: the configured mcp.user
        :type expected_user: str
        :param expected_password: the configured mcp.password
        :type expected_password: str
        :param state_store: persistence for clients and refresh tokens
        :type state_store: OAuthStateStore
        """
        self.public_url = public_url
        self._expected_user = expected_user
        self._expected_password = expected_password
        self.state = state_store
        self.throttle = LoginThrottle()
        # Short-lived, in-memory only: login transactions, auth codes, access tokens.
        self._transactions = {}
        self._auth_codes = {}
        self._access_tokens = {}

    # -- login-page support (called from the custom routes) --

    def check_credentials(self, username, password):
        """Constant-time comparison of both halves of the login."""
        user_ok = hmac.compare_digest(username.encode("utf8"), self._expected_user.encode("utf8"))
        password_ok = hmac.compare_digest(password.encode("utf8"), self._expected_password.encode("utf8"))
        return user_ok and password_ok

    def transaction_valid(self, txn_id):
        """Return True if a login transaction exists and hasn't expired."""
        entry = self._transactions.get(txn_id)
        if not entry:
            return False
        if time.time() - entry["created_at"] > LOGIN_TXN_TTL_SECONDS:
            del self._transactions[txn_id]
            return False
        return True

    def complete_authorization(self, txn_id, subject):
        """Consume a login transaction after successful authentication: mint the
        authorization code and return the client redirect URL carrying it.

        :param txn_id: the (validated) transaction id from the login form
        :type txn_id: str
        :param subject: the authenticated username, propagated to issued tokens
        :type subject: str
        :rtype: str
        """
        from mcp.server.auth.provider import AuthorizationCode, construct_redirect_uri

        entry = self._transactions.pop(txn_id)
        params = entry["params"]
        code = secrets.token_urlsafe(32)
        self._prune_auth_codes()
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            client_id=entry["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=subject,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    # -- OAuthAuthorizationServerProvider protocol --

    async def get_client(self, client_id):
        """Look up a dynamically-registered client in the persisted store.

        A malformed persisted entry (hand-edited file, an older/newer schema) is
        dropped and treated as an unknown client rather than raised - the state
        file's whole contract is that bad state is recoverable (the connector
        just re-registers), never something that breaks requests."""
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import ValidationError

        raw = self.state.clients.get(client_id)
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except ValidationError as exc:
            logging.warning(
                "Dropping malformed MCP OAuth client entry '%s' from %s: %s",
                client_id,
                self.state.state_path,
                exc,
            )
            del self.state.clients[client_id]
            self.state.save()
            return None

    async def register_client(self, client_info):
        """Persist a dynamic client registration (RFC 7591, initiated by Claude)."""
        self.state.clients[client_info.client_id] = client_info.model_dump(mode="json")
        self.state.save()
        logging.info("MCP OAuth client registered: %s (%s)", client_info.client_id, client_info.client_name)

    async def authorize(self, client, params):
        """Start the resource-owner login: stash the request as a single-use
        transaction and send the browser to the login page."""
        txn_id = secrets.token_urlsafe(32)
        self._prune_transactions()
        self._transactions[txn_id] = {
            "client_id": client.client_id,
            "params": params,
            "created_at": time.time(),
        }
        return f"{self.public_url}/login?txn={txn_id}"

    async def load_authorization_code(self, client, authorization_code):
        """Return a stored auth code; the SDK checks client binding and expiry."""
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(self, client, authorization_code):
        """Single-use exchange of an auth code for a fresh token pair."""
        self._auth_codes.pop(authorization_code.code, None)
        return self._issue_tokens(client, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(self, client, refresh_token):
        """Look a refresh token up by hash in the persisted store.

        Same recoverable-state contract as get_client(): a malformed entry is
        dropped and treated as an invalid token (the client falls back to a full
        re-authorization), never raised into the token endpoint."""
        from mcp.server.auth.provider import RefreshToken

        token_hash = _hash_token(refresh_token)
        entry = self.state.refresh_tokens.get(token_hash)
        if entry is None:
            return None
        if not isinstance(entry, dict) or not entry.get("client_id"):
            logging.warning("Dropping malformed MCP OAuth refresh-token entry from %s", self.state.state_path)
            del self.state.refresh_tokens[token_hash]
            self.state.save()
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=entry["client_id"],
            scopes=entry.get("scopes", []),
            expires_at=entry.get("expires_at"),
            subject=entry.get("subject"),
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        """Rotate: revoke the presented refresh token, issue a fresh pair."""
        self.state.refresh_tokens.pop(_hash_token(refresh_token.token), None)
        return self._issue_tokens(client, scopes or refresh_token.scopes, refresh_token.subject)

    async def load_access_token(self, token):
        """Return a live in-memory access token, dropping it if expired."""
        access = self._access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at and access.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return access

    async def revoke_token(self, token):
        """Revoke either token type; unknown tokens are a silent no-op per spec.

        The token string is read via getattr with the value itself as fallback,
        so a raw-string token (or any future representation without a .token
        attribute) revokes cleanly instead of raising AttributeError into the
        revocation endpoint."""
        from mcp.server.auth.provider import RefreshToken

        token_str = getattr(token, "token", token)
        if isinstance(token, RefreshToken):
            if self.state.refresh_tokens.pop(_hash_token(token_str), None) is not None:
                self.state.save()
        else:
            self._access_tokens.pop(token_str, None)

    # -- internals --

    def _issue_tokens(self, client, scopes, subject):
        """Mint an access token (memory) and refresh token (persisted as a hash)."""
        from mcp.server.auth.provider import AccessToken
        from mcp.shared.auth import OAuthToken

        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(48)
        now = time.time()
        self._prune_access_tokens()
        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL_SECONDS),
            subject=subject,
        )
        self.state.prune_expired_refresh_tokens()
        self.state.refresh_tokens[_hash_token(refresh_token)] = {
            "client_id": client.client_id,
            "scopes": scopes,
            "expires_at": int(now + REFRESH_TOKEN_TTL_SECONDS),
            "subject": subject,
        }
        self.state.save()
        return OAuthToken(
            access_token=access_token,
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh_token,
        )

    def _prune_transactions(self):
        """Drop expired login transactions (bounds the dict against drive-by
        /authorize requests that never complete a login)."""
        cutoff = time.time() - LOGIN_TXN_TTL_SECONDS
        expired = [key for key, entry in self._transactions.items() if entry["created_at"] < cutoff]
        for key in expired:
            del self._transactions[key]

    def _prune_access_tokens(self):
        """Drop expired access tokens so the in-memory dict stays bounded."""
        now = time.time()
        expired = [key for key, tok in self._access_tokens.items() if tok.expires_at and tok.expires_at < now]
        for key in expired:
            del self._access_tokens[key]

    def _prune_auth_codes(self):
        """Drop expired authorization codes so the in-memory dict stays bounded.

        The SDK enforces code expiry at exchange time regardless; this only stops
        a client that repeatedly completes /login but never calls /token from
        growing the dict without bound. Called when a new code is minted."""
        now = time.time()
        expired = [key for key, auth_code in self._auth_codes.items() if auth_code.expires_at < now]
        for key in expired:
            del self._auth_codes[key]


def start_mcp_server_thread(settings, settings_file=None):
    """Start the MCP server in a daemon thread and return the thread.

    Config-shaped failures (ConfigError from build_mcp_server) are logged as
    critical and not retried - matching the collectors' contract - but they do
    not take the collection process down: the collectors keep running. Anything
    else (a bind failure, a crash inside the SDK) is logged and retried after a
    flat delay, since the server exiting means nobody can query it.

    :param settings: parsed settings dictionary (validated)
    :type settings: dict
    :param settings_file: settings path, threaded through for the state file default
    :type settings_file: str or None
    :rtype: threading.Thread
    """
    host, port = parse_mcp_bind_address((settings.get("mcp") or {}).get("bind_address"))

    def server_worker():
        while True:
            try:
                server = build_mcp_server(settings, settings_file)
                logging.info(
                    "MCP server listening on %s:%s (public URL %s)",
                    host,
                    port,
                    settings["mcp"]["public_url"],
                )
                server.run(transport="streamable-http")
                logging.error("MCP server exited unexpectedly; restarting in %ss", SERVER_RESTART_SECONDS)
            except ConfigError as exc:
                logging.critical("MCP server cannot start and will not be retried: %s", exc)
                return
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.error("MCP server failed: %s. Restarting in %ss.", exc, SERVER_RESTART_SECONDS)
            time.sleep(SERVER_RESTART_SECONDS)

    thread = threading.Thread(target=server_worker, name="mcp-server", daemon=True)
    thread.start()
    return thread
