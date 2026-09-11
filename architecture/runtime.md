<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Runtime: entry point, settings and validation

Deep detail behind the entry-point and settings summaries in [../AGENTS.md](../AGENTS.md).
Read this before changing `sendtoinflux.py` or `toinflux/general.py`.

## Entry point (`sendtoinflux.py`)

- **Single-source mode** (`--source <name>`): continuous loop, fixed interval per source. Connection failures (`SourceConnectionError`) are retried with exponential backoff (base 5 s, max 300 s); a `ConfigError` is not retried - it exits the process immediately with code 1.
- **Work units.** Every mode expands the requested source names into `(source, instance)` work units via
  `expand_sources()` (`toinflux/general.py`) - one unit per worker, the same shape as `DataHandler.worker_key`.
  Most sources expand to a single `(name, None)`; a source in `INSTANCED_SOURCES` (only `hue`) expands to one
  unit per *configured bridge*, so each bridge gets its own thread, its own backoff and its own write buffer -
  an unreachable bridge delays only itself. One function serves `--source`, the supervisor and `--dump` alike,
  so they cannot disagree about what runs. `_requested_sources()` resolves what was actually asked for: `--source`
  wins, otherwise the `sources:` list, otherwise nothing - there is no `default_source` fallback (removed
  outright, no deprecation window; nothing suggested anyone relied on it). A source that expands to nothing (Hue
  with no usable bridge) simply has no worker; `_exit_if_nothing_to_collect()` stops the process when *every* requested
  source expands to nothing, or nothing was requested at all - two distinct causes, logged distinctly (the
  journal can tell "nothing configured" from "configured but unusable" apart) - rather than idling while
  appearing healthy. Both exit with code 1, the same code a fatal `ConfigError` uses: neither self-resolves by
  waiting, so `packaging/send-to-influx.service` marks that code `RestartPreventExitStatus`, and the packaged
  service is not respawned for any of the three. The startup INFO line ("Starting send-to-influx vX
  (workers=...)") is logged *before* this check, so even an immediate exit is preceded by the normal
  version/intent banner - `workers=none` when nothing was requested - rather than only the critical line. It
  reports `workers=`, not `sources=`, because with an instanced source the two differ. `run_workers()` staggers
  across the *expanded* list, so two bridges are spread apart exactly as two sources are; the supervisor's
  restart/stall bookkeeping is keyed by unit, and `--dump` emits a JSON object keyed by instance whenever the
  source is instanced (even with one bridge, so nothing reading the output depends on the operator's bridge
  count), printing what succeeded and exiting 2 if any bridge failed. `run_one_worker()` keeps the main-thread
  path when there is exactly one unit, which is what lets a streaming source shut down cleanly on a signal.
- **Multi-source mode** (no `--source`): reads `sources` list from `settings.yaml`, expands it into work units (above) and spawns one daemon thread **per unit** - one per source for most, one per bridge for Hue - with a configurable startup stagger (`stagger_seconds`, default 10) applied across the expanded list. Dead threads are detected and restarted with the same exponential backoff - unless that worker stopped because of a `ConfigError`, in which case it is logged and left stopped (every other worker keeps running, including the other bridges of the same source). The restart, stall and stopped bookkeeping is all keyed by work unit, not by source name, so two workers on one source name stay distinguishable.
- `--dump`: one-time raw JSON to stdout, then exit (single source only).
- `--print`: parsed data to stdout instead of InfluxDB.
- `--settings <path>`: use a settings file at a path other than `settings.yaml` in the project root (e.g. `/etc/send-to-influx/settings.yaml` for a packaged install). Threaded through `toinflux.get_class()`/`load_settings()`.
- `--version`: print `__version__` and exit; parsed before settings are loaded, so it works without a `settings.yaml` present.
- `--check-config`: load and validate the settings file (via `load_settings()`) - via `_check_config_and_exit()`, extracted out of `main()` to keep its cyclomatic complexity within the flake8 limit. Prints `Configuration OK` and exits 0 only if validation passes **and** something is actually requested (`_requested_sources()` non-empty); a config that validates cleanly but configures nothing to collect prints the same "no sources are configured" failure `_exit_if_nothing_to_collect()` would stop a real run for, and exits 1 - "OK" must not mean "nothing will happen". Exits 1 with details if invalid (same validation as a normal run). If `--source` is also given, that source's block is validated too even if it isn't in `sources:` (`validate_settings(settings, source=...)`), so checking config for a one-off `--source` can't report a false "OK".
- `-v`/`--verbose`: force `DEBUG`-level logging, overriding the `loglevel` settings.yaml key.
- Handles SIGINT and SIGTERM for graceful shutdown.
- On startup, logs an INFO line with the version and the source(s) that will run, so process (re)starts are visible in the logs.
- CLI arguments are parsed *before* `load_settings()` is called, so `--version`/`--help` don't require a config file to exist.
- After every collection cycle, `maybe_send_heartbeat()` writes a `collector_status,source=<name>` point (fields `ok`, `consecutive_failures`) via `send_heartbeat()`, which reuses the source's own `DataHandler.send_data()` with a swapped-in header. Skipped in `--print` mode.

## Factory / settings

- `toinflux/general.py`: `load_settings(settings_file=None)` (raises `ConfigError` on missing/invalid YAML; `settings_file` defaults to `settings.yaml` in the project root when omitted), `get_class(source, settings_file=None, instance=None)` (case-insensitive factory returning a constructed handler - `source_class()` is the one returning the class itself, uninstantiated; raises `ConfigError` for an unknown source, including the abstract `DataHandler` and `MyEnergi` bases, which are not registered as selectable sources; threads `settings_file` through to the handler's `load_settings()` call), `flatten_dict()` (used by Speedtest to flatten nested JSON), `configure_logging(logfile=None, loglevel="INFO", log_max_bytes=..., log_backup_count=...)` (sets up timestamped **stderr** logging, plus an optional `RotatingFileHandler`; raises `ConfigError` instead of a raw `OSError` if `logfile` can't be opened, e.g. a permissions problem).
- `configure_logging()` is called via `_configure_logging_or_exit()` in `main()` after settings are loaded and `--check-config` has short-circuited - this catches that `ConfigError`, logs it (the stderr handler is already attached by the time it's raised, so this still reaches the journal under systemd as a normal formatted line, not a traceback), and exits 1. Log messages use the format `YYYY-MM-DD HH:MM:SS LEVEL message`.
- **Diagnostics go to stderr; stdout carries the program's data.** Every log level, `--check-config`'s `Configuration error:` and the credential CLI's errors are on stderr; `--dump`/`--print` JSON, `Configuration OK` and the credential CLI's success messages are on stdout. That split is what makes `--dump | jq` reliable: a partially-successful dump reports a failure *and* emits a payload, and while both shared stdout the payload was unparseable exactly when it mattered. Every level moves, not just errors - splitting diagnostics by severity across two streams would interleave them unpredictably for anyone capturing either. Under systemd nothing changes: the unit pins neither `StandardOutput` nor `StandardError`, so both already reach the journal, and the rsyslog rule matches on `programname` rather than a stream - asserted against a real install by `test-packaging.sh` rather than inferred from the unit file. Records emitted *before* `configure_logging()` runs already went to stderr via Python's `lastResort` handler, so the streams now agree; their format still differs (`CRITICAL:root:...`), which is cosmetic and left alone. Effective log level is `-v`/`--verbose` (forces `DEBUG`) > `loglevel` settings.yaml key > `INFO` default.
- Config file: `settings.yaml` (copy from `example_settings.yaml`), or a custom path via `--settings`. Required at runtime; not committed. Optional `logfile` key adds a rotating file log destination (`log_max_bytes`/`log_backup_count` settings keys control rotation, defaulting to 10 MiB / 3 backups). Some fields can optionally be sourced from `systemd-creds` instead on the packaged install - see "Credential storage (`systemd-creds`)" below; an environment-variable secret-override mechanism was considered and deliberately rejected instead - see "Rejected: environment-variable secrets" below.

**Configuration faults are caught at validation, not at the first collection.** Two shapes used
to slip past `--check-config` and surface much later, both now terminal errors from
`_unusable_source_block()`:

- **A source section that is not a mapping.** `"interval" not in source_cfg` is a containment
  test, so a section set to null or a scalar raised a raw `TypeError` out of validation - a
  traceback where `--check-config` exists to give a message, and the same traceback in the
  journal on startup. The null case is the one reached by accident: commenting out every field
  under a section leaves the bare key, which YAML parses as `None`, so it gets its own message
  saying so rather than "got NoneType". The check returns immediately rather than collecting
  further errors, because "interval is required" about a section with no fields at all buries
  the cause under its consequences. `enumerate_bridges()` keeps its own type guard - `Hue.bridge()`
  calls it at runtime, where no validation has run - but `_validate_hue_block()` now defers to the
  shared check so the same sentence is not printed twice.
- **A source name nothing can collect.** `get_class()` always refused an unknown name, but only
  once a worker tried to construct a handler, so `--check-config` reported "Configuration OK" and
  the worker loop's broad handler then retried forever. Validation refuses the name up front and
  lists what is accepted. This also catches an ordinary typo with a matching section, which
  previously validated cleanly.

**Only collectable sources are in the registry.** The `MyEnergi` parent was registered alongside
Zappi/Eddi/Harvi and filtered back out by `known_sources()`, which let the two disagree: the name
validated, constructed, and then died with `AttributeError: 'MyEnergi' object has no attribute
'get_data'` on every cycle. It is simply absent now, like `DataHandler`, and `known_sources()`
needs no filter. `measurement_for()`/`shares_measurement()` are unaffected - they iterate
`known_sources()`, which never included it.

## Running an external command (`toinflux/process.py`)

`run_command()` is the only place this project starts a process. Read this before adding a
call site; `tests/test_repo_hygiene.py::test_only_the_process_helper_starts_a_process` fails a
module under `toinflux/` that imports `subprocess` instead.

What it guarantees, and why each one is there rather than left to the caller:

- **No shell, ever.** The argument list is passed as a list, so a value that reaches argv
  cannot become a second command.
- **An allow-listed environment**, not the inherited one. `INHERITED_ENV_KEYS` names what
  passes through. `CREDENTIALS_DIRECTORY` and `STATE_DIRECTORY` are on it because a control
  process reads its own secrets and its own configuration from them: dropping either produces
  a child that reports a missing file or a permissions error a long way from the cause.

  The list is **generous with benign variables and strict about one category**, and the
  asymmetry is the reason: a missing variable surfaces as what looks like a permissions bug,
  while a spare one a child never reads costs nothing. So identity, locale, timezone and
  `TMPDIR` are all carried. What never passes is anything that changes *what code the child
  runs* - `PYTHONPATH`, `PYTHONHOME`, `LD_PRELOAD`, `LD_LIBRARY_PATH` - which is the actual
  purpose of having a list rather than housekeeping, and is guarded by
  `tests/test_process.py::TestEnvironmentAllowList::test_execution_altering_variables_never_reach_the_child`.
- **argv[0] resolved before the spawn**, by `shutil.which()` against the *child's* PATH so
  lookup and execution cannot disagree, or used as given when it is a path. A path is
  legitimate: a console script inside the packaged venv is on nobody's PATH. Failure is
  `ConfigError`, which no amount of retrying fixes.
- **A mandatory timeout.** No default, because no default fits both a version banner and a
  TPM-backed decrypt. Overrunning raises `ProcessError` after killing the child.
- **A cap on what is kept** from each output stream, which keeps draining past the limit
  rather than stopping. A reader that stopped would leave the child blocked writing into a
  full pipe, turning a large output into a hang.
- **Standard error only in the timeout message.** Standard output is the data channel:
  `systemd-creds decrypt` writes the plaintext credential there, so a decrypt that hung
  after emitting part of it would put the secret into an exception message and from there
  into the journal.

Two shapes of failure, and the split matters:

- **A command that ran and exited non-zero is returned**, not raised. Its output is usually the
  only explanation of why it failed, and some callers legitimately ignore the status. Check
  `CommandResult.ok`.
- **A command that never finished raises.** There is no exit status to report, and returning
  partial output invites a caller to use it as though the command had completed.

`_pump()` reports which of the two happened by returning a bool, and the polarity reads
backwards from the usual instinct: **True means it went wrong** and the caller must kill the
child. False means the pump finished on its own terms, which it only does once the child has
exited - and that post-condition is what makes the `wait()` after it safe without a timeout of
its own.

**Captured output is bytes, deliberately.** `stdout_text`/`stderr_text` decode with replacement
for a message or a log line. A caller holding something that must be exactly what the command
emitted decodes strictly itself: `_decrypt_credential()` does, and treats invalid UTF-8 as the
failure it is, which decoding centrally with replacement would have turned into replacement
characters that still look like a password.

**Nothing here logs captured output**, because a command's stdout can be a decrypted secret.
Whether any of it is safe to log is a question only the caller can answer.

### The pump is single-threaded, and has to be

`_pump()` feeds standard input and drains both outputs from the calling thread, using a
selector over non-blocking pipes. The obvious design - a reader thread per pipe - was tried
first and does not work, for a reason worth recording because it is invisible until it bites:

- A grandchild that inherits a pipe holds its write end open, so the read never reaches EOF
  however long the wait. The direct child can have exited long before.
- A blocked reader thread cannot be cleaned up from outside. Closing the stream from another
  thread waits on the same lock the blocked read holds rather than interrupting it - measured,
  not assumed: a `close()` took as long as the reader stayed blocked.

So the threaded version could only leak a reader per call or block the caller past its own
timeout, and a supervisor launching control processes on a loop would do both. With no
threads there is nothing to leak, the deadline covers the whole interaction rather than only
the wait, and abandoning an inherited pipe is a decision the loop can simply take.

Once the child has exited, draining continues for `_DRAIN_GRACE_SECONDS` and then stops:
whatever still holds that pipe is not going to close it. Output the command produced before
that point is still returned.

## Control configuration (`toinflux/controls.py`)

A control is a closed loop holding something at a target by actuating a device. Each is a
YAML document, one file per control, under the installation's **state directory** rather
than `/etc`: these are written by the running service and by an MCP client on its behalf,
and `/etc/send-to-influx` is root-owned while the service runs as `send-to-influx`.

`resolve_state_dir()` (`toinflux/general.py`) is the single answer to "where does runtime
state live" - systemd's `$STATE_DIRECTORY` when there is one, otherwise beside the settings
file. `resolve_state_path()` in the MCP layer is built on it, so the OAuth state file and
the control store cannot disagree about the location. It lives in `general.py` rather than
the MCP layer so a control process can ask without importing the MCP SDK.

**A control's name is its filename, and an MCP client chooses it.** `CONTROL_NAME_PATTERN`
is an allow-list, and a name outside it is refused rather than sanitised: rewriting a name
would make the control the caller asked for and the control that exists two different
things, and mapping two requested names onto one file is worse than an error. This is the
boundary that stops a caller picking which file gets written.

**Writes are atomic.** A temporary file in the same directory, flushed and fsynced, then
renamed into place - so a control process reading the document at the moment it changes
sees the old one or the new one, never half of either. A failed write removes its
temporary file rather than leaving a dot-file behind per attempt.

**Validation here is structural only.** A rule expression is checked to be a string and no
further; the parser reports its own syntax errors. That split keeps a missing key and a
malformed expression each described by the code that understands it. Every problem in a
document is collected rather than raising on the first, because an operator writing one by
hand would otherwise fix a single typo per run.

Two rules are worth knowing before changing this:

- **Every stage must assign every device the control owns.** A stage that says nothing
  about a device is not the same as a stage turning it off, and an operator reading the
  ladder would assume it was off. This is how a heater silently stays on.
- **A `bool` is not a number.** `bool` subclasses `int`, so `level: true` would otherwise
  validate and then sort as 1, quietly inserting a rung into the ladder.

`--check-config` validates every stored control, because they are configuration even though
they do not live in `settings.yaml`, and the question that mode answers is whether this
installation would start cleanly.
