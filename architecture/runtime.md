<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Runtime: entry point, settings and validation

Deep detail behind the entry-point and settings summaries in [../AGENTS.md](../AGENTS.md).
Read this before changing `sendtoinflux.py` or `toinflux/general.py`.

## Entry point (`sendtoinflux.py`)

**Expand source names into work units through `expand_sources()`** (`toinflux/general.py`) and
key everything off the unit, never the source name. A unit is `(source, instance)`, the same
shape as `DataHandler.worker_key`. Most sources expand to one `(name, None)`; a source in
`INSTANCED_SOURCES` (only `hue`) expands to one unit per configured bridge, so each bridge gets
its own thread, backoff and write buffer and an unreachable bridge delays only itself.

One function serves `--source`, the supervisor and `--dump`, so they cannot disagree about what
runs. Restart, stall and stopped bookkeeping is keyed by unit, so two workers on one source name
stay distinguishable. `run_workers()` staggers across the expanded list.

`_requested_sources()` resolves what was asked for: `--source` wins, otherwise the `sources:`
list, otherwise nothing. There is no `default_source` fallback.

**Exit rather than idle when nothing will collect.** `_exit_if_nothing_to_collect()` stops the
process when every requested source expands to nothing (Hue with no usable bridge) or nothing
was requested at all. Log those two causes distinctly - the journal must tell "nothing
configured" from "configured but unusable". Both exit 1, the code a fatal `ConfigError` uses,
and `packaging/send-to-influx.service` marks it `RestartPreventExitStatus` so the packaged
service is not respawned for any of them.

Log the startup INFO line - `Starting send-to-influx vX (workers=...)` - *before* that check, so
even an immediate exit is preceded by the banner, with `workers=none` when nothing was
requested. It says `workers=`, not `sources=`, because with an instanced source the two differ.

Modes:

- **Single-source** (`--source <name>`): continuous loop at a fixed interval. Retry
  `SourceConnectionError` with exponential backoff, base 5 s and max 300 s; never retry a
  `ConfigError` - exit 1 immediately.
- **Multi-source** (no `--source`): one daemon thread per unit, started with a
  `stagger_seconds` stagger (default 10) across the expanded list. Restart a dead thread with
  the same backoff *unless* it stopped on a `ConfigError`, which is logged and left stopped;
  every other worker keeps running, including other bridges of the same source.
- `run_one_worker()` keeps the main-thread path when there is exactly one unit, which is what
  lets a streaming source shut down cleanly on a signal.

Flags:

- `--dump`: one-time raw JSON to stdout, then exit (single source only). Emits an object keyed
  by instance whenever the source is instanced, even with one bridge, so nothing reading the
  output depends on the operator's bridge count. Exits 2 if any bridge failed.
- `--print`: parsed data to stdout instead of InfluxDB.
- `--settings <path>`: a settings file elsewhere than `settings.yaml` in the project root, e.g.
  `/etc/send-to-influx/settings.yaml`. Threaded through `toinflux.get_class()`/`load_settings()`.
- `--version`: prints `__version__` and exits, parsed before settings load so it works with
  no `settings.yaml` present.
- `--check-config`: validates via `load_settings()`, through `_check_config_and_exit()` (split
  out of `main()` to keep its cyclomatic complexity within the flake8 limit). Prints
  `Configuration OK` and exits 0 only if validation passes **and** `_requested_sources()` is
  non-empty - "OK" must not mean "nothing will happen"; a config that validates and configures
  nothing prints the same failure a real run would. With `--source`, that source's block is
  validated even if it is not in `sources:` (`validate_settings(settings, source=...)`).
- `-v`/`--verbose`: forces `DEBUG`, overriding the `loglevel` key.

Parse CLI arguments before calling `load_settings()`, so `--version`/`--help` need no config
file. SIGINT and SIGTERM shut down gracefully.

After each cycle `maybe_send_heartbeat()` writes a `collector_status,source=<name>` point
(fields `ok`, `consecutive_failures`) via `send_heartbeat()`, reusing the source's own
`DataHandler.send_data()` with a swapped-in header. Skipped in `--print` mode.

## Factory / settings

`toinflux/general.py` holds:

- `load_settings(settings_file=None)` - raises `ConfigError` on missing or invalid YAML;
  defaults to `settings.yaml` in the project root.
- `get_class(source, settings_file=None, instance=None)` - case-insensitive factory returning a
  constructed handler, threading `settings_file` through to the handler's own `load_settings()`.
  `source_class()` returns the class uninstantiated. Both raise `ConfigError` for an unknown
  source, including the abstract `DataHandler` and `MyEnergi` bases.
- `flatten_dict()` - used by Speedtest to flatten nested JSON.
- `configure_logging(logfile=None, loglevel="INFO", log_max_bytes=..., log_backup_count=...)` -
  stderr logging plus an optional `RotatingFileHandler`. Raises `ConfigError` rather
  than a raw `OSError` when `logfile` cannot be opened.

Call it through `_configure_logging_or_exit()` in `main()`, after settings load and after
`--check-config` has short-circuited. That catches the `ConfigError`, logs it and exits 1; the
stderr handler is attached by then, so it reaches the journal as a formatted line rather than a
traceback. Log messages use the format `YYYY-MM-DD HH:MM:SS LEVEL message`, except on the stderr
handler when `$JOURNAL_STREAM` is set - systemd sets it for a unit whose output goes to the
journal, which stamps every line itself, as does the rsyslog rule copying it to a file. There the
format is `LEVEL message`. Not a tty check: stderr redirected to a file by hand has nothing else
stamping it, which is the case a tty check would get backwards. The `RotatingFileHandler` always
keeps the timestamp, for the same reason.

**Put diagnostics on stderr and the program's data on stdout.** Every log level,
`--check-config`'s `Configuration error:` and the credential CLI's errors go to stderr;
`--dump`/`--print` JSON, `Configuration OK` and the credential CLI's success messages go to
stdout. That is what makes `--dump | jq` reliable, since a partially-successful dump reports a
failure *and* emits a payload. Move every level, not just errors: splitting diagnostics by
severity across two streams interleaves them unpredictably for anyone capturing either.

The unit pins neither `StandardOutput` nor `StandardError`, so both already reach the journal,
and the rsyslog rule matches on `programname` rather than a stream - asserted against a real
install by `test-packaging.sh`. Records emitted before `configure_logging()` runs reach stderr
via Python's `lastResort` handler; their format differs (`CRITICAL:root:...`), which is cosmetic
and left alone.

Effective log level: `-v`/`--verbose` (forces `DEBUG`) > the `loglevel` settings.yaml key >
`INFO` default.

Config file: `settings.yaml` (copy `example_settings.yaml`) or a path via `--settings`. Required
at runtime, not committed. An optional `logfile` key adds a rotating file log, with
`log_max_bytes`/`log_backup_count` controlling rotation (10 MiB / 3 backups by default). Some
fields can come from `systemd-creds` on a packaged install - see "Credential storage
(`systemd-creds`)" below, and "Rejected: environment-variable secrets" for what was refused.

### Catch configuration faults at validation, not at first collection

`_unusable_source_block()` makes both of these terminal:

- **A source section that is not a mapping.** `"interval" not in source_cfg` is a containment
  test, so a section set to null or a scalar raised a raw `TypeError` out of validation. The
  null case is the one reached by accident - commenting out every field under a section leaves
  the bare key, which YAML parses as `None` - so it gets its own message rather than "got
  NoneType". Return immediately rather than collecting further errors: "interval is required"
  about a section with no fields buries the cause under its consequences. `enumerate_bridges()`
  keeps its own type guard because `Hue.bridge()` calls it at runtime where no validation has
  run; `_validate_hue_block()` defers to the shared check so the sentence is not printed twice.
- **A source name nothing can collect.** `get_class()` refused an unknown name only once a
  worker tried to construct a handler, so `--check-config` said "Configuration OK" and the
  worker loop retried forever. Validation refuses the name up front and lists what is accepted,
  which also catches a typo with a matching section.

**Register only collectable sources.** The `MyEnergi` parent was registered alongside
Zappi/Eddi/Harvi and filtered back out by `known_sources()`, so the name validated, constructed,
then died with `AttributeError: 'MyEnergi' object has no attribute 'get_data'` every cycle. It
is absent now, like `DataHandler`, and `known_sources()` needs no filter.
`measurement_for()`/`shares_measurement()` iterate `known_sources()` and are unaffected.

## Running an external command (`toinflux/process.py`)

`run_command()` is the only place this project starts a process. Read this before adding a call
site: `tests/test_repo_hygiene.py::test_only_the_process_helper_starts_a_process` fails a module
under `toinflux/` that imports `subprocess`.

What it guarantees, and why the caller does not get to choose:

- **No shell, ever.** Arguments are passed as a list, so a value reaching argv cannot become a
  second command.
- **An allow-listed environment.** `INHERITED_ENV_KEYS` names what passes.
  `CREDENTIALS_DIRECTORY` and `STATE_DIRECTORY` are on it because a control process reads its
  secrets and its configuration from them; dropping either produces a child reporting a missing
  file or a permissions error a long way from the cause.

  Be generous with benign variables and strict about one category: a missing variable surfaces
  as what looks like a permissions bug, while a spare one a child never reads costs nothing. So
  identity, locale, timezone and `TMPDIR` all pass. What never passes is anything changing *what
  code the child runs* - `PYTHONPATH`, `PYTHONHOME`, `LD_PRELOAD`, `LD_LIBRARY_PATH` - guarded
  by `tests/test_process.py::TestEnvironmentAllowList::test_execution_altering_variables_never_reach_the_child`.
- **argv[0] resolved before the spawn**, by `shutil.which()` against the *child's* PATH so lookup
  and execution cannot disagree, or used as given when it is a path - a console script inside the
  packaged venv is on nobody's PATH. Failure is `ConfigError`, which no amount of retrying fixes.
- **A mandatory timeout**, with no default: none fits both a version banner and a TPM-backed
  decrypt. Overrunning raises `ProcessError` after killing the child.
- **A cap on what is kept** from each stream, while draining continues past the limit. A reader
  that stopped would leave the child blocked writing into a full pipe, turning a large output
  into a hang.
- **Standard error only in the timeout message.** Stdout is the data channel:
  `systemd-creds decrypt` writes the plaintext credential there, so a decrypt that hung after
  emitting part of it would put the secret into an exception message and into the journal.

Return a command that ran and exited non-zero; raise only for one that never finished. Its
output is usually the only explanation of a failure and some callers legitimately ignore the
status - check `CommandResult.ok`. A command that never finished has no exit status, and
returning partial output invites a caller to treat it as complete.

`_pump()` returns a bool whose polarity reads backwards from instinct: **True means it went
wrong** and the caller must kill the child. False means the pump finished on its own terms,
which happens only once the child has exited - that post-condition is what makes the following
`wait()` safe without a timeout of its own.

**Captured output is bytes.** `stdout_text`/`stderr_text` decode with replacement for a message
or log line. A caller needing exactly what the command emitted decodes strictly itself:
`_decrypt_credential()` does, and treats invalid UTF-8 as the failure it is, which decoding
centrally with replacement would turn into replacement characters that still look like a
password.

**Never log captured output here.** A command's stdout can be a decrypted secret, and whether
any of it is safe to log is a question only the caller can answer.

### Keep the pump single-threaded

`_pump()` feeds standard input and drains both outputs from the calling thread, using a selector
over non-blocking pipes. Do not reintroduce a reader thread per pipe:

- A grandchild that inherits a pipe holds its write end open, so the read never reaches EOF
  however long the wait. The direct child can have exited long before.
- A blocked reader thread cannot be cleaned up from outside. Closing the stream from another
  thread waits on the same lock the blocked read holds rather than interrupting it - measured,
  not assumed: a `close()` took as long as the reader stayed blocked.

Threads could therefore only leak a reader per call or block the caller past its own timeout,
and a supervisor launching control processes on a loop would do both.

Once the child has exited, draining continues for `_DRAIN_GRACE_SECONDS` and stops; whatever
still holds that pipe is not going to close it. Output produced before that point is returned.

## Logging (`toinflux/general.py`)

Format every record through `IndentedFormatter`, which indents each line after the first. Do not
replace it with `logging.Formatter`.

Entries start with a timestamp and rsyslog writes them to `/var/log/send-to-influx.log`, a
line-oriented file, so an un-indented continuation line can pose as a genuine entry. Records
carry text from outside - an exception's message, a device name, a document a YAML parser quotes
back - and `!r` covers only values this project interpolates itself, never a traceback, whose
last line is the message at column zero.

- Split with `splitlines()`, not `"\n"`: a carriage return, form feed, next-line character and
  the unicode separators all break a line for some readers.
- Indent rather than strip the newlines. A stack trace is the diagnostic the supervisor's broad
  catch exists to preserve.
- When testing a formatter, assert on the handlers `configure_logging` installs. A test that
  builds the formatter itself passes against a project that never uses it.

## Control configuration (`toinflux/controls.py`)

A control is a closed loop holding something at a target by actuating a device: one YAML
document per control, under the installation's **state directory** rather than `/etc`. They are
written by the running service and by an MCP client on its behalf, and `/etc/send-to-influx` is
root-owned while the service runs as `send-to-influx`.

`resolve_state_dir()` (`toinflux/general.py`) is the single answer to where runtime state lives:
systemd's `$STATE_DIRECTORY` when there is one, otherwise beside the settings file. The MCP
layer's `resolve_state_path()` is built on it, so the OAuth state file and the control store
cannot disagree about the location. It lives in `general.py` so a control process can ask
without importing the MCP SDK.

- **Refuse a name outside `CONTROL_NAME_PATTERN`; never sanitise one.** A control's name is its
  filename and an MCP client chooses it. Rewriting a name makes the control the caller asked for
  and the control that exists two different things, and mapping two requested names onto one
  file is worse than an error. This is the boundary that stops a caller picking which file gets
  written.
- **Write atomically**: a temporary file in the same directory, flushed and fsynced, then
  renamed into place, so a control process reading the document at the moment it changes sees
  the old one or the new one and never half of either. A failed write removes its temporary file
  rather than leaving a dot-file behind per attempt.
- **Collect every problem rather than raising on the first.** An operator writing a document by
  hand would otherwise fix a single typo per run.
- **Every stage must assign every device the control owns.** A stage that says nothing about a
  device is not a stage turning it off, and an operator reading the ladder would assume it was.
  This is how a heater silently stays on.
- **A `bool` is not a number.** `bool` subclasses `int`, so `level: true` would validate and then
  sort as 1, quietly inserting a rung into the ladder.

`validate_control()` is the wrapper and the call to make. It runs `validate_control_structure()`
for the shape the store requires, `validate_control_rules()` for what the parser accepts, and
`validate_control_sources()` for whether the sources named exist and can do what is asked of
them. Each half reports only its own faults, so one document does not name a single mistake
twice.

`--check-config` validates every stored control: they are configuration even though they do not
live in `settings.yaml`, and that mode answers whether this installation would start cleanly.

## The control rule language (`toinflux/rules.py`)

A control's setpoint, cap and gate are expressions - hold the conservatory at the greater of the
user's target or dew point plus five, cap the heaters when grid carbon is high, run only while
it is cold outside - and they arrive from a file an MCP client can write.

**Parse them; never evaluate them as code.** The grammar is arithmetic over numbers and nothing
else: no strings, no attribute access, no indexing, no assignment, and no calls beyond a fixed
table of five. There is no path from a rule to an object, an import or the filesystem because
the language cannot express one, which is a stronger position than a deny-list somebody has to
keep complete. `simpleeval` and `asteval` were rejected for that reason: both evaluate a subset
of the Python AST, a far larger surface to defend.

Precedence is Python's - `or` < `and` < `not` < comparison < `+ -` < `* /` < unary minus -
because anyone writing a rule already has that order in their fingers, and a language that
looked like Python but bound differently would be worse than one that looked nothing like it.

- **Truth is a number.** A comparison yields 1.0 or 0.0, `if` treats non-zero as true, and a gate
  is true when it evaluates non-zero. `and`/`or` return 1.0 or 0.0 rather than one of their
  operands as Python's do, so a non-truth value cannot leak out of a gate into a sum.
- **`and`, `or` and `if` short-circuit**, so a rule can guard its own arithmetic:
  `divisor != 0 and total / divisor > 5` never divides by zero. (`not` is unary, so there is
  nothing for it to skip.)
- **Length and nesting depth are bounded.** Recursive descent makes nesting depth stack depth:
  unbounded, a few thousand opening brackets exhaust the interpreter and raise `RecursionError`,
  which is not a `RuleSyntaxError` and so escapes as a crash rather than a report about a bad
  rule. `MAX_NESTING_DEPTH` is measured rather than picked - about ten frames per level against
  a default limit of 1000 - and
  `tests/test_rules.py::TestBounds::test_a_rule_at_the_nesting_limit_still_parses` holds it, so
  a future interpreter spending more frames per level fails CI rather than a control process.
- **Comparisons do not chain.** Python reads `a < b < c` as a chain and C reads it as
  `(a < b) < c`; both are defensible and they disagree, so it is refused with a message naming
  the `and` form to write instead.
- **Names resolve when the rule is parsed**, against what the control declared, so an undeclared
  name is a `--check-config` failure rather than a surprise at three in the morning. A rule does
  **not** decide what gets fetched: `gather()` reads every input the document declares, whether a
  rule mentions it or not, so an input no rule reads is still fetched and a stale one fails the
  whole cycle. A `Rule.referenced` set existed for a while and nothing ever read it; it was removed
  rather than left looking load-bearing.

Keep `RuleSyntaxError` and `RuleEvaluationError` distinct. `RuleSyntaxError` is a `ConfigError`:
the rule is wrong and waiting will not fix it. `RuleEvaluationError` is not: the rule is fine and
*this evaluation* could not produce a value, which the control's fail-safe handles.
