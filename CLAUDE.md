# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Contributor-facing project structure and conventions live in [CONTRIBUTING.md](CONTRIBUTING.md) (this
file is the deeper architecture reference); see also [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
[SECURITY.md](SECURITY.md), and [PRIVACY.md](PRIVACY.md).

## Commands

All Python tooling must use the repo-local virtual environment (`.venv`), not system Python.

```bash
# Setup
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Tests
.venv/bin/pytest -v                  # all tests
.venv/bin/pytest -v tests/test_hue.py::TestClass::test_name  # single test

# Lint / format
.venv/bin/flake8                     # max line length 120, complexity 10
.venv/bin/black .                    # auto-format
.venv/bin/mypy toinflux sendtoinflux.py   # static type check (permissive, see pyproject.toml)
```

CI runs `pytest` (with coverage, matrixed across Python 3.10-3.14), `flake8`, `mypy`, and `arm64-verify`
(builds and smoke-tests the `.deb` on a real `ubuntu-24.04-arm` runner, see "Packaging" below) in
parallel on every push to `main` and every PR (`.github/workflows/premerge.yaml`) - all are required
status checks on `main`'s ruleset, so a failure blocks merging rather than only being noticed
afterward. Dependency and GitHub Actions updates are managed by Dependabot
(`.github/dependabot.yml`), weekly.

## Architecture

**send-to-influx** collects metrics from smart home / energy devices and writes them to InfluxDB using the [line protocol](https://docs.influxdata.com/influxdb/v1/write_protocols/line_protocol_tutorial/). Both InfluxDB v1 (user/password) and v2 (token/org/bucket) are supported.

### Class hierarchy

```
DataHandler      (toinflux/influx.py)          — base; owns send_data() → InfluxDB HTTP POST
├── CarbonIntensity(toinflux/carbonintensity.py)
├── Hue            (toinflux/philipshue.py)
├── OpenMeteo      (toinflux/openmeteo.py)
├── Octopus        (toinflux/octopus.py)
├── Speedtest      (toinflux/speedtest.py)
└── MyEnergi       (toinflux/myenergi.py)     — intermediate parent for MyEnergi API auth
    ├── Zappi      (toinflux/myenergi.py)
    ├── Eddi       (toinflux/myenergi.py)
    └── Harvi      (toinflux/myenergi.py)
```

Each subclass implements `get_data()` which populates `self.data` (dict) and `self.influx_header` (InfluxDB measurement/tag string); `send_data()` in the base class takes it from there. Points are written with an explicit unix-epoch-seconds timestamp: `self.timestamp` if `get_data()` set it (e.g. Octopus uses the reading's own `interval_start` so re-writes of the same reading overwrite rather than duplicate), otherwise the time `send_data()` is called. Field keys are escaped per line protocol rules (commas, `=`, spaces).

Speedtest's `get_data()` additionally rejects an implausible `ping` (>= 5000 ms) as a `SourceConnectionError` rather than writing it. speedtest-cli's `get_best_server()` times each of the 3 latency probes it makes per candidate server with a hardcoded 10-second connection timeout (baked into `SpeedtestHTTPConnection`/`SpeedtestHTTPSConnection`'s constructor default - never overridden by `get_best_server()`, so it applies regardless of the `timeout` passed to `speedtest.Speedtest()`); a probe that doesn't complete within that raises `socket.timeout`, which is caught alongside every other connection failure and penalised with a hardcoded `3600` (seconds) instead of a real sample. The 3 per-server samples (real or penalty) are summed, divided by a fixed 6, and converted to milliseconds - so a real (non-penalised) probe can never contribute more than 10s to that sum, making `(3 * 10 / 6) * 1000 = 5000` ms the true ceiling for a genuine measurement. If every probe to a server fails (observed in practice during a transient network blip), the reported `ping` comes out around 1,800,000 ms instead of triggering an error, and would otherwise be written to InfluxDB as if it were real.

### Entry point (`sendtoinflux.py`)

- **Single-source mode** (`--source <name>`): continuous loop, fixed interval per source. Connection failures (`SourceConnectionError`) are retried with exponential backoff (base 5 s, max 300 s); a `ConfigError` is not retried — it exits the process immediately with code 1.
- **Multi-source mode** (no `--source`): reads `sources` list from `settings.yaml`, spawns one daemon thread per source with a configurable startup stagger (`stagger_seconds`, default 10). Dead threads are detected and restarted with the same exponential backoff — unless the source's worker stopped because of a `ConfigError`, in which case it is logged and left stopped (other sources keep running).
- `--dump`: one-time raw JSON to stdout, then exit (single source only).
- `--print`: parsed data to stdout instead of InfluxDB.
- `--settings <path>`: use a settings file at a path other than `settings.yaml` in the project root (e.g. `/etc/send-to-influx/settings.yaml` for a packaged install). Threaded through `toinflux.get_class()`/`load_settings()`.
- `--version`: print `__version__` and exit; parsed before settings are loaded, so it works without a `settings.yaml` present.
- `--check-config`: load and validate the settings file (via `load_settings()`), print `Configuration OK`, exit 0. Exits 1 with details if invalid (same validation as a normal run). If `--source` is also given, that source's block is validated too even if it isn't in `sources`/`default_source` (`validate_settings(settings, source=...)`), so checking config for a one-off `--source` can't report a false "OK".
- `-v`/`--verbose`: force `DEBUG`-level logging, overriding the `loglevel` settings.yaml key.
- Handles SIGINT and SIGTERM for graceful shutdown.
- On startup, logs an INFO line with the version and the source(s) that will run, so process (re)starts are visible in the logs.
- CLI arguments are parsed *before* `load_settings()` is called, so `--version`/`--help` don't require a config file to exist.
- After every collection cycle, `maybe_send_heartbeat()` writes a `collector_status,source=<name>` point (fields `ok`, `consecutive_failures`) via `send_heartbeat()`, which reuses the source's own `DataHandler.send_data()` with a swapped-in header. Skipped in `--print` mode.

### Exceptions (`toinflux/exceptions.py`)

- `ConfigError`: a fatal, non-retryable problem (missing/invalid settings, unknown source name). Raised by `toinflux/general.py` (`load_settings()`, `get_class()`, `configure_logging()` if `logfile` can't be opened for writing - e.g. an unwritable path under the packaged systemd service's sandboxing) and `DataHandler.__init__()`.
- `SourceConnectionError`: a transient problem talking to a source's API (network error, bad auth, bad response). Raised from each handler's `get_data()`/API-call code and retried with backoff by the worker loop.

### Factory / settings

- `toinflux/general.py`: `load_settings(settings_file=None)` (raises `ConfigError` on missing/invalid YAML; `settings_file` defaults to `settings.yaml` in the project root when omitted), `get_class(source, settings_file=None)` (case-insensitive factory → correct DataHandler subclass; raises `ConfigError` for an unknown source, including `DataHandler` itself — it's the abstract base, not a selectable source; threads `settings_file` through to the handler's `load_settings()` call), `flatten_dict()` (used by Speedtest to flatten nested JSON), `configure_logging(logfile=None, loglevel="INFO", log_max_bytes=..., log_backup_count=...)` (sets up timestamped stdout logging, plus an optional `RotatingFileHandler`; raises `ConfigError` instead of a raw `OSError` if `logfile` can't be opened, e.g. a permissions problem).
- `configure_logging()` is called via `_configure_logging_or_exit()` in `main()` after settings are loaded and `--check-config` has short-circuited - this catches that `ConfigError`, logs it (the stdout handler is already attached by the time it's raised, so this still reaches the journal under systemd as a normal formatted line, not a traceback), and exits 1. Log messages use the format `YYYY-MM-DD HH:MM:SS LEVEL message`. Effective log level is `-v`/`--verbose` (forces `DEBUG`) > `loglevel` settings.yaml key > `INFO` default.
- Config file: `settings.yaml` (copy from `example_settings.yaml`), or a custom path via `--settings`. Required at runtime; not committed. Optional `logfile` key adds a rotating file log destination (`log_max_bytes`/`log_backup_count` settings keys control rotation, defaulting to 10 MiB / 3 backups). Some fields can optionally be sourced from `systemd-creds` instead on the packaged install - see "Credential storage (`systemd-creds`)" below; an environment-variable secret-override mechanism was considered and deliberately rejected instead - see "Rejected: environment-variable secrets" below.

### Adding a new data source

1. Create `toinflux/newsource.py` — class inheriting `DataHandler`, implement `get_data()`.
2. Register it in `get_class()` in `toinflux/general.py` and add it to `toinflux/__init__.py`.
3. Add a section to `example_settings.yaml`.
4. Add tests in `tests/test_newsource.py`, reusing fixtures from `tests/conftest.py`.
5. Update README.md, CLAUDE.md, and `.github/copilot-instructions.md`.

### Testing conventions

- Mock `load_settings`, HTTP calls, and file I/O so tests run without real config or network.
- Shared fixtures (e.g. `sample_settings`) live in `tests/conftest.py`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Normal exit |
| 1 | Configuration error (`ConfigError`: missing/invalid settings, unknown source) |
| 2 | Connection error (`SourceConnectionError`) in `--dump` mode only - there's no worker loop to retry a one-shot dump with backoff. In continuous mode (single- or multi-source), connection errors are always retried with backoff instead of exiting. |

### Packaging (`packaging/`)

- `pyproject.toml` is the single source of truth for the package version (`[project].version`) and runtime dependencies (dynamically read from `requirements.txt`). Bump the version there, not in `sendtoinflux.py`.
- `sendtoinflux.py`'s `__version__` is read from installed package metadata (`importlib.metadata.version("send-to-influx")`), falling back to `"0.0.0-dev"` when run from a source checkout without the package installed. `requirements-dev.txt` includes `-e .` so dev/test environments have it installed and see the real version.
- `packaging/deb/build-deb.sh` builds a `.deb` that bundles the app + dependencies into a venv under `/opt/send-to-influx`, with a systemd unit (`packaging/send-to-influx.service` - kept at the top level of `packaging/` since it's format-agnostic; a future `.rpm` would ship the identical unit file) and maintainer scripts (`packaging/deb/postinst`/`prerm`/`postrm`). Package is `Architecture: all`: the venv's own interpreter is a symlink to the system-provided `/usr/bin/python3` (declared as a `Depends: python3 (>= 3.10), python3 (<< 3.31)`, not bundled), and any optional compiled accelerators pulled in by pip (e.g. PyYAML's `_yaml`, charset-normalizer's `md`/`cd`) are stripped post-install in favour of their pure-Python fallbacks - see the comments at the top of `build-deb.sh`. A venv's `site-packages` normally lives under `lib/pythonX.Y/` (named after the exact interpreter that created it), which would otherwise tie the package to whichever Python the *build host* happened to have; since everything left after the accelerator-stripping is pure Python, the script instead symlinks every minor from 3.10 through 3.30's `lib/pythonX.Y` to the one actually populated (both bounds come from one `PYTHON_MAX_SUPPORTED_MINOR` variable, so `Depends:` and the symlink range can't drift apart), so the package installs correctly on any target with a matching `python3`, regardless of which minor in that range. (An earlier version pinned `Depends:` to the exact build-time minor instead - that broke in practice the first time the target's Python drifted out of sync with whatever GitHub's CI runner image shipped.) `.github/workflows/premerge.yaml`'s `arm64-verify` job builds and smoke-tests the same script on an `ubuntu-24.04-arm` runner on every push/PR (a required status check), to catch a future dependency change that makes a compiled extension load-bearing rather than optional before it can merge. See the README's "Running as a systemd service" section.
- `.github/workflows/release.yaml`: pushing a bare `MAJOR.MINOR` tag (e.g. `3.0` - matching this project's existing tags/releases, no `v` prefix) runs the test suite, verifies the tag matches `pyproject.toml`'s version exactly, builds the `.deb`, and attaches it to a GitHub Release. A second job publishes it to a flat APT repo on the `gh-pages` branch (served via GitHub Pages) - it prunes to the last `KEEP_LAST_N` (currently 5) `.deb` files, full history stays in Releases.
  - The APT repo job needs a one-time setup and is skipped (not failed) until it exists: generate a GPG key (`gpg --batch --gen-key`), add the private key as the `APT_GPG_PRIVATE_KEY` repo secret (`gpg --export-secret-keys --armor <key-id> | base64`), and the public key ends up published as `send-to-influx.gpg` in the repo automatically on first successful run.
  - `gh-pages`'s ruleset requires signed commits (see "Branch protection" below), so the job also imports a *second*, separate `CI_COMMIT_SIGNING_KEY` and signs the publish commit with it - kept apart from `APT_GPG_PRIVATE_KEY` since the two have different trust domains (end users trust the APT key to verify packages; GitHub verifies this one against the maintainer's account) and different rotation needs. The commit is authored with the maintainer's real email (not the usual `github-actions[bot]` noreply address), since GitHub's "verified" signature check requires the commit email to match a verified email on the account the key is registered to.

### Rejected: environment-variable secrets

An earlier version of this project let `INFLUX_TOKEN`/`INFLUX_PASSWORD` environment variables
(sourced from an optional `/etc/send-to-influx/environment`, via the systemd unit's
`EnvironmentFile=`) override the corresponding `settings.yaml` values, intended to let a packaged
install keep secrets out of the settings file. Removed after review concluded it added no real
security value:

- Both files end up owned by the same service user with the same permissions - there is no actual
  security *boundary* between "secrets in settings.yaml" and "secrets in an env file," only an
  organizational one. Splitting secrets into a separate file that is equally (or, in practice, worse
  - see below) protected is security theatre, not a mitigation.
- The environment file was never created or permission-locked by `postinst` - a user following the
  documented advice (`sudo nano /etc/send-to-influx/environment`) would create it with whatever
  default permissions their editor/umask gave it, likely world-readable, ending up *less* secure than
  leaving the secret in the already-`chmod 600` settings file. A real implementation would need
  `postinst` to pre-create and lock down that file, but even then:
- Environment variables add a genuinely distinct exposure path a plain file doesn't have -
  `/proc/<pid>/environ` and (if ever enabled) core dumps both capture them - without removing any
  existing one, since `settings.yaml` still needs to stay locked down regardless (not every field is
  a candidate for env-var override, and the override is opt-in/unverifiable).
- The one semi-plausible benefit (a locked-down settings file is safer to attach to a bug report)
  doesn't hold up: since moving a secret to the env file is optional and unenforced, you can never
  trust that a given user's `settings.yaml` has no secrets in it, so the advice would always have to
  be "redact before sharing" regardless of whether this feature exists.

`systemd`'s `LoadCredential=`/`systemd-creds encrypt` is now implemented for exactly this reason - it
creates a *real* boundary (TPM-bound or host-key encryption at rest, credentials materialized only in
a restricted tmpfs for the service's lifetime) rather than an organizational one. See "Credential
storage (`systemd-creds`)" below. It only helps the packaged systemd install, same as this rejected
approach would have - the plain screen-session/source-checkout path this project treats as equally
first-class is unaffected either way, since `$CREDENTIALS_DIRECTORY` is simply unset there.

### Credential storage (`systemd-creds`)

For the packaged `.deb`/systemd install, secrets can optionally be moved out of `settings.yaml` and
into `systemd-creds` - a real security boundary (TPM-bound or host-key-derived encryption at rest,
decrypted only into a restricted tmpfs for the service's lifetime), unlike the rejected env-var
mechanism above. This is opt-in: the plain-YAML path is unaffected and remains equally first-class for
the source-checkout/screen-session path, where `systemd-creds` doesn't apply at all.

- `toinflux/credentials.py`: `CREDENTIAL_FIELDS` is the single source of truth mapping a systemd-creds
  credential name (e.g. `influx-token`) to the `(top-level key, field)` it overlays in the parsed
  settings dict (e.g. `("influx", "token")`) - 6 credentials across `influx` (`token`, `user`,
  `password`), `hue` (`user`), `myenergi` (`apikey`), `octopus` (`api_key`). `PLACEHOLDER_VALUES`
  matches `example_settings.yaml`'s literal placeholder text per field; `sentinel_for(name)` returns
  the cosmetic string written into `settings.yaml` once a field is migrated (never read back for real
  use - purely informational for a human reading the file). `apply_credential_substitution(settings)`
  overlays whatever's decrypted into `$CREDENTIALS_DIRECTORY` (set by systemd when the unit's
  `LoadCredentialEncrypted=` directives are active) into the settings dict - a no-op when that env var
  is unset, which is what keeps the source-checkout path and any not-yet-migrated packaged install
  byte-for-byte unaffected.
- `toinflux/general.py`'s `load_settings()` calls, in order, right after `yaml.safe_load()` and before
  any other logic touches the parsed dict: `_enforce_settings_file_permissions()` (against an explicit
  `copy.deepcopy()` snapshot of the raw, pre-substitution dict - not dependent on being called before
  `apply_credential_substitution()`, which mutates its input in place), `apply_credential_substitution()`,
  then `_clear_unsubstituted_credential_sentinels()` (blanks any of the 6 fields still holding sentinel
  text after substitution - e.g. `settings.yaml` was migrated but the matching `.cred` file wasn't
  found - so a decoy string can't pass `validate_settings()`'s truthiness checks as if it were real,
  which would otherwise let the daemon start "successfully" and then fail auth forever as a retried
  `SourceConnectionError` instead of failing fast as the `ConfigError` it actually is).
- `_enforce_settings_file_permissions()` is content-aware, not purely mode-based: it only warns/refuses
  when `settings.yaml` is group/other-readable *and* actually contains a real credential (not just a
  placeholder or sentinel) - this is what makes `postinst`'s fresh-install default of `644` (not `600`)
  safe, since a freshly-packaged file never contains a real secret unless a human hand-edits one in.
  Controlled by the `enforce_permissions` settings.yaml key (default `false` when the key is absent, so
  every pre-existing `settings.yaml` keeps working with just a warning; `example_settings.yaml` ships
  `true` explicitly, so new installs enforce by default) - `true` additionally raises `ConfigError`
  instead of just warning.
- `toinflux/credential_cli.py` (`send-to-influx-set-credential`, a second `pyproject.toml` entry point):
  `<name>` encrypts a secret (read from stdin if piped, else an interactive masked prompt) via
  `systemd-creds encrypt`, writes it to `/etc/send-to-influx/credstore.encrypted/<name>.cred`,
  regenerates a systemd drop-in (`/etc/systemd/system/send-to-influx.service.d/50-credentials.conf`,
  rebuilt from a fresh directory listing on every call - idempotent, self-healing, no separate state
  file) with the matching `LoadCredentialEncrypted=` line, and rewrites the corresponding
  `settings.yaml` field to the sentinel text via a `yaml.compose()`-based surgical edit (preserves every
  other byte of the file - comments, ordering - rather than a full load+dump round trip, and refuses
  rather than corrupts if the target isn't a plain single-line scalar). `--remove` reverses this, in a
  specific order for two independent reasons: it rewrites `settings.yaml` back to the placeholder
  *first*, before touching anything else - if that fails (the same "not a plain single-line scalar"
  refusal), nothing else has happened yet, so the credential is still fully intact rather than ending
  up deleted from `systemd-creds` while `settings.yaml` still holds the now-orphaned sentinel (which a
  later `load_settings()` would blank out via `_clear_unsubstituted_credential_sentinels()` and then
  fail `validate_settings()` on - a broken, unrecoverable service for what should have been a clean,
  reversible failure). Only then does it regenerate the drop-in (dropping the credential's line)
  *before* deleting the `.cred` file, never after, since `LoadCredentialEncrypted=NAME:PATH`
  referencing a missing `PATH` hard-fails unit startup with `243/CREDENTIALS` (confirmed via systemd's
  own issue tracker: systemd/systemd#35077, #32667) - the drop-in must never be left pointing at a file
  that's already gone, even transiently.
  `--list` shows configured/not-set per credential. `--set-field`/`--detect-influx-version`/
  `--ensure-influx-storage` support the debconf-driven install flow (below).
- The base `packaging/send-to-influx.service` unit ships zero `LoadCredentialEncrypted=` directives -
  all credential wiring lives purely in the drop-in the CLI manages, so a fresh install that's never
  run the script is byte-for-byte identical to before this feature existed.
- `systemd-creds` availability is checked at runtime (`systemd-creds --version`, must be >= 250, the
  version that introduced it), not via a package-wide `Depends:` floor - Ubuntu 22.04/jammy ships
  systemd 249, one version short, and is otherwise a currently-supported platform (unlike Debian
  11/bullseye, already excluded by the existing `python3 (>= 3.10)` `Depends:`) - a `Depends:` bump
  would make the whole package uninstallable there just to gate one opt-in feature. A missing/too-old
  `systemd-creds` fails with a specific message rather than blocking install.

#### debconf-driven install

`packaging/deb/send-to-influx.templates` + `packaging/deb/config` (copied into `DEBIAN/templates`/
`DEBIAN/config` by `build-deb.sh`, which also adds `debconf (>= 0.5)` to `Depends:`): `config`'s job
is *only* asking questions and stashing answers in debconf's database - a hard Debian packaging
convention, so `dpkg-reconfigure`/backing out of an install never leaves partial side effects. It
never touches the filesystem and never calls into `credential_cli.py` - that only happens later,
from `postinst`, once package files are unpacked and everything's been answered.

- `send-to-influx/sources-to-configure` (`Type: multiselect`, priority `high`) is asked first and
  gates everything else - if nothing's selected, `config` exits immediately, and per-source blocks
  are only shown (via `db_input` called conditionally, not declaratively in the template file) for
  sources actually picked, so choosing one or two sources doesn't walk through prompts for the other
  six. Tuning fields (`interval`, `timeout`, `fields` lists, `stagger_seconds`/`default_source`) are
  never prompted for - see the "Template structure" reasoning in the original plan for why (`fields`
  particularly can't be validated against a source's real field names at install time). The one
  deliberate exception is `hue-temperature-units`, which gets a *computed* default (checks
  `$LC_ALL`/`$LANG` for a `_US` territory code, defaulting to Celsius otherwise) via `db_set` before
  the first `db_input`, rather than a silent guess - getting temperature units wrong is immediately
  visible to the user in a way the other tuning fields aren't.
- InfluxDB's `influx-url`/`influx-identity`/`influx-secret` are asked unconditionally (every selected
  source needs a working InfluxDB connection) - `identity`/`secret` are generic (org+token for v2,
  user+password for v1), asked without knowing which version applies yet. Version detection
  deliberately does **not** happen in `config`: `config` runs *before* the package is unpacked on a
  first install, so it can't rely on the app's own venv/`requests`, and more fundamentally, gating
  *what gets asked* on being able to reach an arbitrary, possibly-remote, possibly-not-yet-provisioned
  URL at the exact moment of package install would defeat the point of the URL being configurable.
  Detection happens later, in `postinst`, via `send-to-influx-set-credential --detect-influx-version`.
  This always skips TLS verification, unconditionally - unlike `--ensure-influx-storage` (which
  respects `influx.insecure`), it never transmits a credential (both `/health` and `/ping` are
  unauthenticated probes) and its result only picks which prompt fields get routed to, not a trust
  decision a MITM'd response could meaningfully downgrade; `influx.insecure` also isn't necessarily
  known yet at this point, since debconf never asks for it (only ever hand-edited into settings.yaml
  afterwards).
- `postinst` (gated on `sources-to-configure` being non-empty, so a plain non-interactive install
  behaves exactly like the non-debconf flow above): resolves the InfluxDB block first via
  `--detect-influx-version` and routes `identity`/`secret` accordingly - v2 writes `identity` to the
  plain (non-secret) `influx.org` field via `--set-field` and `secret` to the `influx-token` credential;
  v1 routes both `identity` and `secret` to the `influx-user`/`influx-password` credentials - if
  detection comes back `unknown` (URL unreachable at install time, expected to happen sometimes since
  the URL can point anywhere), nothing is written and no source is auto-enabled this run; a secret
  prompt is never pre-filled with a previous answer on a later `dpkg-reconfigure` (see the `Type:
  password` note below), so that re-run cleanly re-collects them rather than silently reusing something
  stale. Then, per selected source: secrets via `send-to-influx-set-credential <name>`, non-secret fields via
  `--set-field`, both reusing the same CLI rather than a second YAML-patcher in shell. **Auto-enable**:
  a source is only added to `sources:` (via `--enable-source` - `default_source:` is never touched,
  since `sendtoinflux.py` only falls back to it when `sources:` is absent entirely, which is never true
  once `example_settings.yaml` has shipped it non-empty) if *every* required field for it (and the
  InfluxDB block) actually resolved - not just "was it ticked" - with `--ensure-influx-storage`
  attempted first (best-effort database/bucket creation, logged-not-raised on failure).
- `Type: password` answers *are* written to disk by debconf - contrary to an earlier version of this
  note - into a dedicated `passwords.dat` store kept separate from its general-purpose, more widely
  readable answer database, and restricted to `chmod 600`. Debian's own developers' guide
  (`debconf-devel(7)`) advises clearing a password value out of it "as soon as is possible" once
  consumed, so `postinst` does: immediately after each `db_get` on a password-type template
  (`influx-secret`, `hue-user`, `myenergi-apikey`, `octopus-api-key`), it calls `db_unregister` on
  that question - this removes the question, and its stored answer, from debconf's database entirely,
  regardless of whether the subsequent `systemd-creds` migration for that value goes on to succeed or
  fail. The template definition itself lives in `send-to-influx.templates`, not in the database entry
  that got removed, so it's re-registered fresh the next time `config`/`postinst` run -
  `dpkg-reconfigure` is unaffected. Separately, and unrelated to whether the value is unregistered,
  debconf *never* redisplays/pre-fills a previous password answer in the prompt on a later
  invocation - a UI convention for this template type. So a reconfigure always shows secret prompts
  blank, with no way for `postinst` to distinguish "leave it as-is" from "clear it" from that alone -
  resolved by not supporting clearing via debconf at all: `postinst` treats blank as "keep the existing
  systemd-creds value," and removing a credential goes through `send-to-influx-set-credential <name>
  --remove` directly instead.

### Branch protection

Four rulesets, in decreasing order of strictness - `release/**/*` and `feature/**/*` mirror the same
tiering pattern used on the maintainer's other repos (e.g. `docker-mcp`), adapted to this repo's own
CI check names:

- `main`: no force-pushes/deletion, PR required (1 approval, code-owner review, resolved review
  threads, squash-merge only), Copilot auto-review, CodeQL code scanning, and every check from
  `premerge.yaml` required ("Run flake8", "Run mypy", "Run pytest (3.10)"-"Run pytest (3.14)", "Verify
  .deb build on arm64").
- `release/**/*`: same PR requirements as `main` (1 approval, code-owner review, resolved threads) but
  merge method widened to squash/merge/rebase, and CodeQL and "Verify .deb build on arm64" dropped from
  the required checks (still run, just not a merge-blocking gate at this tier) - kept for longer-lived
  release-prep branches that don't need the full ceremony of `main` on every push.
- `feature/**/*`: one tier looser again - force-pushes/rebasing allowed (no `non_fast_forward` rule),
  and the PR rule relaxed to 0 required approvals and no code-owner review (changes still go through a
  PR and must have review threads resolved, just without needing anyone's sign-off) - for fast
  iteration on shared topic branches without losing CI coverage entirely.
- `gh-pages`: pushed to directly by `release.yaml`'s `apt-repo` job (no PR - there's no human review
  step for an auto-generated APT repo), so its ruleset only has `non_fast_forward` (no force-pushes/
  history rewrites), `deletion` (branch can't be deleted), and `required_signatures` (every commit
  must have a GitHub-verified signature - see the `CI_COMMIT_SIGNING_KEY` note above). It deliberately
  does *not* require PRs, since that would need a carefully-configured bypass for the workflow's
  direct push and getting that wrong silently breaks every release.

All four use a `RepositoryRole` bypass actor for the repo admin, though `main`'s bypass actor predates the other
three and is scoped to `bypass_mode: "pull_request"` (bypasses the PR requirement only) rather than
`"always"` (bypasses every rule) used on the newer three - not yet reconciled.
