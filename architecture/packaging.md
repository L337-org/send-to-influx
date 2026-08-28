<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Packaging, credentials and the debconf install flow

Deep detail behind the packaging summary in [../AGENTS.md](../AGENTS.md). Read this before
changing anything under `packaging/`, `toinflux/credentials.py` or
`toinflux/credential_cli.py`, or before adding a settings section or a credential.

## Packaging (`packaging/`)

- `pyproject.toml` is the single source of truth for the package version (`[project].version`) and runtime dependencies (dynamically read from `requirements.txt`). Bump the version there, not in `sendtoinflux.py`.
- `sendtoinflux.py`'s `__version__` is read from installed package metadata (`importlib.metadata.version("send-to-influx")`), falling back to `"0.0.0-dev"` when run from a source checkout without the package installed. `requirements-dev.txt` includes `-e .` so dev/test environments have it installed and see the real version.
- `packaging/deb/build-deb.sh` builds a `.deb` that bundles the app + dependencies into a venv under `/opt/send-to-influx`, with a systemd unit (`packaging/send-to-influx.service` - kept at the top level of `packaging/` since it's format-agnostic; a future `.rpm` would ship the identical unit file) and maintainer scripts (`packaging/deb/preinst`/`postinst`/`prerm`/`postrm`). `/etc/send-to-influx/settings.yaml` is deliberately *not* a dpkg conffile: `postinst` and `send-to-influx-set-credential` write debconf answers/sentinels into it, and dpkg's conffile machinery treats any maintainer-script write as a local modification (Debian Policy 10.7.3 forbids the combination) - guaranteeing a "modified (by you or by a script)" prompt on every upgrade that ships a changed example, with a one-keypress path to replacing a configured file with the pristine example. Instead the example ships at `/usr/share/send-to-influx/example_settings.yaml` and `postinst` copies it into place only if `/etc/send-to-influx/settings.yaml` doesn't exist (the Policy 10.7.3 "configuration files handled by maintainer scripts" pattern; `postrm` removes it on purge, as that pattern requires) - so upgrades never touch the live file at all. The Nuki device-tag migration and `UPGRADING.md`
ship into that same directory, so an apt install can run the 5.2->5.3 data migration without
cloning the repo - deliberately not a `pyproject.toml` entry point and asserted *off* `$PATH` by
the scenario suite, since a destructive irreversible one-off must be a deliberate act. It is run
with `/opt/send-to-influx/venv/bin/python3`, not a bare `python3`: it needs `requests`, which the
package bundles rather than declaring as a system dependency. On upgrade (and after a `dpkg-reconfigure` that rewrote configuration), `postinst` restarts the service if - and only if - it's currently running, so unattended upgrades don't leave the replaced code running until the next reboot; a stopped service is never started. Package is `Architecture: all`: the venv's own interpreter is a symlink to the system-provided `/usr/bin/python3` (declared as a `Depends: python3 (>= 3.10), python3 (<< 3.31)`, not bundled), and any optional compiled accelerators pulled in by pip (e.g. PyYAML's `_yaml`, charset-normalizer's `md`/`cd`) are stripped post-install in favour of their pure-Python fallbacks - see the comments at the top of `build-deb.sh`. The exceptions since the MCP server landed are `pydantic_core` and `rpds-py`, plus `cffi` and `cryptography` since the mcp 2.x port (all required by the `mcp` SDK, compiled, no pure-Python fallback - `cryptography` arrives via `pyjwt[crypto]`, which `mcp/server/request_state.py` imports unconditionally, so it is load-bearing rather than optional): the blanket strip removes them too, and a dedicated compiled-wheel-matrix step re-adds them. **These come in two shapes and the difference is load-bearing.** `pydantic_core`, `rpds-py` and `cffi` publish a wheel per CPython minor whose `.so` filename carries both the minor and the architecture (`_cffi_backend.cpython-310-aarch64-linux-gnu.so`), so every variant across the supported minors (3.10-3.14, `COMPILED_WHEEL_MINORS`) x both architectures can be merged into the one shared site-packages and coexist - CPython only imports a `.so` tagged with its own exact ABI. `cryptography` publishes a *stable-ABI* (`cp39-abi3`) wheel instead: one per architecture, serving every minor, whose `.so` name carries **neither** tag - `cryptography/hazmat/bindings/_rust.abi3.so` is identically named in the x86_64 and the aarch64 wheel, so merging both would silently have one overwrite the other. Those (`COMPILED_WHEEL_ABI3_PACKAGES`) are therefore staged side-by-side as `<name>.so.<arch>` with the un-suffixed name deliberately never written, and `postinst` symlinks the variant matching `dpkg --print-architecture` at install time (discovered by pattern, so a future abi3 dependency needs no postinst change; on an architecture with no staged variant nothing is linked, leaving the pure-Python collectors unaffected and the optional MCP server reporting its usual "could not be imported" `ConfigError`). The build fails loudly if any minor/arch combination is missing, or if an abi3 extension ever appears un-suffixed - which (together with the `rpds-py~=0.30.0` hold in requirements.txt) is the guard against a wheel version dropping a supported minor or platform. A venv's `site-packages` normally lives under `lib/pythonX.Y/` (named after the exact interpreter that created it), which would otherwise tie the package to whichever Python the *build host* happened to have; since everything left after the accelerator-stripping is pure Python, the script instead renames it to the version-independent `lib/python3` and `postinst` symlinks every supported minor to it (see the `preinst`/layout bullet below for why the symlinks are created there rather than shipped; both bounds come from `PYTHON_MIN_SUPPORTED_MINOR`/`PYTHON_MAX_SUPPORTED_MINOR`, which also drive `Depends:` and are substituted into `postinst`, so the range can't drift apart), so the package installs correctly on any target with a matching `python3`, regardless of which minor in that range. (An earlier version pinned `Depends:` to the exact build-time minor instead - that broke in practice the first time the target's Python drifted out of sync with whatever GitHub's CI runner image shipped.) `.github/workflows/premerge.yaml`'s `arm64-verify` job builds the same script's output on an `ubuntu-24.04-arm` runner on every push/PR (a required status check) and runs `packaging/deb/test-packaging.sh` against it - catching both a future dependency change that makes a compiled extension load-bearing rather than optional, and any regression in the maintainer-script behaviour below, before it can merge; `bookworm-verify` re-runs the same suite in a `debian:12` container for systemd-252 coverage (the restart scenario self-skips there - no running systemd - but the systemd-creds *tooling* is the real 252 binaries, which is what caught out 4.1). See the README's "After installing" section (under "Using the .deb package").

The package also ships rsyslog and logrotate config (`packaging/deb/send-to-influx.rsyslog`/
`send-to-influx.logrotate`, installed to `/etc/rsyslog.d/49-send-to-influx.conf` and
`/etc/logrotate.d/send-to-influx`) mirroring the real haproxy Debian package's own pattern
(confirmed directly off a live install, not reconstructed from memory) rather than having the app
manage its own dedicated logfile: a rule matching `:programname, isequal, "send-to-influx"`
redirects to `/var/log/send-to-influx.log` and `stop`s further processing, so these messages are
removed from the shared `daemon.log`/`syslog` rather than merely duplicated into a second file -
zero application code changes needed, since journald already forwards stdout to syslog tagged with
the program name. `Recommends: rsyslog, logrotate`, not `Depends:` (the service works via the
journal alone either way - this is a Priority:important, not essential, enhancement layered on
top, consistent with no hard `Depends:` on systemd either). Both config files are real dpkg
conffiles (`DEBIAN/conffiles`, the first use of that mechanism in this package - unlike
`settings.yaml`, no maintainer script ever rewrites either, so the Policy 10.7.3 conflict that
rules out conffile treatment there doesn't apply here). `postinst` best-effort `try-restart`s
rsyslog on every `configure` (not gated on fresh-install, since the config's content can change
between releases) so a new/changed rule takes effect immediately; `postrm` explicitly removes the
runtime-created logfile and its rotated backups on purge, since dpkg only owns the two conffiles
themselves. `test-packaging.sh` asserts both files ship, validates their syntax
(`rsyslogd -N1`/`logrotate -d -f`), and confirms purge removes the log data - both CI jobs install
`rsyslog`/`logrotate` explicitly for this (`Recommends:` isn't pulled in by a bare `dpkg -i`).
- `packaging/deb/preinst` deletes the whole bundled venv (`/opt/send-to-influx/venv`) so the
  unpack that follows lays down a pristine one. The venv is entirely package-owned and recreated by
  every install - no configuration (that's `/etc/send-to-influx`), no credentials (the credstore),
  nothing user-editable - and wiping it removes several failure modes at once: stale modules being
  imported in preference to new ones (a locally-built 4.4 once logged its 4.4 banner while running
  pre-4.3 library code, failing with "unexpected keyword argument 'use_buffer'" and "Source nuki not
  found"), leftover `lib/python<major.minor>/` trees from a package built against a different
  interpreter, and runtime-generated `__pycache__` files that dpkg doesn't own and won't clean up.
  **The safety guard is the `DEBCONF_RECONFIGURE=1` early exit at the top**, and it is essential:
  `dpkg-reconfigure` also runs `preinst`, as `upgrade <version>` - indistinguishable from a real
  upgrade by its arguments alone - but with *no unpack following it*, so anything deleted on that
  path is gone permanently. An earlier version without that guard destroyed the installation on
  every reconfigure. `DEBCONF_RECONFIGURE` is the same flag `postinst` uses to tell the two apart,
  and is verified to be visible in `preinst`.
- Relatedly, `build-deb.sh` names the venv's real site-packages directory `lib/python3` (version
  *independent*), and `postinst` - not the package - creates the `lib/python3.X -> python3` symlinks
  across the supported range, removed again by `postrm`. Both details exist to keep dpkg quiet and
  correct: a version-named real directory would need to swap places with a symlink whenever the
  build interpreter differed from the installed one (which dpkg cannot reliably do), and shipping
  the symlinks in the package would leave them in place during dpkg's post-unpack cleanup, so old
  `lib/python3.<minor>/...` paths would resolve through them into the freshly-unpacked tree and fail
  to `rmdir` - ~166 "unable to delete old directory" warnings on an upgrade where nothing was
  actually wrong. The supported range lives once, as `PYTHON_MIN/MAX_SUPPORTED_MINOR` in
  `build-deb.sh`, which drives `Depends:` and is substituted into `postinst` at build time (the
  build fails if a placeholder survives).
- `packaging/deb/test-packaging.sh` is the scenario suite for the maintainer scripts - shell behaviour pytest can't reach. Against a built `.deb` it asserts, in order: upgrade over the *latest published release* (obsolete-conffile handover, no re-prompt of the old `db_unregister`-era secret, config/credentials preserved; skipped gracefully offline or via `SKIP_RELEASE_UPGRADE=1`); a fresh debconf-seeded install (fields applied, credential migrated, plaintext secret absent from both `settings.yaml` and debconf's own database, ownership/modes, no conffiles, `/opt` root-owned); plain-upgrade silence with an *interactive* frontend over a hand-edited config (no prompts, no warnings, file byte-identical); restart-on-upgrade of a running service (real `MainPID` change - the example config's placeholder values pass validation, workers just retry, so the service stays active without a real InfluxDB; skipped where systemd isn't running, e.g. containers); `dpkg-reconfigure` semantics (answers re-applied, a stored systemd-creds credential satisfies the blank secret prompt, running service restarted); post-upgrade `dpkg-reconfigure` against a release-era `settings.yaml` (the `mqtt:`/`nuki:` sections back-filled by `--ensure-section`, the venv surviving - `preinst` also runs on that path - and the result still passing `--check-config`); incoherent MQTT auth (a username with no password material warns instead of auto-enabling); per-source question visibility at debconf's *default* priority (`high`, via the teletype frontend), including that the conditional `mqtt-*` questions are absent when no MQTT source is selected; and purge (config, credentials, debconf answers, service, and the postinst-created venv symlinks all gone). It is deliberately destructive - CI runners or throwaway containers only, requires root. Every assertion maps to something that regressed, or nearly regressed, during PR #48.
- `.github/workflows/release.yaml`: triggered by a GitHub Release being **published** (`on: release: types: [published]`), *not* by tag pushes - so tags can be created for other purposes without triggering a build. The release process is: draft the release in the UI (tag = bare `MAJOR.MINOR` matching `pyproject.toml`, no `v` prefix; hand-written notes on top of the generated ones) and publish; the workflow then runs the test suite, verifies the release tag matches `pyproject.toml`'s version exactly, builds the `.deb`, and attaches it to the release. Uploads go by **release id straight from the event payload** - the `releases/tags/{tag}` lookup endpoint is never called, after it served 503s for an extended period during the 4.3 release, which `gh release upload <tag>` rendered as a bogus "release not found" that failed the run (the 4.3 `.deb` was rescued by hand-uploading the run's saved artifact via the release-id endpoint - the same path the workflow now uses; the artifact upload exists precisely so that manual rescue is always possible). No in-job retries by design: a failure is reported plainly and the remedy is re-running the failed job. APT publishing moved out on 2026-07-15.
  - The flat APT repo at `https://apt.l337.org/` is owned by [L337-org/apt](https://github.com/L337-org/apt) - an hourly single-writer aggregator that pulls `.deb` assets from L337-org projects' GitHub Releases (this repo is listed in its `repos.yaml`), regenerates and signs the index (`APT_GPG_PRIVATE_KEY`/`CI_COMMIT_SIGNING_KEY` live *there* now, not here), and pushes to its own `gh-pages`. A new release here appears in the APT repo within the hour (or immediately via that repo's *Run workflow* button). This repo's own `gh-pages` branch and publishing secrets are vestigial once this lands and are removed as a post-merge follow-up.
  - `https://gavinlucas.github.io/send-to-influx/` (the pre-org-move URL) serves a frozen 4.2 snapshot from a placeholder repo at the old name, so pre-move installs keep working; the repo has lived in the `L337-org` org since 2026-07-15.

## Credential storage (`systemd-creds`)

For the packaged `.deb`/systemd install, secrets can optionally be moved out of `settings.yaml` and
into `systemd-creds` - a real security boundary (TPM-bound or host-key-derived encryption at rest,
decrypted only into a restricted tmpfs for the service's lifetime), unlike the rejected env-var
mechanism above. This is opt-in: the plain-YAML path is unaffected and remains equally first-class for
the source-checkout/screen-session path, where `systemd-creds` doesn't apply at all.

- `toinflux/credentials.py`: `CREDENTIAL_FIELDS` is the single source of truth mapping a systemd-creds
  credential name (e.g. `influx-token`) to the `(top-level key, field)` it overlays in the parsed
  settings dict (e.g. `("influx", "token")`) - 8 credentials across `influx` (`token`, `user`,
  `password`), `hue` (`user`), `mqtt` (`password`), `mcp` (`password`), `myenergi` (`apikey`),
  `octopus` (`api_key`). `PLACEHOLDER_VALUES`
  matches `example_settings.yaml`'s literal placeholder text per field (`mcp-password`'s is
  deliberately the empty string - see the MCP server section above); `sentinel_for(name)` returns
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
- **Slot credentials.** `hue-user2`, `hue-user3`, ... are credentials exactly like the static eight, and
  are **uncapped**. Every consumer asks one shared predicate in `credentials.py` rather than testing membership
  of `CREDENTIAL_FIELDS` itself: `credential_field(name)` -> `(section, field)`, `credential_name_for(section,
  field)` -> the inverse, `is_credential_field()`, `placeholder_for(name)` (a slot shares slot 1's placeholder),
  and `slot_credential_names(settings)` which *discovers* slots from the config rather than enumerating them -
  that discovery is what removes the cap. `CANONICAL_SLOT_SUFFIX_RE` lives there too, not in `philipshue.py`,
  because the settings side (which slot fields exist) and the credential side (which credential names exist)
  must agree and a second copy would drift. Two of the six consumers were security-relevant: `_cmd_set_field`'s
  refusal (without it, `--set-field hue.user2 <token>` writes a real token into settings.yaml in **plaintext**)
  and `_contains_real_secret` (without it, that token is invisible to the group/other-readable check). The
  others: `--remove`'s placeholder lookup (it indexed `PLACEHOLDER_VALUES` directly and would have died with a
  `KeyError`), `apply_credential_substitution` (now driven by a listing of `$CREDENTIALS_DIRECTORY`, since an
  unbounded set has no table to iterate), the CLI's name argument (a validator, since `choices=` cannot express
  unbounded names - it still refuses a typo), and `--list` (static names always, plus slots from settings, plus
  any stored credential with no matching field, reported as a probable leftover rather than cleaned up).
- **Creating a missing field.** `_rewrite_settings_field()` will now *create* `hue.hostN`/`hue.userN` when
  absent, which is what lets a bridge be added without hand-editing `settings.yaml` - `settings.yaml` is written
  once at install time from an example that has no `host2`. Gated to recognised slot names by
  `_is_creatable_field()`: creating any field on request would destroy the refusal's other job, which is
  catching a typo (`--set-field hue.hsot2 <address>` must stay an error, not become a key nothing reads).
  Slot 1's unnumbered `host`/`user` are excluded, since they ship in the example. The insertion point is
  `_last_scalar_line()`, **not** the section node's `end_mark`: a block mapping's end mark sits at the next
  token, which in this comment-dense file is past the blank line *and* the following section's leading comment,
  so inserting there would put the field under the wrong section. Values are written double-quoted like every
  other value this tool writes - unquoted would parse for a hostname and then break on one containing `#`.
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

### debconf-driven install

`packaging/deb/send-to-influx.templates` + `packaging/deb/config` (copied into `DEBIAN/templates`/
`DEBIAN/config` by `build-deb.sh`, which also adds `debconf (>= 0.5)` to `Depends:`): `config`'s job
is *only* asking questions and stashing answers in debconf's database - a hard Debian packaging
convention, so `dpkg-reconfigure`/backing out of an install never leaves partial side effects. It
never touches the filesystem and never calls into `credential_cli.py` - that only happens later,
from `postinst`, once package files are unpacked and everything's been answered.

- **Install/upgrade/reconfigure gating**: both scripts run their debconf flow only on a genuinely
  fresh install or an explicit `dpkg-reconfigure` - a plain package upgrade neither asks questions
  nor applies answers. `config` is invoked as `configure <previously-configured-version>` by
  `dpkg-preconfigure` (`$2` empty only on a fresh install) and as `reconfigure <version>` by
  `dpkg-reconfigure`, so it early-exits when `$1 != reconfigure && $2` is non-empty. `postinst` is
  invoked as `configure <version>` in both the upgrade and reconfigure cases, so it can't use its
  arguments alone - it checks `DEBCONF_RECONFIGURE=1`, which `dpkg-reconfigure` exports precisely so
  postinsts can tell the two apart (its source calls this "a hack to let postinsts know when they're
  being reconfigured"). Without this gate, debconf's database - a UI cache that persists every
  non-password answer indefinitely, not a change log - was effectively treated as a signal that
  configuration had happened *this* run: every upgrade re-prompted for `influx-secret` (blank,
  contextless - see the `db_set ""` note below), re-warned "not fully configured - not enabling it"
  for every previously-selected source whose password answers had (deliberately) been cleared, and
  re-wrote Open-Meteo's latitude/longitude into `settings.yaml` from the original install's answers,
  reverting any hand edits made since.
- InfluxDB's `influx-url`/`influx-identity`/`influx-secret` are asked *first*, unconditionally -
  deliberately **not** gated on any source being selected. An earlier version of this design asked
  them only after `sources-to-configure` and only if at least one source was picked, exiting `config`
  immediately otherwise - this made InfluxDB unreachable both interactively (an admin who only wants
  to migrate an already-configured InfluxDB credential into systemd-creds, without touching source
  config at all, had no way to get there) and via `dpkg-reconfigure` on a later run. That's a real,
  common case: an admin upgrading an already-working install has no reason to re-answer per-source
  questions for sources that already work, but commonly does want the new systemd-creds option for
  the credential they already have. `identity`/`secret` are generic (org+token for v2, user+password
  for v1), asked without knowing which version applies yet. Version detection deliberately does
  **not** happen in `config`: `config` runs *before* the package is unpacked on a first install, so it
  can't rely on the app's own venv/`requests`, and more fundamentally, gating *what gets asked* on
  being able to reach an arbitrary, possibly-remote, possibly-not-yet-provisioned URL at the exact
  moment of package install would defeat the point of the URL being configurable. Detection happens
  later, in `postinst`, via `send-to-influx-set-credential --detect-influx-version`. This always skips
  TLS verification, unconditionally - unlike `--ensure-influx-storage` (which respects
  `influx.insecure`), it never transmits a credential (both `/health` and `/ping` are unauthenticated
  probes) and its result only picks which prompt fields get routed to, not a trust decision a MITM'd
  response could meaningfully downgrade; `influx.insecure` also isn't necessarily known yet at this
  point, since debconf never asks for it (only ever hand-edited into settings.yaml afterwards).
- `send-to-influx/sources-to-configure` (`Type: multiselect`, priority `high` - matching InfluxDB's
  questions above, so a debconf priority threshold can't show one but silently hide the other) is
  asked next. Per-source blocks are only shown (via `db_input` called conditionally, not declaratively
  in the template file) for sources actually picked, so choosing one or two sources doesn't walk
  through prompts for the other six. Those conditional per-source questions are also priority `high`,
  not `medium` - debconf's default threshold is `high` (`debconf/priority` defaults to `high`), so a
  `medium` follow-up would be silently skipped on a normal install: the user ticks a source in the
  checklist, is never asked for the fields it needs, and postinst then reports it "not fully
  configured" (only `dpkg-reconfigure`, which shows low-priority questions regardless of the
  threshold, ever revealed them - which is why this wasn't caught by reconfigure-based testing).
  There's no prompt-spam risk in `high` here, since each question is only asked at all when its
  source was explicitly selected. Tuning fields (`interval`, `timeout`, `fields` lists,
  `stagger_seconds`) are never prompted for - see the "Template structure" reasoning
  in the original plan for why (`fields` particularly can't be validated against a source's real field
  names at install time). The one deliberate exception is `hue-temperature-units`, which gets a
  *computed* default (checks `$LC_ALL`/`$LANG` for a `_US` territory code, defaulting to Celsius
  otherwise) via `db_set` before the first `db_input`, rather than a silent guess - getting temperature
  units wrong is immediately visible to the user in a way the other tuning fields aren't.
- The shared MQTT broker block (`mqtt-broker-host`/`mqtt-username`/`mqtt-password`) is the second
  shared-infrastructure question group after InfluxDB, but *conditional* where InfluxDB's is
  unconditional: it's only asked (and only processed by `postinst`) when an MQTT-based source
  (currently `nuki`) is in the `sources-to-configure` selection - every install needs InfluxDB,
  but only MQTT sources have a broker, so a non-MQTT install must never be prompted for one.
  Its stored-credential semantics mirror InfluxDB's: a blank `mqtt-password` on reconfigure is
  satisfied by an existing `mqtt-password.cred`, and non-secret fields provided alongside a blank
  secret are still applied - except `mqtt-broker-host`, which is *required* like `hue-host` (not
  blank-keeps like `influx-url`): debconf string answers persist across reconfigures, so any
  install configured through the prompts always has a non-blank host anyway, and a blank one
  means hand-configured - where auto-enable would be speculative (possibly against the shipped
  placeholder host), so those installs hand-edit `sources:` instead, same precedent as
  plaintext-settings credentials. Blank username *and* password mean anonymous broker access - a
  valid configuration, not an incomplete one. Auth must be *coherent* to auto-enable though: a
  username with no password material (neither typed nor stored) warns instead of enabling a
  guaranteed auth-rejection retry loop. And switching an existing authenticated install to
  anonymous is not expressible through the prompts (blank means keep, per the standing
  no-clearing-via-debconf convention) - it's done by blanking `mqtt.username` in settings.yaml
  plus `send-to-influx-set-credential mqtt-password --remove`.
  `mqtt-username` is cleared from debconf's database after use like
  `influx-identity` (the other half of a credential pair), and both are in the final sweep.
- `postinst` (inside the fresh-install-or-reconfigure gate above): `sources-to-configure` is read
  first (`$SOURCES`, purely to know whether *anything* was
  selected - no processing happens from it yet), then InfluxDB is processed unconditionally,
  independent of that selection - a run where every question was
  left blank (e.g. non-interactive) is still a no-op for all of it, since each per-source block below
  self-gates on
  `$SOURCES` containing that source's name; nothing here requires the outer "was anything selected"
  gate the earlier design used. The one place `$SOURCES` matters this early: the "InfluxDB not
  provided" warning only fires if the admin engaged with the prompts this run (selected a source, or
  entered a secret without the matching identity) - a fully-blank run stays silent. Resolves the
  InfluxDB block via
  `--detect-influx-version` and routes `identity`/`secret` accordingly - v2 writes `identity` to the
  plain (non-secret) `influx.org` field via `--set-field` and `secret` to the `influx-token` credential;
  v1 routes both `identity` and `secret` to the `influx-user`/`influx-password` credentials - if
  detection comes back `unknown` (URL unreachable at install time, expected to happen sometimes since
  the URL can point anywhere), nothing is written and no source is auto-enabled this run; a secret
  prompt is never pre-filled with a previous answer on a later `dpkg-reconfigure` (see the `Type:
  password` note below), so that re-run cleanly re-collects them rather than silently reusing something
  stale. Then, per selected source: secrets via `send-to-influx-set-credential <name>`, non-secret fields via
  `--set-field`, both reusing the same CLI rather than a second YAML-patcher in shell. **Auto-enable**:
  a source is only added to `sources:` (via `--enable-source`) if *every* required field for it (and
  the InfluxDB block) actually resolved - not just "was it ticked" - with `--ensure-influx-storage`
  attempted first (best-effort database/bucket creation, logged-not-raised on failure). `example_settings.yaml`
  ships a bare `sources:` key with every entry commented out (nothing enabled by default - see the entry-point
  section above), which parses as null, not an empty sequence. `_enable_source()`/`_load_sources_sequence()` in
  `toinflux/credential_cli.py` handle that as their own case - along with the less common but equally valid
  explicit `sources: []` - since neither has an existing item to anchor a block-style append after: the first
  `--enable-source` call rewrites that one line into `sources:` plus a single indented item (the commented-out
  placeholder lines survive untouched below it, same as any other pre-existing comment), rather than refusing
  (as it still does for a *populated* flow-style list like `sources: ["hue"]`, where there's no safe insertion
  point without risking invalid YAML). Refuses instead of silently dropping any unexpected trailing content on
  the `sources:` line itself (e.g. a hand-added inline comment). A secret
  field also counts as resolved when its credential is already stored in systemd-creds (`.cred`
  file present in the credstore) - secret prompts always come back blank on a reconfigure ("blank
  keeps the stored value"), so an already-configured install revisiting the prompts (e.g. to add
  one new source) isn't wrongly reported "not fully configured", and `--ensure-influx-storage`
  resolves stored credentials itself, so InfluxDB counting as configured this way still lets
  newly-added sources auto-enable. (A credential kept in plaintext `settings.yaml` and never
  migrated doesn't get this treatment - postinst can't cheaply distinguish it from a placeholder -
  so that setup re-enters secrets on reconfigure or hand-edits `sources:`.)
- `Type: password` answers *are* written to disk by debconf - contrary to an earlier version of this
  note - into a dedicated `passwords.dat` store kept separate from its general-purpose, more widely
  readable answer database, and restricted to `chmod 600`. Debian's own developers' guide
  (`debconf-devel(7)`) advises clearing a password value out of it "as soon as is possible" once
  consumed, so `postinst` does: immediately after each `db_get` on a password-type template
  (`influx-secret`, `hue-user`, `myenergi-apikey`, `octopus-api-key`), it clears the stored answer
  with `db_set <question> ""` (plus `influx-identity`, string-typed but a v1 *username*, and a final
  unconditional sweep of all of them so a preseed for an unselected source can't leave a secret in
  `passwords.dat`),
  regardless of whether the subsequent `systemd-creds` migration for that value goes on to succeed or
  fail. (An earlier version used `db_unregister`, which cleared the value equally well but deleted
  the question's `seen` flag with it - the question was recreated fresh/unseen from the templates
  file on the next run, so debconf re-asked it, blank, on every upgrade; `db_set ""` empties the
  value while leaving the question registered and seen.) Separately, and unrelated to the clearing,
  debconf *never* redisplays/pre-fills a previous password answer in the prompt on a later
  invocation - a UI convention for this template type. So a reconfigure always shows secret prompts
  blank, with no way for `postinst` to distinguish "leave it as-is" from "clear it" from that alone -
  resolved by not supporting clearing via debconf at all: `postinst` treats blank as "keep the existing
  systemd-creds value," and removing a credential goes through `send-to-influx-set-credential <name>
  --remove` directly instead.

## Rejected: environment-variable secrets

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
