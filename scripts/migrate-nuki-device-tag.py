#!/usr/bin/env python3
"""Migrate pre-5.3 Nuki data to the per-lock `device` tag.

Before 5.3 every Nuki lock's state was flattened into one InfluxDB point per collection
cycle, with field keys built as the lock's name followed by the field name -
``Front_Door_stateValue``. The lock was encoded in the field key, so it could not be
queried as a dimension. Since 5.3 each lock is its own point tagged ``device=<lock>`` with
bare field keys. This rewrites the old points into the new shape so the history stays
usable.

RUN THIS BY HAND. It is deliberately not invoked by the package or the service:

  * postinst runs unattended during unattended-upgrades, which is the worst possible
    trigger for rewriting data.
  * Several collector hosts can share one database, so an upgrade-triggered run would fire
    once per host and race on the same measurement.
  * The service may not own the database - a remote InfluxDB, or a read-scoped credential.

It reads no configuration and no credentials of its own, on any install type. On a packaged
install the credentials live in systemd-creds and materialise only for the service, so a
hand-run script would not see them anyway - but the choice is deliberate and universal
rather than incidental. Falling back to reading settings.yaml where it happens to be
readable would mean the safeguard existed on packaged installs and not in a source
checkout: an organisational boundary that depends on how you installed, not a real one.
Supplying the credential every time makes "someone consciously authorised this rewrite" a
property of the tool.

TWO PHASES, separately invoked. The rewrite is non-destructive by construction, because the
new format writes to different series - old and new coexist and nothing is lost by phase 1.
Deleting the old series is phase 2, driven by a manifest phase 1 writes, so it can only
remove series phase 1 confirmed it had carried across. Backing out means not running
phase 2.

  Phase 1, dry run:   migrate-nuki-device-tag.py rewrite --url URL --database DB --dry-run
  Phase 1:            migrate-nuki-device-tag.py rewrite --url URL --database DB --manifest FILE
  Phase 2:            migrate-nuki-device-tag.py delete  --url URL --database DB --manifest FILE

Read UPGRADING.md before running either phase.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"

import argparse
import getpass
import json
import sys
from collections import Counter

import requests

# The field names a pre-5.3 Nuki collector could write, which is what makes splitting an old
# key deterministic. Deliberately a superset of the current collector's own KNOWN_STATE_FIELDS
# and *not* imported from toinflux.nuki: this script migrates data written by older releases,
# so it must describe every field set those releases wrote, not the one the collector writes
# today. A test asserts it stays a superset, so it cannot silently fall behind.
#
# LEGACY_FIELDS are no longer written by any current collector but still hold real history, so
# they must migrate rather than halt the run. Found by listing the field keys of a real live
# database rather than by reading the current source: `stateName`/`doorsensorStateName` are the
# text state names an early release wrote before the numeric stateValue rename, and they still
# carry years of points. Omitting them made the migration halt on the actual data it exists to
# rescue - and had the halt not been there, phase 2 would have deleted them. This is exactly
# why the project's migration rule says to test against real data from the previous release
# rather than a fixture matched to the migration's own assumptions.
LEGACY_FIELDS = (
    "doorsensorStateName",
    "stateName",
)

CURRENT_FIELDS = (
    "batteryCharging",
    "batteryChargeState",
    "batteryCritical",
    "connected",
    "deviceType",
    "doorsensorBatteryCritical",
    "doorsensorState",
    "doorsensorStateValue",
    "firmware",
    "keypadBatteryCritical",
    "mode",
    "name",
    "ringactionTimestamp",
    "serverConnected",
    "state",
    "stateValue",
    "timestamp",
)

KNOWN_FIELDS = tuple(sorted(CURRENT_FIELDS + LEGACY_FIELDS))

# Fields whose values are text and must never be shape-cast back to numbers. An InfluxDB
# field's type is fixed by its first write, so a firmware of "4.0" rewritten as a float would
# poison the field type against every later real write - the exact bug the collector's own
# STRING_FIELDS exists to prevent. Re-inferring types by shape would reintroduce it here.
# The legacy *Name fields are text state descriptions ("locked", "door closed"), so they
# belong here for the same reason - confirmed against real values, not assumed.
STRING_FIELDS = frozenset({"firmware", "timestamp", "ringactionTimestamp", "stateName", "doorsensorStateName", "name"})

MEASUREMENT = "nuki"
CHUNK = 500


class MigrationError(Exception):
    """A problem that must stop the migration rather than be worked around."""


def split_field_key(key):
    """Split a pre-5.3 field key into its lock label and field name.

    The suffix must be preceded by an underscore, which is the load-bearing part: the old keys
    were always built as ``<lock>_<field>``, so a key merely *ending* in a field name -
    ``MyLockstate`` - is not one of them and must halt rather than be split three characters
    short of its real name. ``rsplit("_", 1)`` would be wrong for the opposite reason: it would
    cut a lock name containing underscores, which every one of them does.

    Matches the *longest* candidate. Note this cannot currently change any answer and no test
    can kill it: two known fields could only both match one key if one ended with
    ``_<the other>``, and no member contains an underscore at all - the invariant
    ``test_no_known_field_contains_an_underscore`` pins, and the real reason the split is
    exact. Longest-match is kept as the guard that keeps it exact if an underscored field name
    is ever added, not because it is exercised today. camelCase is what makes the set
    unambiguous even so: ``keypadBatteryCritical`` does not end with ``_batteryCritical`` and
    ``doorsensorStateValue`` does not end with ``_stateValue``, so a case-insensitive
    comparison would break the split.

    :param key: the old field key
    :type key: str
    :return: (label, field)
    :rtype: tuple
    :raises MigrationError: the key ends with no known field name. Halting is the point: a
        skipped key is data silently left behind, and it would look like success
    """
    best = None
    for field in KNOWN_FIELDS:
        if key.endswith("_" + field) and (best is None or len(field) > len(best)):
            best = field
    if best is None:
        raise MigrationError(
            f"cannot split field key {key!r}: it does not end with any field name a pre-5.3 "
            f"Nuki collector wrote. Refusing to continue rather than skip it and report success"
        )
    label = key[: -(len(best) + 1)]
    if not label:
        raise MigrationError(f"field key {key!r} has no lock name before {best!r}")
    return label, best


def line_protocol_value(field, value):
    """Render a value for line protocol exactly as the collector does.

    **Numbers are written bare, with no ``i`` suffix**, so they land as InfluxDB's float
    field type. This is not a stylistic choice and getting it wrong is not cosmetic: a
    field's type is fixed by its first write, and these fields are already established as
    float in every existing database. Writing ``1i`` here made the migration establish them
    as integer instead, after which every subsequent write from the live collector failed
    with a 400 type conflict - so the migration would have broken the running collector.
    Caught by writing both the migration's output and the collector's own output to one real
    InfluxDB, which is the only way the conflict shows itself.

    The collector's ``_format_field_value()`` says the same thing in its own docstring. This
    duplicates it rather than importing it, because the script has to run standalone under a
    system Python that may not have ``toinflux`` importable at all - and a test asserts the
    two agree for every value shape, so the duplication cannot drift silently.

    One deliberate divergence from the collector: a value that arrives numeric for a field the
    collector holds as text is still written quoted. InfluxDB reports the type it stored, so
    this cannot happen for data any current release wrote - but if it ever did, writing it bare
    would put a float into the same series the collector writes strings to, and the collector's
    next write would fail with the 400 type conflict described above. The stricter answer is
    the safe one, and it is why ``field`` is a parameter at all.

    :param field: the bare field name, to decide whether it must stay text
    :type field: str
    :param value: the value as InfluxDB returned it
    :return: the line protocol representation
    :rtype: str
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if field in STRING_FIELDS or isinstance(value, str):
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return str(value)


def escape_tag(value):
    """Escape a tag value for line protocol, refusing what cannot be escaped."""
    if any(char in value for char in (chr(10), chr(13))):
        raise MigrationError(
            f"lock name {value!r} contains a newline, which cannot appear in a tag value - "
            f"a newline is what separates points"
        )
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def escape_influxql_string(value):
    """Escape a value for use inside an InfluxQL single-quoted string literal.

    The delete phase names its target in a ``WHERE "host" = '...'`` predicate, and the value
    comes from the database's own tag values via the manifest - not from a trusted constant.
    ``escape_key_or_tag_value()`` (which the collector used when *writing* that tag) escapes
    commas, equals signs, spaces and backslashes, but **not single quotes**, so a
    ``mqtt.broker_host`` containing one reaches the tag intact. Verified against InfluxDB 1.8:
    the unescaped predicate returns ``400 error parsing query: found ker, expected ;``, so the
    delete could never succeed and that series could never be removed by any supported path.

    Escaping is required rather than merely tidy. The observed failure is a rejected statement,
    but that is InfluxDB's parser declining to guess - relying on every hostile value happening
    to fail closed is not a property worth depending on for an irreversible delete, and the
    manifest is a file an operator can hand-edit.

    :param value: the literal's contents
    :type value: str
    :return: the escaped contents, without the surrounding quotes
    :rtype: str
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def escape_influxql_identifier(value):
    """Escape a value for use inside an InfluxQL double-quoted identifier.

    The same exposure as ``escape_influxql_string()`` one layer over: the rewrite phase selects
    the old field keys by name, and those names come from the database via ``SHOW FIELD KEYS``.
    ``escape_key_or_tag_value()`` does not escape double quotes when *writing* a field key, so a
    lock named ``Front"Door`` produces the field key ``Front"Door_stateValue`` verbatim.
    Verified against InfluxDB 1.8: ``SELECT "Front"Door_stateValue" FROM "nuki"`` returns
    ``400 error parsing query``, so the migration failed outright for that install, while the
    escaped form returns the row.

    Found by sweeping for the pattern after the string-literal case was raised, rather than
    waiting for the second instance to be reported separately.

    :param value: the identifier
    :type value: str
    :return: the escaped identifier, without the surrounding quotes
    :rtype: str
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


class Influx:
    """The InfluxDB calls this migration needs, over one session.

    Deliberately minimal and standalone: importing the project's own read/write plumbing
    would drag in settings loading and credential substitution, which this script exists not
    to use.
    """

    def __init__(self, url, database, credential, verify=True, timeout=60):
        self.url = url.rstrip("/")
        self.database = database
        self.session = requests.Session()
        self.verify = verify
        self.timeout = timeout
        self._auth = None
        self._headers = {}
        if credential:
            if ":" in credential:
                user, _, password = credential.partition(":")
                self._auth = (user, password)
            else:
                self._headers = {"Authorization": f"Token {credential}"}

    def query(self, statement):
        """Run one InfluxQL statement and return ``(columns, values)`` across **every** series.

        Reading only the first series is a data-loss bug waiting to happen: none of the
        statements here use ``GROUP BY``, and InfluxDB merges tag sets into one series for those
        (verified on 1.8), but that is a property of the statements rather than of this method,
        and a future statement or version that split them would silently migrate a subset and
        report success. Columns must match across series - for these statements they always do,
        and a mismatch halts rather than being stitched together on a guess.

        :param statement: the InfluxQL statement
        :type statement: str
        :return: (columns, rows) with the rows of every series concatenated
        :rtype: tuple
        :raises MigrationError: InfluxDB rejected the statement, or its series disagree on
            columns
        """
        response = self.session.get(
            f"{self.url}/query",
            params={"db": self.database, "q": statement, "epoch": "ns"},
            auth=self._auth,
            headers=self._headers,
            verify=self.verify,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        columns = []
        rows = []
        for result in payload.get("results", []):
            if result.get("error"):
                raise MigrationError(f"InfluxDB rejected {statement!r}: {result['error']}")
            for series in result.get("series", []):
                these = series.get("columns", [])
                if not columns:
                    columns = these
                elif these != columns:
                    raise MigrationError(
                        f"InfluxDB returned series with differing columns for {statement!r} "
                        f"({columns} vs {these}). Refusing to guess how to combine them"
                    )
                rows.extend(series.get("values", []) or [])
        return columns, rows

    def write(self, lines):
        """Write a batch of line protocol points at nanosecond precision."""
        if not lines:
            return
        response = self.session.post(
            f"{self.url}/write",
            params={"db": self.database, "precision": "ns"},
            data="\n".join(lines).encode(),
            auth=self._auth,
            headers=self._headers,
            verify=self.verify,
            timeout=self.timeout,
        )
        if response.status_code >= 300:
            raise MigrationError(f"write failed ({response.status_code}): {response.text[:400]}")


def old_series_hosts(influx):
    """Return the ``host`` tag values the pre-5.3 points carry.

    This is what makes the delete phase precise. Old points were written as
    ``nuki,host=<broker>`` and new ones as ``nuki,device=<lock>``, so the two are different
    series distinguished by their tags - and the old ones can be dropped by naming that tag,
    leaving the migration's own output untouched. Without this the only available delete would
    be measurement-wide, which would destroy the migrated points along with the history.
    """
    _, values = influx.query(f'SHOW TAG VALUES FROM "{MEASUREMENT}" WITH KEY = "host"')
    return sorted({row[1] for row in values if len(row) > 1 and isinstance(row[1], str)})


def old_field_keys(influx):
    """Return the pre-5.3 field keys present in the measurement.

    A key with no underscore cannot be an old prefixed key, so the bare keys the new
    collector writes are left alone - which is what makes a second run a no-op rather than a
    re-migration.
    """
    _, values = influx.query(f'SHOW FIELD KEYS FROM "{MEASUREMENT}"')
    return sorted({row[0] for row in values if row and isinstance(row[0], str) and "_" in row[0]})


def read_old_points(influx, keys):
    """Read every pre-5.3 point, as ``{timestamp: {old_key: value}}``.

    Selects the old keys explicitly rather than ``*`` so a point that also carries new bare
    keys - possible once the new collector has started writing - contributes only its old
    half, and the new half is not rewritten on top of itself.
    """
    selected = ", ".join(f'"{escape_influxql_identifier(key)}"' for key in keys)
    columns, values = influx.query(f'SELECT {selected} FROM "{MEASUREMENT}"')
    index = {name: position for position, name in enumerate(columns)}
    points = {}
    for row in values:
        stamp = row[index["time"]]
        present = {key: row[index[key]] for key in keys if row[index[key]] is not None}
        if present:
            # MERGED, never replaced. InfluxDB returns one row per tag set, so a history
            # spanning a broker change - or two collectors writing into one database - has
            # several rows at the same timestamp, each carrying a different lock's fields.
            # Assigning here dropped every row but the last: verified on 1.8, two locks under
            # two host tags at one timestamp migrated as one lock and reported success, after
            # which phase 2 would have deleted the other's history for good. Silent loss in the
            # tool whose whole purpose is not losing data.
            points.setdefault(stamp, {}).update(present)
    return points


def iter_rewritten_lines(points, counts, keys_by_label):
    """Yield new-format line protocol for each old point, one line per lock per timestamp.

    A generator rather than a list because the list was the migration's largest allocation:
    holding every rendered line alongside every read point roughly doubled peak memory for no
    benefit, since the caller writes in ``CHUNK``-sized batches and never needs an earlier line
    again. Raised in review as an OOM risk on a long-lived database. Measured against a real
    install to size it rather than guessing: ~9,200 points over 31 days, so a few megabytes
    today, and Nuki's local MQTT API needs 2022-or-later firmware, which caps any existing
    history at a few years. Streaming the lines is the cheap half of the fix.

    **The read is still bounded by the whole result set** - ``read_old_points`` holds every point
    - so this reduces peak memory rather than making it independent of database size. Paging the
    SELECT itself would do that, and is deliberately not attempted here: a ``LIMIT``-and-resume
    cursor has to cope with several rows sharing one timestamp (see ``read_old_points``), so a
    window boundary landing mid-timestamp would silently drop the rest of that group - the exact
    class of bug already found in this script twice, in the one tool that must not lose data.
    Worth doing deliberately if an install ever needs it, not bolted on.

    ``counts`` and ``keys_by_label`` are accumulated into rather than returned, since a generator
    cannot return them and the caller needs the totals only once it is exhausted.

    :param points: ``{timestamp: {old_key: value}}`` from read_old_points
    :type points: dict
    :param counts: per-lock point counter, mutated in place
    :type counts: collections.Counter
    :param keys_by_label: per-lock set of old field keys, mutated in place
    :type keys_by_label: dict
    :return: an iterator of line protocol strings
    :rtype: collections.abc.Iterator
    :raises MigrationError: a field key could not be split, or a lock name cannot be a tag
    """
    for stamp in sorted(points):
        by_label = {}
        for old_key, value in points[stamp].items():
            label, field = split_field_key(old_key)
            by_label.setdefault(label, {})[field] = value
            keys_by_label.setdefault(label, set()).add(old_key)
        for label in sorted(by_label):
            fields = ",".join(
                f"{name}={line_protocol_value(name, value)}" for name, value in sorted(by_label[label].items())
            )
            counts[label] += 1
            yield f"{MEASUREMENT},device={escape_tag(label)} {fields} {stamp}"


def rewritten_lines(points):
    """Materialise :func:`iter_rewritten_lines` for callers that want the whole list.

    Used by the tests, which assert over the complete output, and nowhere in the write path.

    :param points: ``{timestamp: {old_key: value}}`` from read_old_points
    :type points: dict
    :return: (lines, per-lock point counts, per-lock old field-key sets)
    :rtype: tuple
    """
    counts = Counter()
    keys_by_label = {}
    lines = list(iter_rewritten_lines(points, counts, keys_by_label))
    return lines, counts, keys_by_label


def _dry_run(points, counts, keys_by_label):
    """Report what the real run would write, writing nothing.

    Walks every point rather than sampling, because that is what proves every field key splits -
    the halt it can report is the whole reason to dry-run first.

    :param points: the old points that were read
    :type points: dict
    :param counts: per-lock point counter, mutated in place
    :type counts: collections.Counter
    :param keys_by_label: per-lock old field keys, mutated in place
    :type keys_by_label: dict
    :return: None
    :raises MigrationError: a field key could not be split, or a lock name cannot be a tag
    """
    sample = []
    written = 0
    for line in iter_rewritten_lines(points, counts, keys_by_label):
        written += 1
        if len(sample) < 3:
            sample.append(line)
    _report_totals(points, written, counts, keys_by_label)
    print("\nDry run - nothing written. Re-run without --dry-run to apply.")
    print("Sample of what would be written:")
    for line in sample:
        print(f"  {line}")


def _write_all(influx, points, counts, keys_by_label):
    """Write every rewritten point in ``CHUNK``-sized batches.

    Lines are generated as they are written rather than built into one list first - see
    :func:`iter_rewritten_lines`. A field key that cannot be split halts partway through, which
    is safe: phase 1 writes to different series from the ones it reads, so nothing is lost, no
    manifest is produced, and phase 2 refuses to run without one.

    :param influx: the connection to write through
    :param points: the old points that were read
    :type points: dict
    :param counts: per-lock point counter, mutated in place
    :type counts: collections.Counter
    :param keys_by_label: per-lock old field keys, mutated in place
    :type keys_by_label: dict
    :return: how many points were written
    :rtype: int
    :raises MigrationError: a write failed, a field key could not be split, or a lock name
        cannot be a tag
    """
    written = 0
    chunk = []
    for line in iter_rewritten_lines(points, counts, keys_by_label):
        chunk.append(line)
        if len(chunk) >= CHUNK:
            influx.write(chunk)
            written += len(chunk)
            chunk = []
            print(f"  written {written}")
    if chunk:
        influx.write(chunk)
        written += len(chunk)
    return written


def _report_totals(points, written, counts, keys_by_label):
    """Print what was read and produced, per lock.

    Shared by the dry run and the real run so the two can never describe the same database
    differently - the dry run's whole job is to tell you what the real run will do.

    :param points: the old points that were read
    :type points: dict
    :param written: how many new points were produced
    :type written: int
    :param counts: per-lock point counts
    :type counts: collections.Counter
    :param keys_by_label: per-lock old field keys
    :type keys_by_label: dict
    :return: None
    """
    print(f"Read {len(points)} old point(s), producing {written} new point(s):")
    for label in sorted(counts):
        print(f"  {label}: {counts[label]} point(s) from {len(keys_by_label[label])} field key(s)")


def check_manifest_writable(path):
    """Fail before writing any points if the manifest cannot be written afterwards.

    Phase 2 is driven by the manifest and refuses to run without it, so a manifest that cannot
    be written strands the operator: the points are migrated, the old ones are still there, and
    there is no supported way to remove them. Re-running phase 1 is safe (it is idempotent), but
    finding that out from a traceback after a long write is a poor way to learn it.

    Checked by actually opening the path for append rather than by inspecting permissions, which
    is the only way to be right about a read-only mount, a missing parent directory, a directory
    in the way, or an ACL.

    :param path: the manifest path phase 1 will write
    :type path: str
    :return: None
    :raises MigrationError: the path cannot be opened for writing
    """
    try:
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        raise MigrationError(
            f"the manifest path {path!r} cannot be written ({exc}). Phase 2 is driven by the "
            f"manifest and will not run without it, so this is checked before anything is "
            f"written rather than after. Pass --manifest with a writable path"
        ) from exc


def phase_rewrite(influx, args):
    """Phase 1: write the new-format points, leaving every old point in place.

    Non-destructive by construction - the new format is a different series - so this can be
    run, inspected in Grafana for as long as wanted, and only then followed by phase 2.
    Re-running it rewrites identical points over themselves, which InfluxDB treats as an
    overwrite, so it is idempotent.
    """
    keys = old_field_keys(influx)
    if not keys:
        print("No pre-5.3 Nuki field keys found - nothing to migrate.")
        return 0
    print(f"Found {len(keys)} pre-5.3 field key(s).")
    points = read_old_points(influx, keys)
    if not points:
        print("Field keys exist but no points carry them - nothing to migrate.")
        return 0
    counts = Counter()
    keys_by_label = {}
    if args.dry_run:
        _dry_run(points, counts, keys_by_label)
        return 0
    # Before the first write, not after the last: see check_manifest_writable.
    check_manifest_writable(args.manifest)
    written = _write_all(influx, points, counts, keys_by_label)
    _report_totals(points, written, counts, keys_by_label)
    manifest = {
        "measurement": MEASUREMENT,
        "database": args.database,
        "old_field_keys": keys,
        # The tags identifying the series to drop in phase 2. Recorded here, while the old
        # data is still present, because phase 2 must delete exactly what this phase carried
        # across and nothing else.
        "old_series_hosts": old_series_hosts(influx),
        "old_points": len(points),
        "new_points": written,
        "devices": {label: counts[label] for label in sorted(counts)},
    }
    try:
        with open(args.manifest, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
    except OSError as exc:
        # The pre-check above makes this unlikely, but the window between them is real (a full
        # disk, a path removed mid-run). The points are already written, so say what state
        # things are in and what fixes it, rather than raising a bare traceback.
        raise MigrationError(
            f"the points were migrated successfully, but the manifest could not be written to "
            f"{args.manifest!r} ({exc}). Nothing is lost and no old data was touched. Phase 2 "
            f"needs the manifest, so re-run this same rewrite phase with a writable --manifest "
            f"path - it is idempotent, and will rewrite the same points and produce the manifest"
        ) from exc
    print(f"\nWrote {written} point(s). Manifest: {args.manifest}")
    print("The old points are untouched. Verify in Grafana, then run the delete phase.")
    return 0


def phase_delete(influx, args):
    """Phase 2: drop the old prefixed field keys named in the manifest.

    Driven by the manifest rather than by re-deriving what to delete, so it can only remove
    what phase 1 confirmed it had carried across. Never a blanket ``DROP MEASUREMENT``: the
    new points live in the same measurement, and dropping it would destroy the migration's
    own output along with the history.
    """
    try:
        with open(args.manifest, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except OSError as exc:
        raise MigrationError(
            f"cannot read the manifest at {args.manifest} ({exc}). The delete phase is driven by "
            f"the manifest the rewrite phase wrote, so it cannot run without it"
        ) from exc
    if manifest.get("database") != args.database:
        raise MigrationError(
            f"the manifest was written for database {manifest.get('database')!r}, not "
            f"{args.database!r}. Refusing to delete from a database this manifest does not describe"
        )
    hosts = manifest.get("old_series_hosts") or []
    if not hosts:
        raise MigrationError(
            "the manifest names no old series to drop. It was written by an older version of "
            "this script, or by a rewrite that found nothing - either way there is nothing "
            "safe to delete, because deleting without naming the old series would take the "
            "migrated points with it. Re-run the rewrite phase to produce a current manifest"
        )
    print(f"About to drop the pre-5.3 series from {MEASUREMENT}, identified by host tag:")
    for host in hosts:
        print(f"  host={host}")
    print(f"\nThe manifest records {manifest.get('new_points')} migrated point(s) across")
    print(f"{len(manifest.get('devices') or {})} lock(s); those carry a device tag and are not touched.")
    if args.dry_run:
        print("\nDry run - nothing dropped.")
        return 0
    if not args.yes:
        confirm = input('\nThis cannot be undone. Type "delete" to proceed: ')
        if confirm.strip() != "delete":
            print("Aborted - nothing dropped.")
            return 1
    for host in hosts:
        # Scoped by tag, never a bare DROP SERIES FROM the measurement: the migrated points
        # live in the same measurement, so an unscoped drop would destroy this migration's own
        # output along with the history it was preserving.
        influx.query(f"""DROP SERIES FROM "{MEASUREMENT}" WHERE "host" = '{escape_influxql_string(host)}'""")
        print(f"  dropped host={host}")
    print("\nDropped the pre-5.3 series. The migrated points remain.")
    return 0


def read_credential(args):
    """Return the InfluxDB credential, read from stdin when piped else prompted for.

    Never from settings.yaml, on any install type - see the module docstring. Accepts
    ``user:password`` for v1 or a bare token for v2.
    """
    if args.no_auth:
        return None
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return getpass.getpass("InfluxDB credential (v1 'user:password', or a v2 token): ").strip()


def main(argv=None):
    """Parse arguments and run the requested phase."""
    parser = argparse.ArgumentParser(
        description="Migrate pre-5.3 Nuki data to the per-lock device tag. Read UPGRADING.md first.",
    )
    parser.add_argument("phase", choices=("rewrite", "delete"), help="which phase to run")
    parser.add_argument("--url", required=True, help="InfluxDB base URL, e.g. http://influx.example.com:8086")
    parser.add_argument("--database", required=True, help="database or bucket holding the nuki measurement")
    parser.add_argument("--manifest", default="nuki-migration-manifest.json", help="manifest path")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen and change nothing")
    parser.add_argument("--yes", action="store_true", help="skip the delete phase's confirmation prompt")
    parser.add_argument("--no-auth", action="store_true", help="the InfluxDB needs no credential")
    parser.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
    args = parser.parse_args(argv)

    influx = Influx(args.url, args.database, read_credential(args), verify=not args.insecure)
    try:
        if args.phase == "rewrite":
            return phase_rewrite(influx, args)
        return phase_delete(influx, args)
    except MigrationError as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as exc:
        print(f"\nInfluxDB request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
