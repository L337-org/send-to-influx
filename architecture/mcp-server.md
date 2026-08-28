<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# MCP server internals

Deep detail behind the MCP server summary in [../AGENTS.md](../AGENTS.md). Read this before
changing anything under `toinflux/mcp*.py` or `toinflux/mcpserver.py`.

## MCP server (`toinflux/mcpserver.py`)

The optional remote MCP server (introduced in 5.0) is *not* a `DataHandler` - it's the project's
first inbound-network-facing component, a Streamable-HTTP server built on the official `mcp` SDK's
`MCPServer` (called `FastMCP` before the SDK's 2.0 rename) + built-in OAuth 2.1 authorization
server, run in its own daemon thread (`anyio` inside
the thread; nothing else in the synchronous codebase changes). Enabled iff both `mcp.user` and
`mcp.password` are set (credentials-present is the primary enablement mechanism; one without the
other is a `ConfigError` - see `mcp_block_errors()`/`mcp_enabled()` in `toinflux/general.py`) -
*unless* `mcp.disabled: true` is set, which forces the server off and skips the user/password
coherence check entirely. That override exists because blanking the YAML fields alone can't reach
a coherent disabled state once the password has been migrated to systemd-creds (the stored
credential still gets substituted in at load time regardless of what settings.yaml says), and it
doubles as a quick, credential-independent kill switch for isolating the server during
troubleshooting. Started from `sendtoinflux.py`'s `maybe_start_mcp_server()` (skipped in
`--print`/`--dump` modes). Key
decisions:

- **Bind vs public**: binds `mcp.bind_address` (default `127.0.0.1:8420`) in plain HTTP;
  `validate_settings()` refuses the any-interface wildcards (`0.0.0.0`/`::`) *and* any
  globally-routable public IP literal outright with no override (loopback and private/LAN addresses
  are allowed, since the reverse proxy may run on another host; a non-IP hostname can't be
  classified without a DNS lookup so is allowed with a WARNING) - plain-HTTP OAuth on a public
  interface is never valid, TLS termination belongs to the user's reverse proxy. The
  external HTTPS address is `mcp.public_url` (required when enabled, must be `https://`): the
  OAuth issuer/discovery metadata and login-page redirects are built from it, never from the bind
  address. The SDK's DNS-rebinding protection stays enabled with the public hostname allowlisted
  (a reverse-proxied request carries the public Host header, which the SDK's localhost-only
  default would reject).
- **OAuth storage** (`SendToInfluxOAuthProvider` + `OAuthStateStore`): dynamic client
  registrations and refresh tokens persist across restarts in `mcp.state_file` (default
  `mcp-oauth-state.json` next to settings.yaml - the one path the packaged service's sandbox
  guarantees writable), written atomically at 0600; refresh tokens stored as SHA-256 hashes only.
  postinst restarts the service on every upgrade, so in-memory-only state would break the Claude
  connector on every unattended upgrade. Access tokens are in-memory (1 h TTL) - a restart
  invalidates them and the client recovers silently via refresh. The SDK's token endpoint does
  PKCE/expiry/client-binding verification itself; the provider only stores, loads, and issues.
- **Login page** (`/login`, via `MCPServer.custom_route`): resource-owner step gated on
  `mcp.user`/`mcp.password` (constant-time comparison), single-use unguessable transaction ids
  minted by `authorize()`. Failed attempts are throttled per client address
  (`LoginThrottle`: 5 failures -> 300 s lockout, WARNING-logged) - behind a reverse proxy every
  request carries the proxy's address, so the lockout is effectively global, which is the intended
  behaviour for a single-user login page, not a limitation.
- **Transport options are per-run, not per-server** (`app_options()`/`run_options()` in
  `toinflux/mcpserver.py`): mcp 1.x took `host`/`port`/`streamable_http_path`/`transport_security`
  on the server constructor, so every app derived from a built server inherited the DNS-rebinding
  allowlist. 2.x takes them on `run()`/`streamable_http_app()` instead, which means a call site
  that forgets `transport_security=` silently gets the SDK's localhost-only default and rejects
  every reverse-proxied request. Hence one canonical builder that every call site (including the
  tests) derives from, plus `TestTransportOptions` asserting the two builders stay in step and
  that every key they emit is actually accepted by the SDK's signatures - a renamed SDK keyword
  would otherwise only surface as a `TypeError` at service start, not in CI.
- `mcp-password` is in `CREDENTIAL_FIELDS` like every other secret; its `PLACEHOLDER_VALUES`
  entry is deliberately the empty string (empty-means-disabled is the block's enablement
  mechanism, and `--remove` reverting to `""` is exactly the disabled state).
- The `mcp` SDK is imported only inside `toinflux/mcpserver.py` (lazily, gated on `mcp_enabled()`),
  like `paho-mqtt` in the MQTT transport - but unlike paho it is **not** pure Python: its chain
  needs `pydantic_core`, `rpds-py`, `cffi` and `cryptography` (compiled, no fallback), which is what
  the packaging section's compiled-wheel matrix exists for. `rpds-py` is held to `~=0.30.0` in
  requirements.txt (2026.x CalVer releases dropped Python 3.10 wheels); the build fails loudly if
  coverage regresses.
**Write tools** (`toinflux/mcp_write.py`, `register_write_tools()`): the MCP server is read-only by
default. A source becomes controllable only when it's both `MCP_WRITABLE` (a class flag - Hue and
Speedtest today) *and* the operator opts in with `<source>.mcp_read_write: true`
(`DataHandler.mcp_write_enabled()`, strict `is True`; `validate_settings()` rejects a non-bool so a
mistyped `"true"` fails loud instead of silently staying off). Design points:
  - **Least privilege**: when no source is write-enabled, `register_write_tools()` registers *nothing*
    - no write tool appears on the server's advertised surface at all, not present-and-refusing. So
      the capability can't be probed or bypassed when it's off.
  - **Per-collector, not generic**: writes are heterogeneous (a Hue light takes on/brightness/colour,
    a Speedtest run takes nothing), so each writable source gets its own bespoke, well-described
    tool(s), wired by a per-source registrar in `_WRITE_TOOL_REGISTRARS` (keyed by source name) and
    gated per source; a write-enabled source with no registrar is logged and skipped, not a crash.
    The vendor logic lives on the source class (like the read domain knowledge); `mcp_write.py` only
    wires it up and owns the per-call handler lifecycle. (This supersedes the earlier single generic
    `set_device_state`; a later PID-actuation feature reuses the shared `mcp_common` plumbing, not one tool.)
  - **Hue** (`toinflux/philipshue.py`): tools `hue_list_devices` + `hue_set_light`.
    `mcp_set_device_state()` resolves the target against the live device list
    (`mcp_list_writable_devices()`, the write allowlist which also reports each light's capabilities -
    an unknown or ambiguous name is refused, never guessed, since actuating the wrong light isn't
    recoverable), and is **capability-aware per capability**: brightness, colour temperature and
    colour are independent (a Hue install spans white-only / colour-temperature / full-RGB tiers), and
    asking a light for one it lacks is a `ToolParamError` naming the device, not a silent no-op.
    Brightness 0-100% maps to the bridge's 1-254 `bri` (0% is min-on, not off - off is `on=False`);
    `color_temp_k` (kelvin) converts to `ct` mireds and clamps to the light's reported range; `color`
    (an `#rrggbb` hex or a known name) converts to CIE `xy` (`ct`/`xy` are mutually exclusive - asking
    for both is rejected). Setting brightness/temperature/colour auto-adds `on=True` (the bridge
    ignores them on an off light) unless `on` is explicitly false. `PUT`s to
    `/api/{user}/lights/{id}/state` over the collector's own session/auth and `hue.insecure` TLS
    policy; the CLIP API returns 200 with a per-key success/error list, so a bridge-reported error is
    surfaced as `SourceConnectionError`.
  - **Multi-bridge Hue reads**: `get_current_state` reports each bridge separately under
    `instances`, keyed by bridge host, because two bridges can carry the same field name (a "Kitchen" per
    floor) and one flat map would silently lose one of them. Keyed whenever the source is instanced - even with
    a single bridge - so nothing reading the payload depends on the bridge count; a single-*target* source keeps
    the historical flat `fields`/`as_of` shape untouched. One failing bridge gets an `error` entry while the
    others still report `fields`, a partial answer *with* its failure status; only when every bridge fails is
    `SourceConnectionError` raised, since then there is nothing useful to return. **Scoping a history read runs through the shared
    instance mechanism, not a Hue-specific path**. Hue sets `MCP_INSTANCE_TAG = "host"`, so
    `query_history`'s `instance` scopes to one bridge exactly as it scopes Speedtest to one collecting host -
    the handler is resolved *unscoped* and the filter applied at the query, rather than the older route of
    `resolve_schema(instance=...)` adding the tag through `Hue.mcp_tag_filters()`. That override still exists
    and is still load-bearing (it is what forces `Hue.bridge()` to resolve, so `resolve_handler` refuses an
    unconfigured bridge), but the read tools no longer depend on it for scoping - one concept, one
    implementation.
    - **`bridge` was removed outright rather than deprecated**, and the reasoning generalises to any
      model-facing parameter. The compatibility rule about accepting a renamed key for a release exists for
      interfaces whose *caller* persists across an upgrade - a settings key, a library signature, an emitted
      metric name. An MCP tool schema is the opposite: the client fetches it with `tools/list` at session
      start, so after an upgrade the next session already uses the new name and there is no stored caller to
      break. An alias would therefore have cost context on every session - which a tool description is
      explicitly a budget for - to cover a window shorter than one conversation. It was never a documented
      parameter in the README either, so no user was following it. **Do not add a deprecation window to a
      tool parameter by reflex; ask first whether the caller persists.**
    - **An unscoped Hue query now reports per bridge rather than merging.** A deliberate reversal of the
      earlier span-everything default: two bridges can each hold a "Kitchen", so a merged series is a wrong
      answer, not an estate-wide one.
    - **The instance allowlist is the union of the values present in the data and the targets currently
      configured.** Discovered values alone would refuse a bridge configured but not yet collecting - which
      `bridge` accepted - and would leave `query_history` disagreeing with `get_current_state`, which reads
      live from whatever is configured. Neither half suffices: a decommissioned bridge still has history worth
      querying, a new one has config but no data. `configured_instances()` supplies the configured half via
      `expand_sources()`, the same function the collectors use. The refusal message therefore says *accepted*
      values, never *recorded* - the union includes targets that have recorded nothing, and calling them
      recorded would state something untrue about the very value offered as the alternative.
  - **Multi-bridge Hue**: both write tools cover *every* configured bridge, via
    `resolve_handlers()` in `toinflux/mcp_common.py` - one handler per bridge, built from the same
    `expand_sources()` the collectors use so the MCP surface and the collectors cannot disagree about which
    bridges exist. `hue_list_devices` labels each device with its `bridge` and reports an unreachable bridge
    under `unreachable` rather than silently omitting it (a short list must not read as "no such light" when it
    means "could not ask"). `hue_set_light` gains an optional `bridge`, and `_resolve_hue_target()` does the
    cross-bridge arbitration: a device matching exactly one light across the estate acts without a bridge; a
    device matching several is **refused** with every match named, since light ids repeat on every bridge (so a
    bare id is ambiguous by nature) and names often repeat too, and actuating the wrong light is not
    recoverable. Arbitration lives in `mcp_write.py` rather than on the class because it is about the *tool's*
    parameters - each handler already resolves a device within its own bridge, and this only decides which
    handler to ask. The write opt-in stays per *source* (`hue.mcp_read_write`), not per bridge: one estate,
    one settings block, one switch.
  - **Speedtest** (`toinflux/speedtest.py`): tool `speedtest_run` (no args) via `mcp_trigger_run()` -
    runs a test *on the local host only* (separate hosts run separate processes with no listener, so
    cross-host triggering isn't possible; kept deliberately simple) and records it to InfluxDB
    best-effort (a failed write flags `recorded: false`, not fatal). A class-level `_run_lock` in
    `get_data()` enforces one run at a time per host - shared between the collector worker and the
    trigger, so a triggered run overlapping the scheduled cycle (or another trigger) raises
    `SourceConnectionError` rather than starting a second, mutually-skewing test. Unlike Hue this
    controls no external device; `MCP_LIVE_STATE=False` still keeps the *read* path from ever running
    a test.
  - Per-call handler/session lifecycle and the ToolParamError-vs-SourceConnectionError split are the
    same as the read tools: the shared per-call plumbing (`resolve_handler`, `close_session`,
    `configured_sources`) lives in `toinflux/mcp_common.py`, which every tool module imports from
    rather than from each other. Every applied write is logged at INFO.
  - This is the project's first device-control capability and gets a dedicated `/security-review`
    before the feature branch merges to `main`.

**The OAuth state file lives in the systemd `StateDirectory`, not `/etc`.** This was broken from
5.0 until 5.3: the service runs as `send-to-influx` while `postinst` leaves `/etc/send-to-influx`
root-owned 755, so `save()` could create neither the state file nor the `.tmp` its atomic write
needs beside it. `PermissionError` was logged and persistence degraded to nothing, exactly as the
error says - and because access tokens are in-memory anyway, the only symptom was a connected MCP
client re-authorising after **every** restart, which an operator meets at upgrades. Neither the
unit test suite nor the packaging suite caught it: the former asserted the resolved *path*, the
latter that the server *bound*, and `save()` is deliberately non-fatal so nothing raised.

`resolve_state_path()` now prefers `$STATE_DIRECTORY`, which is the same shape as
`apply_credential_substitution()`'s `$CREDENTIALS_DIRECTORY` - set by systemd only for a unit that
declares the directory, so a source checkout or screen session (equally first-class here) finds it
unset and keeps the historical location beside `settings.yaml`. Colon-separated when several are
declared, so only the first is taken. An explicit `mcp.state_file` still wins.

**The state moved rather than `/etc` being opened up**, deliberately: giving the service user write
access to the directory holding `settings.yaml` and the credential store is the wrong trade, and
the packaging suite now asserts `/etc/send-to-influx` stayed root-owned as well as that the service
user *can* write the state directory - probed as that user, since root could write either way.
`postinst` migrates an existing file across (an install predating the removal of the old
`chown -R` may have a working one); `postrm` removes the directory on purge, because systemd only
does so on `systemctl clean`.

**Packaging** (debconf + systemd): the `mcp:` block is the third shared-infrastructure block after
`influx:` and `mqtt:`, but gated on its own `mcp-enable` boolean (asked at priority `high`, default
no) rather than a source selection - the MCP server is an interface over all sources, not a source.
When enabled, debconf collects `mcp-public-url`/`mcp-user`/`mcp-password`; `bind_address` is a
defaulted tuning field and never prompted. `postinst` back-fills the `mcp:` section with
`--ensure-section` (settings.yaml is never rewritten by an upgrade, so the section is absent on
installs predating this feature) and requires public_url + user + a password (typed or already in
systemd-creds) all present before enabling - a partial `mcp:` block makes `load_settings()` raise a
fatal `ConfigError` that stops **every** collector, not just the server. `public_url` and `user` are
required strings that persist and pre-fill on reconfigure (like `mqtt-broker-host`/`hue-host` - only
the *secret* `mcp-password` is cleared after use, so password-only rotation works: leave the
pre-filled url/user and type just the new password). Because the service is only (re)started at the
very end of `postinst`, only the final settings state matters, so on a failed password store: if no
credential existed yet (a fresh enable) the username is reverted (enable-then-revert -> coherent
disabled block); if one already existed (a reconfigure) the previously-stored password is kept and
the working install is left enabled - never disabled out from under a running server.
`hue.mcp_read_write` stays hand-edited (a tuning toggle, never prompted). The MCP server also made
this the first inbound-network-facing service, so the systemd unit gained a conservative hardening
set (`ProtectKernel*`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, empty
`CapabilityBoundingSet`, `SystemCallFilter=@system-service`, etc. - `MemoryDenyWriteExecute` and a
hand-rolled narrower syscall filter deliberately omitted as Python-fragile); the OAuth state file lives in `StateDirectory=send-to-influx`
(`/var/lib/send-to-influx`, 0700, service-owned) and there is deliberately **no**
`ReadWritePaths` at all - the daemon writes nothing under `/etc`, so `ProtectSystem=strict`
leaves the whole of it read-only to the service. `test-packaging.sh` seeds the MCP
answers in the fresh-install scenario (asserting public_url/user land in settings.yaml, the password
in the credstore and not in plaintext) and, where real systemd is present, asserts the server
actually binds `127.0.0.1:8420` under the full hardened sandbox (the real test that the hardening +
`LoadCredentialEncrypted` don't break the network-facing server).

**Read tools** (`toinflux/mcp_read.py`, registered onto the server by `register_read_tools()`):
six read-only tools - `list_sources`, `list_fields`, `query_history`, `get_current_state`,
`get_data_range`, and `get_documentation` - exposing each configured collector's live and historical
state, domain-aware
rather than a raw passthrough. The read mechanics live in `mcp_read.py`; the per-source domain
knowledge lives on the `DataHandler` subclasses as class attributes (`MCP_MEASUREMENT`,
`MCP_TAG_FILTERS`, `MCP_INSTANCE_TAG`, `MCP_FIELD_METADATA`, plus `MCP_DESCRIPTION` and
`MCP_LIVE_STATE` - see below) so
there's no parallel schema to keep in step - `ReadSchema`/`build_schema()` combine those with a live
field set. Design points:
  - **A tag can be a constant to pin or an axis to enumerate, and the two are different
    attributes.** `MCP_TAG_FILTERS` pins a tag to one value (`device=zappi`) to disambiguate a
    source within a shared measurement. `MCP_INSTANCE_TAG` names the tag that separates
    *producers* within one source's measurement - something to enumerate, scope by, and report
    per value. Only having the first is what made a two-host Speedtest install give wrong
    answers: both hosts' points came back interleaved in one unlabelled series, and
    `aggregation="mean"` averaged across them. Grafana honoured the dimension all along; the MCP
    layer flattened it. Set on `Speedtest` (`host`), and deliberately per source rather than one
    global "collector" tag, because the axis means different things (a collecting host, a bridge,
    a lock, a device) and most sources genuinely have one producer.
    - `discover_tag_values()` is the exact analogue of `discover_measurement_keys()`: `SHOW TAG VALUES`
      gives the live allowlist an `instance` argument is validated against, so a value never
      written is refused rather than answering confidently with nothing. Discovered rather than
      configured, so a host that started reporting yesterday is queryable today with no config
      change. Verified identical on real InfluxDB 1.8 and 2.7's v1-compatibility endpoint -
      worth checking, since that same endpoint reports bucket retention as `0s`.
    - **Payload shape depends on the source, never on how many producers it happens to have.**
      Scoped, or a source with no axis, returns flat `points` exactly as before; unscoped on a
      source with an axis returns `instances` keyed by value - keyed even for a single producer,
      the same reasoning as Hue's per-bridge map. Never merged: two hosts' ping in one unlabelled
      list is a wrong answer, not a partial one.
    - **`LIMIT` is applied per series once a query groups by a tag** (verified: `LIMIT 2` across
      two hosts returned two rows each, not two in total). Left alone, N producers would multiply
      `MAX_RESULT_POINTS` and the response cap would stop capping anything - so the limit is
      divided across the known producers and reported as `limit_per_instance`, not `limit`, since
      a caller comparing it with the figure they passed would otherwise be misled.
    - **The axis is not the same thing as `INSTANCED_SOURCES`.** That names a collector *work
      unit* (a Hue bridge with its own credentials and worker) and would reject Speedtest, whose
      hosts are separate processes: the axis exists in the data without the collector having any
      notion of instances. `query_history` therefore carries both `bridge` (Hue's work unit) and
      `instance` (the data axis) until the shared parameter unified them for callers.
    - **The `collector_status` heartbeat takes its extra tags from the source**, via
      `DataHandler.heartbeat_tags()` - an instanced source tags its bridge, `Speedtest` tags the
      collecting machine through `Speedtest.collector_host()`, which is also what its data uses so
      the two cannot drift. Until this existed every Speedtest host wrote
      `collector_status,source=speedtest` and overwrote the others at second precision, so a dead
      collector was indistinguishable from a healthy estate.
  - **History vs current state**: `query_history` answers "when did X change / trends"; the new
    `get_current_state(source)` answers "what is X *now*" ("is the door locked", "which lights are
    on"). For a live source it calls the source's own `get_data()` (a cheap API/MQTT read) and
    decodes it through the *same* `MCP_FIELD_METADATA` as history, so a coded value reads back as its
    label ("locked"), never a bare number. `MCP_LIVE_STATE` (base default `True`) is `False` on
    Speedtest (its `get_data()` runs a full speed test - must never fire on a read) and Octopus (~24 h
    delayed, so no fresher than InfluxDB); for those, current-state reads the latest recorded point
    from InfluxDB (`build_latest_query`/`_latest_recorded`) and the result's `state` field says which
    (`live`/`last_recorded`). `get_documentation()` synthesises a static, InfluxDB-free Markdown
    reference of every source's `MCP_DESCRIPTION` + field units/codes (not a shipped file - it can't
    drift from what the tools expose); `list_sources` now carries each source's `MCP_DESCRIPTION`.
  - **Data range vs retention are two answers, not one** (`get_data_range`, `data_range_result`):
    the oldest/newest points actually present, *and* what InfluxDB is configured to keep. Both are
    reported because they differ - a three-year-old install with 30-day retention holds 30 days of
    data - and neither alone answers "how far back does this go". `build_edge_time_query` gives either
    edge by `ORDER BY time ASC|DESC` and shares `_build_single_point_query` with
    `build_latest_query`, so the injection defence cannot drift between them. It selects `*` rather
    than enumerating fields, unlike `build_latest_query`, because only the `time` column is read:
    the query travels in a GET parameter, and measured against a real InfluxDB a 120-field
    measurement produced a **3.4 KB** query string that way (46 characters now). A measurement grows
    with device count - Nuki prefixes fields per lock - so a wide enough estate would exceed a
    reverse proxy's request-line limit on a read with no need of the width. Tag columns in the
    returned row are harmless when no value is read from it; `build_latest_query` still enumerates,
    because it *does* read values and must exclude them. **Retention is read differently per version, and
    this is the load-bearing part**: v1 uses `SHOW RETENTION POLICIES` over the existing `/query`
    path (preferring the `default` policy, since that is where writes with no explicit policy land),
    but v2 uses the `/api/v2/buckets` management API *even though the same InfluxQL succeeds on v2's
    v1-compatibility endpoint with the same credential*. Verified against InfluxDB 2.7: for a bucket
    with 720h retention and a 24h shard group, that endpoint answers `duration=0s` and
    `shardGroupDuration=168h0m0s` - it reports the virtual DBRP mapping's policy, not the bucket's,
    and `0s` means keep-forever, so it would report unlimited retention for data that expires in 30
    days. No broader credential is needed for the management API: querying a v2 bucket already
    requires `read:buckets` on it, confirmed with a token scoped to read exactly one bucket. A
    retention read that fails degrades to `retention.known: false` with a reason and keeps the range,
    since the range is the primary answer - and it is *reported* rather than omitted, because an
    absent retention key reads as "nothing expires", the same misleading direction as the `0s`.
    Durations are rendered in v1's own `720h0m0s` style on both versions so answers are comparable
    without knowing which version produced them.
  - **Measurements aren't always the source name**: `openmeteo` writes to `weather`, and the three
    MyEnergi devices share the `myenergi` measurement distinguished by a `device` tag - so their
    classes set `MCP_MEASUREMENT`/`MCP_TAG_FILTERS`, or a query for one device would return all
    three. Every other source owns its measurement (`MCP_MEASUREMENT` stays `None` -> source name).
  - **Injection defence, layered** (InfluxQL has no identifier parameter binding): the measurement
    and tags come from the source class's static schema, never model input; a requested field must
    exactly match a key discovered live via `SHOW FIELD KEYS` (the field set *is* the allowlist,
    and it handles collectors with dynamic field names - Hue sensors, per-lock Nuki prefixes);
    every identifier is additionally charset-validated and double-quoted with escaping; time bounds
    are parsed in Python and re-emitted as RFC3339 (the model's raw string never reaches the query);
    aggregation is a fixed name->InfluxQL-function map and any GROUP BY interval matches a duration
    grammar. Result size is capped (`MAX_RESULT_POINTS`).
  - **A single query path serves v1 and v2**, mirroring `_build_write_request()`'s branch: `GET
    /query` with a `Token` header (v2) or HTTP basic auth (v1), `epoch=s`. v2's v1-compatibility
    `/query` endpoint needs no extra provisioning in the default case (virtual DBRP mappings keyed
    by bucket name since InfluxDB 2.9) - verified against real v1 and v2 containers.
  - **`SHOW FIELD KEYS` is per-measurement, not per-tag**, so for the three shared-measurement
    MyEnergi devices `list_fields` shows the others' fields too; a query for a cross-device field
    is safe and returns no points (the tag filter excludes it). Documented accepted limitation.
  - **`MCP_FIELD_METADATA`** maps a field key - or a `_`-delimited suffix, for dynamically-prefixed
    fields like Nuki's `Front_Door_stateValue` - to any of `unit`, `codes` (`{int: str}`), `kind` and
    `description`; `annotate_rows()` attaches units and decodes coded values to labels (an
    undocumented code passes through with a null label, matching the collector's raw-passthrough
    rule). Sourced from UNITS.md.
  - **`list_fields` answers the whole question a dashboard asks, in one call**: `database` (previously
    only reachable through `get_data_range`, which also does retention work, so a query cost a second
    call for one short string), `measurement`, `tag_keys` (every dimension to group by - a MyEnergi
    `device` or a Nuki lock was in the data and in no payload unless the source happened to declare it
    as its instance axis), and per field its `type`, `unit`, `codes` and `kind`. A key is omitted
    rather than nulled, so "no unit" and "unit unknown" are told apart the only honest way there is.
  - **`kind` is the one field-level fact that cannot be recovered from the value**, which is why it is
    declared rather than derived: taking the mean of a cumulative counter produces a plausible chart
    that means nothing, and no unit, type or coded value distinguishes those fields from an
    instantaneous reading. Four values (`FIELD_KINDS`): `gauge`, `interval`, `counter`, `state`.
    - **A numeric field with nothing declared reports no kind at all, deliberately.** Defaulting to
      `gauge` would say "averaging this is fine" about a counter, which is the exact failure the
      field exists to prevent; saying nothing is recoverable where saying that is not. A *string* or
      *boolean* field does get `state` from its InfluxDB type without being declared, which is what
      makes an untabulatable field key answerable at all - Hue's field keys are the operator's own
      device names, so no static table can cover them.
  - **Hue's fields are described from a companion measurement, not from a static table.** Its field
    keys are the operator's own device names, so nothing declared in advance can say that
    `Conservatory_Temperature_Sensor` is a temperature in °C. The bridge reports each device's type
    on every poll and the collector used to discard it; it now writes it to `hue_devices` (one point
    per device, tagged `host`/`device`, one string field `class`), and `mcp_field_metadata()` reads
    it back. `HUE_DEVICE_CLASSES` maps a class to a unit and a kind.
    - **Only the varying fact is written.** Which class a device is goes to InfluxDB; the class ->
      unit/kind mapping stays declared in `philipshue.py`, because writing the unit into every point
      would duplicate the table and give it somewhere to drift. `documented_as` names the UNITS.md
      row each class corresponds to, which is what lets Hue join the metadata drift test after all -
      that file's Hue table is keyed by *device class*, which is precisely why Hue was excluded
      before.
    - **Three alternatives were rejected, all for the same reason: they answer differently on
      different runs.** An in-process cache is empty until the first poll and after every restart. A
      git-excluded state file survives restarts but goes stale - swap a plug for a dimmable bulb
      under the same name and it reports the old unit until something refreshes it - and is private
      to this process. Reading the bridge from the read tools would make a schema listing depend on a
      device being awake, so the same field would have a unit on one call and none on the next; it
      would also let a model generate device traffic by calling `list_fields`, and cross the line
      `MCP_LIVE_STATE` exists to draw, since every other schema tool touches InfluxDB and nothing
      else. Writing it to InfluxDB adds no dependency the schema path did not already have, is
      rewritten every cycle so it self-corrects, and is visible to everything else reading the
      database rather than only to us.
    - **It is a separate measurement, so it is transparent to existing queries** - a query names its
      measurement, so nothing selecting from `hue` can see it. Same pattern and same fire-and-forget
      write as the `collector_status` heartbeat, and it expires under the same retention as the data
      it describes.
    - **The description can never fail a collection.** The readings are written first and their
      `InfluxWriteError` contract is untouched; the description follows, and a failure is logged and
      swallowed. In the other direction `mcp_field_metadata()` degrades to the static table if
      InfluxDB cannot be read, so a live current-state read never fails because an annotation could
      not be resolved.
    - **`build_documentation` deliberately does not use the hook.** The generated reference promises
      no InfluxDB round trip - `get_documentation`'s own description says so - so it keeps reading
      the class attribute, and a source with only per-install metadata is absent from it. That is
      the honest trade: `list_fields` is where those fields are described. It was wired to the hook
      by mistake once, which broke the promise with nothing failing, so
      `test_the_reference_uses_the_class_attribute_and_never_the_hook` now asserts the hook is not
      called at all.
    - **A field key is not unique across bridges**, so the lookup groups by `host` as well as
      `device`: two bridges with a device of the same name write the *same* field key under
      different host tags. Where they are the same class it is described once; where they disagree
      it is described not at all, because no unit is correct for a key that means two things and the
      data cannot separate them either. Grouping by `device` alone would let InfluxDB merge the
      bridges and `last()` pick a winner silently, so the query text is asserted.
  - **`description` sits behind `detail=False`, and is the only optional part** because it is the only
    bulky one. Every other addition is a handful of bytes and always present. It exists to decode an
    unobvious key (MyEnergi's `ectp1`, `che`) or to carry semantics the name cannot - cumulative,
    forecast-not-actual, accumulated-over-an-interval. **A description that restates the name is
    worse than none**, costing context on every detailed call and conveying nothing, so
    self-describing fields (`temperature_2m`, `download`) have none; `tests/test_field_metadata.py`
    fails on one whose words are all derivable from its field key.
  - **Field keys and tag keys come from one request, not two** (`discover_measurement_keys()`,
    replacing `discover_fields()`): InfluxQL takes semicolon-separated statements and answers with a
    result per statement, so `SHOW TAG KEYS` costs no round trip on a call already making one.
    Verified on real 1.8 and 2.7's v1-compatibility endpoint - identical responses, `statement_id`
    present on both (position is the fallback), and `fieldType` giving `float`/`integer`/`string`/
    `boolean`. **The per-statement error check is load-bearing here in a way it was not before**: an
    unusable database answers with statement 0 carrying `"error": "not executed"` and *no result at
    all* for the statements after it, so a caller ignoring the error would read the missing statement
    as "this measurement has no tags" rather than as a failure.
  - **`tests/test_field_metadata.py` is the coverage ratchet**, replacing prose asking someone to
    remember: every declared entry must say something (a unit, coded values *or* a description - not
    all three, since a flag, a label and a status code genuinely have no unit and demanding one
    invites a made-up unit) and must declare a valid `kind`; and UNITS.md must agree about every unit
    string and every coded value. **The UNITS.md check is deliberately one-way** - metadata implies a
    UNITS.md row, never the reverse - because that file legitimately documents things that are not
    field keys: Hue's rows are by device class, and carbon intensity's `gen_<fuel>` is a pattern. It
    compares units and coded values only, **never prose**: the MCP `description` and the Notes column
    serve different readers (a model choosing a field, versus a maintainer reading caveats and
    disagreements with vendor documentation), so neither is derived from the other and comparing them
    would force them to converge on whichever reader was served worse.
    - Two real defects it caught on its first run, neither of which any existing test could see:
      UNITS.md gave Speedtest's unit as "bits per second" where the metadata says `bits/s` (the cell
      now leads with the symbol and keeps the words as a note), and the checker's own code-table
      parser read past `stateValue`'s table into `doorsensorStateValue`'s, so every code the two
      share was reported as a disagreement that did not exist.
  - **The four MyEnergi day/hour fields are hourly, not daily, and UNITS.md said otherwise.** Found
    while writing their descriptions: `get_data()` calls `dayhour_results(..., now.hour)`, and the
    matching-hour branch *assigns* that hour's value and breaks rather than accumulating, so
    `Charge`/`Import`/`Export`/`Genera` hold the current hour's energy and reset on the hour. Where
    the current hour's entry is absent from the response - MyEnergi omits all-zero entries, so this
    happened at any hour the API had no entry for - the loop fell through to summing every bucket and
    the value silently became the day so far. Fixed: the hour is selected and totalled, and an hour
    with no entry reads 0. UNITS.md's "Daily totals" was corrected, and the hourly reset is what the
    descriptions state, since it is the fact that governs how to aggregate them either way. The
    meaning flipping between hourly and daily is a separate defect, not fixed here.
  - Blocking InfluxDB HTTP runs in a worker thread (`anyio.to_thread.run_sync`) so a query doesn't
    stall the server's async event loop. `ToolParamError` (a bad field/time/aggregation/device -
    shared by the read and write tools, defined in `toinflux/exceptions.py`; a non-retryable
    caller/model mistake) surfaces to the model as a tool error; `SourceConnectionError` is a
    transient transport failure the collector loop would retry, so the two are kept distinct.

**Dashboard panels** (`toinflux/mcp_dashboards.py`, `register_dashboard_tools()`): one tool,
`suggest_dashboard_panels`, turning a source's schema into what a Grafana panel needs - per field an
InfluxQL query, a panel type, the aggregation to use, the aggregations to avoid, a Grafana unit,
value mappings decoding a coded field, and a series alias.

- **A tool rather than a prompt, decided deliberately.** A prompt would have cost no permanent
  surface, but it is not in the model's tool list, so nothing would tell a model this data can be
  charted - which was the entire problem. Clients also vary in whether they surface prompts at all.
  And a tool is the only form that can be *tested*: "never take the mean of a counter" is a hope in
  prose and an assertion in CI. The cross-server workflow prose lives in README.md instead, where a
  human reads it and it costs no context.
- **Its own module, and that boundary is the design.** Every Grafana-specific fact this project
  knows lives here: the panel type names, `GRAFANA_UNITS`, the value-mapping shape.
  `MCP_FIELD_METADATA` and `list_fields` stay vendor-neutral, which was scoped as a deliberate
  exclusion - encoding another product's vocabulary into the schema would undo the separation the
  schema depends on. `mcp_read` does not import this module, so the leak is structurally impossible
  rather than merely avoided.
- **It emits panel parts, never dashboard JSON.** Measured against a real Grafana 13.2, a saved
  dashboard came back *verbatim* with nothing added and **no `schemaVersion` at all**
  (`meta.apiVersion` is `v0alpha1`), where older Grafana carries that field and more defaults. A
  template for a product we do not control and cannot assert against in CI would rot silently, so
  the caller assembles the envelope, copying one of its own dashboards for the shape.
- **`build_panel_query()` lives in `mcp_read.py`, not here**, so the injection defence cannot drift:
  measurement, field and tag keys go through the same charset validation, quoting and live-allowlist
  check as every other query. It is deliberately *not* a flag on `build_query()` - that one resolves
  concrete RFC3339 bounds and applies a LIMIT because it executes here, where a panel query carries
  `$timeFilter` and `time($__interval)` and no LIMIT, which would fight the panel's `maxDataPoints`.
- **`avoid_aggregations` is the load-bearing field**, not `aggregation`. A caller composing its own
  query needs to recognise the mistake, not just copy the suggestion - and the mistake is invisible:
  the mean of a resetting counter is a plausible line with no referent.
- **`interval` exists because one vocabulary could not serve two facts.** `gauge` warns against
  `sum`, which is right for a temperature or a power in watts and *wrong* for a quantity accumulated
  over a reporting period: Octopus's `consumption_kwh` and `gas_consumption` are the energy used
  during one interval and Open-Meteo's `precipitation` is what fell during one, so summing them is
  not merely allowed - it is how a daily total is obtained. Those three were declared gauges, so the
  tool steered callers away from the correct aggregation, and **only a suppressed review comment
  surfaced it**. Dropping `sum` from gauge's list fixed those three and made the warning useless for
  every real gauge; keeping it kept the wrong advice. Neither statement is true of one class, so the
  class was split. `gauge` now warns against `sum` soundly, and `interval` recommends it.
  - **The interval's duration is deliberately not in the schema.** A sum is correct whatever it is,
    so the aggregation guidance does not need it; it is observable anyway, since Octopus stamps each
    point at its own `interval_start` and consecutive timestamps are therefore spaced by it; and it
    is not uniformly knowable - gas granularity follows the meter type, the same dependence its unit
    has, and Open-Meteo's is the model's own (900 s observed live, an hour in the documented hourly
    series) which this project discards. A field populated for one of three cases would be worse
    than none.
  - Two tests hold the split in place: `test_an_interval_quantity_is_a_kind_of_its_own` fails if one
    of the three moves back to `gauge`, and `test_no_declared_gauge_is_really_an_interval_quantity`
    fails if any *other* field describes itself as per-interval while calling itself a gauge - which
    is how the contradiction arose the first time. It exempts a description saying "average", since
    an average over an interval is still a reading and summing averages means nothing.
  - **That second test is a keyword heuristic, not a proof**: it matches "during one",
    "accumulated" and "preceding interval". A per-interval field phrased another way ("per
    half-hour") passes it while still being wrong, so the word list is a floor to extend when a new
    phrasing appears, not a guarantee the contradiction cannot recur.
- **An undeclared numeric field gets no `kind` and is suggested `last`**, the one aggregation that
  cannot be wrong for any kind. Suggesting `mean` would say averaging is safe about a field that may
  be a counter, which is the failure the whole feature exists to prevent.
- **A tag already pinned by `MCP_TAG_FILTERS` is not grouped by** (`series_tags()`): a Zappi pins
  `device` to its own label, so grouping by it would add a series dimension with one member. Hue's
  `host` and Nuki's `device` are real axes and are grouped.
- **Every panel is aliased, including single-series ones.** Without an alias Grafana names the series
  after the query - a Zappi energy panel came back as `myenergi.last` on a real Grafana - so a
  grouped panel gets `$tag_<key>` and an ungrouped one the bare field name (a literal alias, verified
  to pass through unchanged). Two or more tags is the space-joined form and is **untested**: no source
  ships more than one unpinned tag, so it is documented as such rather than left looking verified.
- **An unmappable unit is omitted, not passed through** - though the reason first recorded here was
  wrong, so the decision is open rather than settled. Grafana accepts any string as a unit id
  server-side, and `getValueFormat()` in grafana-data's `valueFormats.ts` falls through to
  `toFixedUnit(id)` for an id with no recognised `key:` prefix, which returns
  `{text, suffix: " " + id}`. So a bare `W/m²` renders as `123 W/m²` - correctly - rather than as
  nothing. Their own test pins the explicit form: `suffix:d` on 1532.82 gives `1533 d`. Emitting the
  four unmapped units as `suffix:` forms would therefore work; `suffix:` rather than a bare string,
  because a bare one would silently adopt Grafana's formatter (possibly one that rescales) if a real
  id of that name were ever added.
  - The belief this replaces was that an unknown id renders as *no* unit, so a raw `W/m²` would look
    configured and do nothing. That was never checked against Grafana's source, only against a
    grep of its minified bundle that came back inconclusive - and inconclusive was recorded as
    settled. `GRAFANA_UNITS` still covers only ids read out of a running Grafana's own bundle, and
    W/m², gCO2/kWh, pence/kWh and "kWh or m³" still get no `unit` key, but now because nobody has
    decided to add them rather than because it would not work.
- **Everything Grafana-side was established by execution**, against Grafana 13.2.0 and InfluxDB 1.8:
  the target field names (`query`/`rawQuery`/`resultFormat`/`alias`), that `$timeFilter` and
  `$__interval` both resolve and return data, the value-mapping shape, and every unit id. The panels
  this tool suggests were assembled into a real dashboard, saved, and re-executed through
  `/api/ds/query` to confirm each returns rows.

**Resources** (`toinflux/mcp_resources.py`, `register_resources()`): the addressable/listable view of
the same read data - the design rule is *anything exposed as a resource is also a tool* (MCP clients
use resources in limited ways, so the tools stay the workhorses). Three kinds, all built from the
read builders in `mcp_read.py` so there's no second implementation: `docs://reference` (the
`get_documentation` Markdown), and per configured source `schema://<source>` (its `list_fields`
payload) and `state://<source>` (its `get_current_state` payload). Per-source resources are
registered *concretely* (one per source, via a factory so each closure binds its own source name),
not as one URI template, so a client's `resources/list` enumerates each source's snapshot and schema
directly. The current-state builder (`current_state_result`) and `list_fields` payload builder
(`list_fields_result`) are public in `mcp_read` for exactly this reason - the resources module imports
them, rather than reaching into privates.

**Prompts** (`toinflux/mcp_prompts.py`, `register_prompts()`): parameterised task templates the user
invokes from the client - they add no capability, only orient the model on how to combine the tools
for the tasks this server is for. Three, kept generic (a free-text focus/question/request, never
hard-coded devices): `home_status` (summarise current state, optional focus area), `usage_trends`
(historical analysis/cross-source comparison), and `control_device` (the check-state->act flow).
`control_device` is registered *only* when a source has writes enabled (`writable_enabled_sources`,
the same gate as the write tools) - a read-only install offers no control prompt, so nothing
advertises a capability that isn't there. `home_status`/`usage_trends` are always registered.
`build_mcp_server()` computes the write-enabled list *once* and passes it into both
`register_prompts()` and `register_write_tools()` (each accepts an `enabled_sources=` override, and
falls back to computing it when called standalone), so the per-source handler construction that
computation costs isn't done twice at startup.

**The advertised surface** - every tool, prompt and resource description, plus their titles - is
held to the AI-consumer standard rather than to ordinary documentation standards, because a model
reads it to choose what to call and every byte is paid for on each session that loads it.
`tests/test_mcp_surface.py` is the guard for the prose half, spanning all four registration modules
(the per-module tests already fail on a missing title or safety hint). It enforces: a description
and a distinct title on everything registered; a `SIBLINGS` table that must name every registered
tool, so a new tool fails until someone has decided which neighbours it must be told apart from;
that no description names a tool which does not exist (a rename otherwise leaves an authoritative
pointer to nothing); that every tool says how it fails and whether it changes anything; and a
recorded byte budget. Line wrapping is normalised away before matching - a docstring keeps its
newlines, so `changes nothing` split across a break would fail a guard the description satisfies.

Measured with that module's fixture (two sources, both write-enabled: nine tools, three prompts,
five resources), the surface went from **10,162 bytes to 13,296** in the prose pass - tools 9,937 -> 11,252,
prompts 225 -> 523, resources 0 -> 1,521. The growth is where the surface was *silent* rather than
merely terse:

- The three resources carried no description at all from 5.0 to 5.3, so a client enumerating
  `resources/list` saw a URI and a name and nothing about what it held, what reading it cost, or
  which tool covered the same data.
- `get_current_state` and `get_data_range` documented neither the per-producer `instances` grouping
  they return nor the partial-failure reporting - both shipped in earlier releases without their
  descriptions following.
- `list_sources` claimed to be "the only one needing no arguments", which `get_documentation`
  disproves; the false claim was itself the sibling-discrimination failure.
- `hue_list_devices` named only `hue_set_light`, so nothing ruled out the reading that it lists
  devices for every collector - the question that raised this work. It now names
  `get_current_state`, the source-agnostic tool a caller would otherwise be reaching for.

`query_history`, the largest single description, *shrank* by 301 bytes: its behaviour kept, the
justifications for that behaviour dropped. A caller needs to know what a tool does, not why it was
designed that way - the reasoning belongs here, where it is loaded by a person, not on every
session.

**Every tool registers through `register_tool()` in `toinflux/mcp_common.py`, and that
exists for a version trap the budget guard caught on its first CI run.** CPython 3.13
strips a docstring's leading indentation at compile time; 3.10-3.12 do not, and the SDK
advertises `fn.__doc__` verbatim (`func_doc = description or fn.__doc__ or ""`). So on the
older half of the supported range - which the `.deb` explicitly allows, `Depends: python3
(>= 3.10)` - every continuation line of every tool description reached the model carrying
eight leading spaces: **14,569 bytes advertised on 3.12 against 13,297 on 3.14**, the
1,272-byte difference being pure whitespace, paid for on every session and completely
invisible to anyone developing on 3.13+. `register_tool()` passes
`description=inspect.cleandoc(fn.__doc__)`, so every supported version advertises the same
bytes. Two guards, because one cannot do it: the *effect* is asserted (no advertised
description carries an indented line), which is real on 3.10-3.12 and trivially true on
3.13+; and the *source* is asserted (`@server.tool(` appears in neither registration
module), which is the only check that fails on the machine where the mistake is made.
Verified by simulation as well as by CI - re-indenting each 3.14 description the way
3.10-3.12 present it and pushing it back through `cleandoc` reproduces `query_history` at
exactly the 2,209 bytes CI reported, and returns all nine to byte-identical.

**Two things deliberately left out**, both being context that buys nothing. A *registration*
precondition ("requires `hue.mcp_read_write: true`") is guaranteed true whenever the model can see
the tool at all, since a disabled capability is not registered - it was drafted into all three write
tools and then removed. And a `title` on a *prompt* stays a short display name with the model-facing
instructions in the returned message, not in the advertised list.

**What the published criteria actually require, checked rather than recalled** (2026-08-21): the
authority on the fields is the MCP specification, revision **2026-07-28**, whose data types make
`title`, `description` and `annotations` all *optional* on Tool, and `title`/`description` optional
on Prompt and Resource - so nothing but a test stops a registration shipping without them. It also
tells clients they **MUST** treat tool annotations as untrusted unless the server is trusted, which
is the reason preconditions, side effects and error behaviour go in prose and not only in
`readOnlyHint`/`destructiveHint`. The official registry at `registry.modelcontextprotocol.io` turns
out to impose **nothing** on per-tool description prose: its moderation policy is explicitly
permissive ("we only remove illegal content, malware, spam, and completely broken servers", and it
"does not make guarantees about moderation"), removing low-quality servers only where they are spam
- for which one named example is "a description stuffed with marketing copy". Its one hard
description rule is on the *server-level* `description` in `server.json`: `minLength` 1,
`maxLength` **100**, "should focus on capabilities, not implementation details". This project
publishes no `server.json` and is not registry-listed, so that limit does not currently bind - worth
knowing before ever listing it, since the constraint is far tighter than a tool description's. The
real per-tool review is done by downstream directories, not the official registry.
