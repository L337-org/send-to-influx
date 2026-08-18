"""Tests for the pre-5.3 Nuki device-tag migration script.

The migration is irreversible once its delete phase runs, so these tests exist to prove
specific properties rather than to cover lines: that no data is dropped, that field types
survive, that the split cannot corrupt a lock name, and that the script halts rather than
skipping anything it does not understand.

The fixture is derived from a real pre-5.3 database - the field-key structure and the value
shapes are exactly what a live install holds, with the lock name and device ID substituted
because a real name describes the layout of a house and this is a public repo. A fixture
invented to match the migration's own assumptions would encode the same misunderstanding as
the code, which is what the project's migration rule warns about; the one used here already
caught two legacy field keys the migration would otherwise have halted on.
"""

import importlib.util
import json
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from toinflux.influx import _format_field_value
from toinflux.nuki import KNOWN_STATE_FIELDS, STATE_VALUE_FIELDS, Nuki


def _load_migration():
    """Import the migration script by path.

    It lives in ``scripts/`` with a hyphenated filename and is deliberately not a package
    module or a console entry point - a destructive one-off does not belong on ``$PATH`` - so
    it cannot be imported normally.
    """
    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate-nuki-device-tag.py"
    spec = importlib.util.spec_from_file_location("migrate_nuki_device_tag", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


# One collection cycle as a real pre-5.3 install recorded it: a named lock carrying the full
# field set, plus a second device known only by its Nuki ID because no retained `name` topic
# had arrived for it. Both shapes are present in real data and the second is the one a
# hand-written fixture would have missed.
REAL_CYCLE = {
    "Side_Gate_Lock_batteryChargeState": 51,
    "Side_Gate_Lock_batteryCharging": False,
    "Side_Gate_Lock_batteryCritical": False,
    "Side_Gate_Lock_connected": True,
    "Side_Gate_Lock_deviceType": 4,
    "Side_Gate_Lock_doorsensorBatteryCritical": False,
    "Side_Gate_Lock_doorsensorStateName": "door closed",
    "Side_Gate_Lock_doorsensorStateValue": 2,
    "Side_Gate_Lock_firmware": "3.10.7",
    "Side_Gate_Lock_keypadBatteryCritical": False,
    "Side_Gate_Lock_mode": 2,
    "Side_Gate_Lock_serverConnected": True,
    "Side_Gate_Lock_stateName": "locked",
    "Side_Gate_Lock_stateValue": 1,
    "Side_Gate_Lock_timestamp": "2026-08-18T12:10:43Z",
    "1A2B3C4D_connected": True,
    "1A2B3C4D_deviceType": 4,
}

STAMP = 1787065531000000000


def split_fields(section):
    """Split a line protocol field section into ``{key: raw value}``.

    Quote-aware rather than a plain ``split(",")``: a string field value may legitimately
    contain a comma, and a naive split would silently mangle it into extra fields - which
    would make an assertion pass or fail for the wrong reason.
    """
    fields = {}
    key = ""
    value = ""
    in_key = True
    quoted = False
    escaped = False
    for char in section:
        if in_key:
            if char == "=":
                in_key = False
            else:
                key += char
            continue
        if escaped:
            value += char
            escaped = False
        elif char == "\\":
            value += char
            escaped = True
        elif char == '"':
            value += char
            quoted = not quoted
        elif char == "," and not quoted:
            fields[key] = value
            key, value, in_key = "", "", True
        else:
            value += char
    if key:
        fields[key] = value
    return fields


def parse_line(line):
    """Parse a line protocol point into ``(header, {field: value}, timestamp)``.

    Field *order* is not part of a point's meaning, so comparing parsed points rather than
    raw strings is what lets the migration sort its fields while the collector emits them in
    insertion order without that counting as a difference.
    """
    head, _, stamp = line.rpartition(" ")
    # Split on the first *unescaped* space: a lock name containing spaces is escaped into the
    # tag value as "Side\ Gate\ Lock", so a plain partition(" ") would cut the header in half
    # and read the rest of the name as a field key.
    boundary = None
    escaped = False
    for position, char in enumerate(head):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == " ":
            boundary = position
            break
    assert boundary is not None, f"no unescaped space separating tags from fields in {line!r}"
    return head[:boundary], split_fields(head[boundary + 1 :]), int(stamp)


class TestTheTestsOwnParser:
    """The equivalence assertions are only as trustworthy as the parser behind them.

    A parser that quietly mangled a line would make those comparisons pass for the wrong
    reason, so its two hard cases are pinned here rather than assumed.
    """

    def test_a_comma_inside_a_quoted_value_is_not_a_field_separator(self):
        header, fields, stamp = parse_line('nuki,device=Gate firmware="a,b",stateValue=1 12')
        assert (header, fields, stamp) == ("nuki,device=Gate", {"firmware": '"a,b"', "stateValue": "1"}, 12)

    def test_an_escaped_space_in_a_tag_value_is_not_the_header_boundary(self):
        header, fields, stamp = parse_line("nuki,device=Side\\ Gate stateValue=1 12")
        assert (header, fields, stamp) == ("nuki,device=Side\\ Gate", {"stateValue": "1"}, 12)

    def test_an_escaped_quote_inside_a_value_does_not_end_it(self):
        _, fields, _ = parse_line('nuki,device=Gate firmware="a\\",b",stateValue=1 12')
        assert fields == {"firmware": '"a\\",b"', "stateValue": "1"}


class TestFieldSet:
    """The duplicated field set must stay ahead of the collector's own."""

    def test_covers_every_field_the_collector_writes(self):
        """A field added to the collector but not here would halt a future migration.

        The script cannot import the collector's set - it has to run standalone under a
        system Python - so this is what stops the copy drifting behind the original.
        """
        collector_fields = set(KNOWN_STATE_FIELDS) | set(STATE_VALUE_FIELDS.values())
        assert collector_fields <= set(migration.KNOWN_FIELDS), sorted(collector_fields - set(migration.KNOWN_FIELDS))

    def test_no_known_field_contains_an_underscore(self):
        """The whole split depends on this: an underscored field name would make
        ``<lock>_<field>`` ambiguous and no longer recoverable."""
        assert [field for field in migration.KNOWN_FIELDS if "_" in field] == []

    def test_legacy_fields_are_not_written_by_the_current_collector(self):
        """Documents why LEGACY_FIELDS is separate: these hold history but take no new
        points, so a reader is not left wondering whether the collector still writes them."""
        collector_fields = set(KNOWN_STATE_FIELDS) | set(STATE_VALUE_FIELDS.values())
        assert set(migration.LEGACY_FIELDS).isdisjoint(collector_fields)


class TestSplitFieldKey:
    """Recovering the lock name from the old field key."""

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("Side_Gate_Lock_stateValue", ("Side_Gate_Lock", "stateValue")),
            ("Side_Gate_Lock_doorsensorStateValue", ("Side_Gate_Lock", "doorsensorStateValue")),
            ("Side_Gate_Lock_batteryCritical", ("Side_Gate_Lock", "batteryCritical")),
            ("Side_Gate_Lock_keypadBatteryCritical", ("Side_Gate_Lock", "keypadBatteryCritical")),
            ("Side_Gate_Lock_doorsensorStateName", ("Side_Gate_Lock", "doorsensorStateName")),
            ("Side_Gate_Lock_stateName", ("Side_Gate_Lock", "stateName")),
            ("1A2B3C4D_connected", ("1A2B3C4D", "connected")),
        ],
    )
    def test_splits_real_keys(self, key, expected):
        assert migration.split_field_key(key) == expected

    @pytest.mark.parametrize(
        "key,expected",
        [
            # The camelCase pairs that make longest-suffix matching load-bearing. A
            # case-insensitive match would split these on the shorter name and corrupt the
            # lock label; keeping them here is what catches a future field name that breaks
            # the property.
            ("Lock_keypadBatteryCritical", ("Lock", "keypadBatteryCritical")),
            ("Lock_doorsensorStateValue", ("Lock", "doorsensorStateValue")),
            ("Lock_doorsensorStateName", ("Lock", "doorsensorStateName")),
            ("Lock_doorsensorBatteryCritical", ("Lock", "doorsensorBatteryCritical")),
        ],
    )
    def test_prefers_the_longest_known_suffix(self, key, expected):
        assert migration.split_field_key(key) == expected

    def test_lock_name_ending_in_a_field_name_still_splits_on_the_last_one(self):
        """A lock called "Back Door state" underscores to ``Back_Door_state``, so its
        ``connected`` field is ``Back_Door_state_connected``. The split must take
        ``connected``, leaving the name intact - this is where a wrong split would land."""
        assert migration.split_field_key("Back_Door_state_connected") == ("Back_Door_state", "connected")

    @pytest.mark.parametrize("key", ["MyLockstate", "Lockconnected", "Gatefirmware"])
    def test_a_field_name_not_preceded_by_an_underscore_halts(self, key):
        """The old keys were always built as ``<lock>_<field>``, so a key merely ending in a
        field name is not one of them. Without the underscore in the comparison these split
        three characters short and would migrate a corrupted lock name."""
        with pytest.raises(migration.MigrationError, match="does not end with any field name"):
            migration.split_field_key(key)

    def test_unknown_suffix_halts(self):
        """Skipping an unrecognised key is how data gets lost while reporting success."""
        with pytest.raises(migration.MigrationError, match="does not end with any field name"):
            migration.split_field_key("Side_Gate_Lock_somethingNew")

    def test_key_with_no_lock_name_halts(self):
        with pytest.raises(migration.MigrationError, match="no lock name before"):
            migration.split_field_key("_stateValue")


class TestValueFormatting:
    """Field types must come out of the migration exactly as the collector writes them."""

    @pytest.mark.parametrize(
        "value",
        [1, 0, 51, 4, 255, 3.5, 0.0, -1, True, False, "locked", "3.10.7", "2026-08-18T12:10:43Z", "", "door closed"],
    )
    def test_agrees_with_the_collector_for_every_value_shape(self, value):
        """The migration duplicates the collector's formatter because it must run standalone.

        This is the test that pins the duplication. It failed for every integer while the
        migration emitted the ``i`` suffix - which would have established these fields as
        InfluxDB's integer type and made every subsequent collector write fail with a 400
        type conflict, breaking the running collector rather than merely looking untidy.
        """
        assert migration.line_protocol_value("stateValue", value) == _format_field_value(value)

    @pytest.mark.parametrize("value", [1, 51, 4, 3.5])
    def test_numbers_carry_no_integer_suffix(self, value):
        """Asserted directly as well as by equivalence: if both implementations were changed
        to emit ``i`` together, the equivalence test above would still pass."""
        assert not migration.line_protocol_value("stateValue", value).endswith("i")

    @pytest.mark.parametrize("field", ["firmware", "timestamp", "ringactionTimestamp"])
    def test_the_deliberately_string_fields_stay_quoted(self, field):
        """Named explicitly because this is the failure that cannot be undone: a firmware of
        "4.0" written as a float fixes the field's type as float forever, and "3.10.7" then
        fails against it."""
        assert migration.line_protocol_value(field, "4.0") == '"4.0"'

    @pytest.mark.parametrize("field", ["stateName", "doorsensorStateName"])
    def test_the_legacy_name_fields_stay_quoted(self, field):
        assert migration.line_protocol_value(field, "locked") == '"locked"'

    @pytest.mark.parametrize("field,value", [("firmware", 4.0), ("timestamp", 20260818)])
    def test_a_numeric_value_on_a_text_field_is_still_written_as_text(self, field, value):
        """The one deliberate divergence from the collector's formatter, and the reason the
        field name is a parameter.

        InfluxDB reports the type it stored, so no current release's data reaches this - but
        writing such a value bare would put a float into the same series the collector writes
        strings to, and the collector's very next write would fail with a 400 type conflict.
        That is the failure this migration already caused once via the integer suffix; being
        stricter than the collector here is what keeps the second route to it closed.
        """
        assert migration.line_protocol_value(field, value) == f'"{value}"'
        assert _format_field_value(value) == str(value)

    def test_quotes_and_backslashes_are_escaped(self):
        assert migration.line_protocol_value("firmware", 'a"b\\c') == '"a\\"b\\\\c"'


class TestTagEscaping:
    """Lock names become tag values, and a real one contains spaces."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Side Gate Lock", "Side\\ Gate\\ Lock"),
            ("Kitchen,Hall", "Kitchen\\,Hall"),
            ("a=b", "a\\=b"),
            ("back\\slash", "back\\\\slash"),
            ("Side_Gate_Lock", "Side_Gate_Lock"),
        ],
    )
    def test_escapes_what_line_protocol_requires(self, name, expected):
        assert migration.escape_tag(name) == expected

    def test_agrees_with_the_collector(self):
        """The migration's tag escaping must match the collector's, or a lock whose name
        needs escaping would migrate into a different series from its own new points."""
        from toinflux.influx import escape_key_or_tag_value

        for name in ("Side Gate Lock", "Kitchen,Hall", "a=b", "back\\slash"):
            assert migration.escape_tag(name) == escape_key_or_tag_value(name)

    @pytest.mark.parametrize("name", ["Gate\nnuki,device=Injected fake=1", "Gate\rX"])
    def test_a_newline_in_a_lock_name_is_refused(self, name):
        """Line protocol has no escape for a newline - it is what separates points - so an
        unescaped one would turn the remainder into a second, fabricated point."""
        with pytest.raises(migration.MigrationError, match="newline"):
            migration.escape_tag(name)


class TestInfluxQLEscaping:
    """The two places a database-supplied value is interpolated into a statement.

    Neither value is a trusted constant: the host tag and the field keys both come out of the
    database, and the collector's line-protocol escaping does not cover either InfluxQL context
    (it escapes commas, equals signs, spaces and backslashes - not single or double quotes). Both
    verified against a real InfluxDB 1.8, where the unescaped forms return 400.
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("plain.example.com", "plain.example.com"),
            ("bro'ker", "bro\\'ker"),
            ("back\\slash", "back\\\\slash"),
            ("both'\\end", "both\\'\\\\end"),
        ],
    )
    def test_string_literals_are_escaped(self, value, expected):
        assert migration.escape_influxql_string(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Gate_stateValue", "Gate_stateValue"),
            ('Front"Door_stateValue', 'Front\\"Door_stateValue'),
            ("back\\slash_stateValue", "back\\\\slash_stateValue"),
        ],
    )
    def test_identifiers_are_escaped(self, value, expected):
        assert migration.escape_influxql_identifier(value) == expected

    def test_the_delete_predicate_escapes_its_host(self, tmp_path):
        """The one that matters most: this is the irreversible statement, and a host tag
        carrying a quote made it fail with a parse error, so that series could never be
        removed by any supported path."""
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                {
                    "database": "test",
                    "old_series_hosts": ["bro'ker"],
                    "new_points": 1,
                    "devices": {"Gate": 1},
                }
            )
        )
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.statements = []
        influx.query = lambda statement: (influx.statements.append(statement), ([], []))[1]
        args = type("Args", (), {"manifest": str(manifest), "database": "test", "dry_run": False, "yes": True})()
        assert migration.phase_delete(influx, args) == 0
        assert influx.statements == ["""DROP SERIES FROM "nuki" WHERE "host" = 'bro\\'ker'"""]

    def test_the_select_escapes_its_field_keys(self):
        """A lock named with a double quote produces a field key carrying it, and the
        unescaped SELECT returned 400 - the migration failed outright for that install."""
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.statements = []

        def query(statement):
            influx.statements.append(statement)
            return ["time", 'Front"Door_stateValue'], [[STAMP, 1]]

        influx.query = query
        migration.read_old_points(influx, ['Front"Door_stateValue'])
        assert influx.statements == ['SELECT "Front\\"Door_stateValue" FROM "nuki"']


class TestRewrittenLines:
    """Turning a real old cycle into the new format."""

    def test_produces_one_point_per_device(self):
        lines, counts, _ = migration.rewritten_lines({STAMP: REAL_CYCLE})
        assert len(lines) == 2
        assert dict(counts) == {"1A2B3C4D": 1, "Side_Gate_Lock": 1}

    def test_conserves_every_device_field_timestamp_triple(self):
        """The no-data-loss assertion. Counted rather than eyeballed, so a silently dropped
        device or field fails here instead of passing quietly."""
        cycles = {STAMP: REAL_CYCLE, STAMP + 300000000000: REAL_CYCLE}
        lines, _, _ = migration.rewritten_lines(cycles)

        expected = set()
        for stamp in cycles:
            for old_key in REAL_CYCLE:
                label, field = migration.split_field_key(old_key)
                expected.add((label, field, stamp))

        produced = set()
        for line in lines:
            header, fields, stamp = parse_line(line)
            label = header.split("device=", 1)[1]
            for field in fields:
                produced.add((label, field, stamp))

        assert produced == expected
        assert len(produced) == len(REAL_CYCLE) * len(cycles)

    def test_every_line_carries_the_original_timestamp(self):
        lines, _, _ = migration.rewritten_lines({STAMP: REAL_CYCLE})
        assert [line.rsplit(" ", 1)[1] for line in lines] == [str(STAMP), str(STAMP)]

    def test_all_of_one_cycles_locks_share_one_timestamp(self):
        """The snapshot must not be scattered: a query for the state at time T has to see
        every lock's reading, which is why the collector uses one timestamp per cycle too."""
        lines, _, _ = migration.rewritten_lines({STAMP: REAL_CYCLE})
        assert len({line.rsplit(" ", 1)[1] for line in lines}) == 1

    def test_is_idempotent(self):
        """Same measurement, tag set and timestamp overwrite in InfluxDB, so re-running
        phase 1 is a no-op. Asserted rather than assumed."""
        first, _, _ = migration.rewritten_lines({STAMP: REAL_CYCLE})
        second, _, _ = migration.rewritten_lines({STAMP: REAL_CYCLE})
        assert first == second

    def test_a_lock_missing_from_one_cycle_is_not_invented(self):
        """Locks come and go - a lock offline for a cycle has no fields in it, and must
        produce no point rather than an empty or a carried-forward one."""
        cycles = {STAMP: REAL_CYCLE, STAMP + 300000000000: {"1A2B3C4D_connected": True}}
        _, counts, _ = migration.rewritten_lines(cycles)
        assert dict(counts) == {"1A2B3C4D": 2, "Side_Gate_Lock": 1}

    def test_halts_on_an_unparseable_key_rather_than_migrating_the_rest(self):
        """Partial success is the dangerous outcome: phase 2 would then delete the old data
        including the part that never made it across."""
        cycle = dict(REAL_CYCLE, Side_Gate_Lock_somethingNew=1)
        with pytest.raises(migration.MigrationError):
            migration.rewritten_lines({STAMP: cycle})


class TestEquivalenceWithTheCollector:
    """The migrated history must be the same series the new collector writes.

    This is the acceptance question the whole script exists to answer: after migrating, a
    Grafana panel or an MCP query must see one continuous series per lock, not history in one
    series and new points in another.
    """

    @staticmethod
    def _collector_lines(per_device, timestamp):
        """What Nuki.send_data() actually posts, captured at the HTTP boundary."""
        settings = {
            "influx": {"url": "http://influx.example.com:8086", "user": "u", "password": "p"},
            "mqtt": {"broker_host": "mqtt.example.com"},
            "nuki": {"db": "test", "interval": 300},
        }
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Nuki("nuki")
        written = []
        with patch.object(Nuki.__mro__[2], "_post_line", side_effect=lambda line, *a, **k: written.append(line)):
            handler.send_data(data=per_device, timestamp=timestamp, use_buffer=False)
        return written

    def test_migrated_lines_match_what_the_collector_writes(self):
        """Compared as whole line-protocol strings, so the measurement, the tag, every field
        key and every field value all have to agree - not just the ones a narrower assertion
        would have thought to check."""
        per_device = {}
        for old_key, value in REAL_CYCLE.items():
            label, field = migration.split_field_key(old_key)
            per_device.setdefault(label, {})[field] = value

        seconds = STAMP // 1_000_000_000
        collector = sorted(parse_line(line) for line in self._collector_lines(per_device, seconds))
        migrated, _, _ = migration.rewritten_lines({STAMP: REAL_CYCLE})
        # Normalised for the two differences that carry no meaning: the timestamp's unit (the
        # collector writes seconds, the migration preserves the original point's nanoseconds)
        # and field order within a point. Everything else - measurement, tag, every field key
        # and every rendered value - has to agree exactly.
        migrated = sorted((header, fields, seconds) for header, fields, _ in map(parse_line, migrated))

        assert migrated == collector

    def test_a_lock_name_needing_escaping_also_matches(self):
        """Separately, because REAL_CYCLE's names are already underscored - a real lock name
        with spaces only appears once the collector stops underscoring it."""
        per_device = {"Side Gate Lock": {"stateValue": 1, "firmware": "3.10.7"}}
        seconds = STAMP // 1_000_000_000
        collector = self._collector_lines(per_device, seconds)
        migrated, _, _ = migration.rewritten_lines(
            {STAMP: {"Side Gate Lock_stateValue": 1, "Side Gate Lock_firmware": "3.10.7"}}
        )
        assert [(header, fields, seconds) for header, fields, _ in map(parse_line, migrated)] == [
            parse_line(line) for line in collector
        ]


class TestReadOldPoints:
    """Reading the old points out of InfluxDB."""

    @staticmethod
    def _influx(columns, values):
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.query = lambda statement: (columns, values)
        return influx

    def test_drops_null_fields_rather_than_writing_them(self):
        """InfluxDB pads a row with nulls for a field that point does not carry - a lock
        added later, or a legacy field no longer written. Writing them back as values would
        fabricate readings."""
        influx = self._influx(["time", "a_stateValue", "b_stateValue"], [[STAMP, 1, None]])
        assert migration.read_old_points(influx, ["a_stateValue", "b_stateValue"]) == {STAMP: {"a_stateValue": 1}}

    def test_skips_a_row_with_no_old_fields_at_all(self):
        """A point written entirely by the new collector must not become an empty old point."""
        influx = self._influx(["time", "a_stateValue"], [[STAMP, None]])
        assert migration.read_old_points(influx, ["a_stateValue"]) == {}

    def test_keeps_a_false_value(self):
        """``batteryCritical=False`` is real data, and a truthiness filter would drop it -
        along with every zero reading."""
        influx = self._influx(["time", "a_batteryCritical"], [[STAMP, False]])
        assert migration.read_old_points(influx, ["a_batteryCritical"]) == {STAMP: {"a_batteryCritical": False}}

    def test_keeps_a_zero_value(self):
        influx = self._influx(["time", "a_stateValue"], [[STAMP, 0]])
        assert migration.read_old_points(influx, ["a_stateValue"]) == {STAMP: {"a_stateValue": 0}}


class TestMultipleRowsAtOneTimestamp:
    """The silent-loss case: several rows sharing a timestamp.

    InfluxDB returns one row per tag set, so a history spanning a broker change - or two
    collectors writing into one database, which is what this whole epic is about - has several
    rows at the same timestamp, each carrying a different lock's fields. Verified on a real
    InfluxDB 1.8: two locks under two host tags at one timestamp came back as two rows.
    """

    @staticmethod
    def _influx(columns, values):
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.query = lambda statement: (columns, values)
        return influx

    def test_rows_sharing_a_timestamp_are_merged_not_replaced(self):
        """Assigning instead of merging dropped every row but the last, migrated one lock,
        and reported success - after which phase 2 would have deleted the other lock's
        history for good."""
        influx = self._influx(
            ["time", "Front_stateValue", "Back_stateValue"],
            [[STAMP, None, 3], [STAMP, 1, None]],
        )
        points = migration.read_old_points(influx, ["Front_stateValue", "Back_stateValue"])
        assert points == {STAMP: {"Front_stateValue": 1, "Back_stateValue": 3}}

    def test_both_locks_survive_into_the_written_lines(self):
        """The end the operator actually cares about: two points, not one."""
        influx = self._influx(
            ["time", "Front_stateValue", "Back_stateValue"],
            [[STAMP, None, 3], [STAMP, 1, None]],
        )
        points = migration.read_old_points(influx, ["Front_stateValue", "Back_stateValue"])
        _, counts, _ = migration.rewritten_lines(points)
        assert dict(counts) == {"Front": 1, "Back": 1}


class TestQueryReadsEverySeries:
    """``Influx.query()`` must not stop at the first series.

    None of this script's statements use ``GROUP BY``, and InfluxDB merges tag sets for those,
    so today only one series comes back. That is a property of the statements, not of the
    method - a future statement or version that split them would migrate a subset and report
    success, which is the failure mode this script exists to prevent.
    """

    @staticmethod
    def _influx(payload):
        influx = migration.Influx("http://influx.example.com:8086", "test", None)

        class Response:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return payload

        influx.session = type("S", (), {"get": staticmethod(lambda *a, **k: Response())})()
        return influx

    def test_rows_from_every_series_are_returned(self):
        influx = self._influx(
            {
                "results": [
                    {
                        "series": [
                            {"columns": ["time", "v"], "values": [[1, 10]]},
                            {"columns": ["time", "v"], "values": [[2, 20]]},
                        ]
                    }
                ]
            }
        )
        assert influx.query("SELECT 1") == (["time", "v"], [[1, 10], [2, 20]])

    def test_series_disagreeing_on_columns_halts(self):
        """Stitching mismatched columns together would corrupt the row indexing silently, so
        this halts - the same choice as an unparseable field key."""
        influx = self._influx(
            {
                "results": [
                    {
                        "series": [
                            {"columns": ["time", "v"], "values": [[1, 10]]},
                            {"columns": ["time", "other"], "values": [[2, 20]]},
                        ]
                    }
                ]
            }
        )
        with pytest.raises(migration.MigrationError, match="differing columns"):
            influx.query("SELECT 1")

    def test_an_error_result_still_halts(self):
        influx = self._influx({"results": [{"error": "no such measurement"}]})
        with pytest.raises(migration.MigrationError, match="no such measurement"):
            influx.query("SELECT 1")

    def test_no_series_is_empty_not_an_error(self):
        influx = self._influx({"results": [{}]})
        assert influx.query("SELECT 1") == ([], [])


class TestOldFieldKeys:
    """Selecting which keys are old ones."""

    @staticmethod
    def _influx(values):
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.query = lambda statement: (["fieldKey", "fieldType"], values)
        return influx

    def test_ignores_the_bare_keys_the_new_collector_writes(self):
        """What makes a second run a no-op rather than a re-migration: a key with no
        underscore cannot be an old prefixed key."""
        influx = self._influx([["stateValue", "float"], ["a_stateValue", "float"], ["firmware", "string"]])
        assert migration.old_field_keys(influx) == ["a_stateValue"]


class TestPhaseRewrite:
    """Phase 1's own guards, as distinct from the transform."""

    @staticmethod
    def _influx(points, manifest_path):
        """An Influx that reports one old point and records what gets written."""
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.written = []

        def query(statement):
            if "SHOW FIELD KEYS" in statement:
                return ["fieldKey", "fieldType"], [["Gate_stateValue", "float"]]
            if "SHOW TAG VALUES" in statement:
                return ["key", "value"], [["host", "mqtt.example.com"]]
            return ["time", "Gate_stateValue"], [[STAMP, 1]]

        influx.query = query
        influx.write = influx.written.append
        return influx

    @staticmethod
    def _args(manifest, dry_run=False):
        return type("Args", (), {"manifest": str(manifest), "database": "test", "dry_run": dry_run})()

    def test_every_point_survives_chunk_boundaries(self, tmp_path):
        """Lines are generated and written in CHUNK-sized batches rather than materialised as
        one list, so a boundary is a place a point could be dropped or written twice.

        Sized to cross several boundaries. Verified against a real InfluxDB 1.8 as well - 1300
        old points across two locks produced exactly 2600, with the manifest agreeing.
        """
        manifest = tmp_path / "m.json"
        old_points = {
            STAMP + index * 300_000_000_000: {"Front_stateValue": 1, "Back_stateValue": 3}
            for index in range(migration.CHUNK * 2 + 7)
        }
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.batches = []

        def query(statement):
            if "SHOW FIELD KEYS" in statement:
                return ["fieldKey", "fieldType"], [["Front_stateValue", "float"], ["Back_stateValue", "float"]]
            if "SHOW TAG VALUES" in statement:
                return ["key", "value"], [["host", "mqtt.example.com"]]
            columns = ["time", "Back_stateValue", "Front_stateValue"]
            return columns, [[stamp, 3, 1] for stamp in sorted(old_points)]

        influx.query = query
        influx.write = influx.batches.append

        assert migration.phase_rewrite(influx, self._args(manifest)) == 0

        # Every batch bar the last is exactly CHUNK, and nothing is written twice.
        assert all(len(batch) == migration.CHUNK for batch in influx.batches[:-1])
        written = [line for batch in influx.batches for line in batch]
        expected = len(old_points) * 2  # two locks per old point
        assert len(written) == expected
        assert len(set(written)) == expected, "a line was written twice across a chunk boundary"

        recorded = json.loads(manifest.read_text())
        assert recorded["new_points"] == expected
        assert recorded["old_points"] == len(old_points)
        assert recorded["devices"] == {"Back": len(old_points), "Front": len(old_points)}

    def test_the_dry_run_reports_the_same_totals_as_a_real_run(self, tmp_path):
        """The dry run's whole job is to say what the real run will do, so the two must not be
        able to describe the same database differently - they share one report function."""
        old_points = {STAMP: {"Front_stateValue": 1, "Back_stateValue": 3}}
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.batches = []

        def query(statement):
            if "SHOW FIELD KEYS" in statement:
                return ["fieldKey", "fieldType"], [["Front_stateValue", "float"], ["Back_stateValue", "float"]]
            if "SHOW TAG VALUES" in statement:
                return ["key", "value"], [["host", "mqtt.example.com"]]
            return ["time", "Back_stateValue", "Front_stateValue"], [[STAMP, 3, 1]]

        influx.query = query
        influx.write = influx.batches.append

        counts = Counter()
        keys = {}
        migration._dry_run(old_points, counts, keys)
        assert influx.batches == []
        assert dict(counts) == {"Front": 1, "Back": 1}

        manifest = tmp_path / "m.json"
        assert migration.phase_rewrite(influx, self._args(manifest)) == 0
        recorded = json.loads(manifest.read_text())
        assert recorded["devices"] == dict(counts)

    def test_writes_the_points_and_the_manifest(self, tmp_path):
        manifest = tmp_path / "m.json"
        influx = self._influx(None, manifest)
        assert migration.phase_rewrite(influx, self._args(manifest)) == 0
        assert influx.written
        recorded = json.loads(manifest.read_text())
        assert recorded["database"] == "test"
        assert recorded["old_series_hosts"] == ["mqtt.example.com"]
        assert recorded["devices"] == {"Gate": 1}

    def test_an_unwritable_manifest_stops_before_anything_is_written(self, tmp_path):
        """The gap this closes: the manifest used to be written after every point, so an
        unwritable path left the data migrated with no manifest - and phase 2 refuses to run
        without one, so the old points could never be removed. Failing first means the operator
        can simply fix the path and re-run.
        """
        unwritable = tmp_path / "no-such-directory" / "m.json"
        influx = self._influx(None, unwritable)
        with pytest.raises(migration.MigrationError, match="cannot be written"):
            migration.phase_rewrite(influx, self._args(unwritable))
        assert influx.written == [], "points were written despite the manifest being unwritable"

    def test_a_dry_run_writes_nothing_and_needs_no_writable_manifest(self, tmp_path):
        """A dry run must not be blocked by the manifest check - it produces no manifest."""
        unwritable = tmp_path / "no-such-directory" / "m.json"
        influx = self._influx(None, unwritable)
        assert migration.phase_rewrite(influx, self._args(unwritable, dry_run=True)) == 0
        assert influx.written == []

    def test_nothing_to_migrate_is_reported_not_failed(self, tmp_path):
        """An install with no pre-5.3 data - or one already migrated - must exit cleanly, since
        the operator is told to run this and may well have nothing to convert."""
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.written = []
        influx.query = lambda statement: (["fieldKey", "fieldType"], [["stateValue", "float"]])
        influx.write = influx.written.append
        assert migration.phase_rewrite(influx, self._args(tmp_path / "m.json")) == 0
        assert influx.written == []


class TestPhaseDelete:
    """Phase 2 is the irreversible one, so its guards are the ones that matter."""

    @staticmethod
    def _args(tmp_path, manifest, database="test", **kwargs):
        path = tmp_path / "manifest.json"
        if manifest is not None:
            path.write_text(json.dumps(manifest))
        return type(
            "Args",
            (),
            {"manifest": str(path), "database": database, "dry_run": False, "yes": True, **kwargs},
        )()

    @staticmethod
    def _recording_influx():
        influx = migration.Influx("http://influx.example.com:8086", "test", None)
        influx.statements = []
        influx.query = lambda statement: (influx.statements.append(statement), ([], []))[1]
        return influx

    def test_refuses_without_a_manifest(self, tmp_path):
        influx = self._recording_influx()
        args = self._args(tmp_path, None)
        with pytest.raises(migration.MigrationError, match="cannot read the manifest"):
            migration.phase_delete(influx, args)
        assert influx.statements == []

    def test_refuses_a_manifest_for_a_different_database(self, tmp_path):
        """A destructive operation names its target rather than inheriting context, and the
        manifest is part of that target."""
        influx = self._recording_influx()
        args = self._args(tmp_path, {"database": "other", "old_series_hosts": ["mqtt"]})
        with pytest.raises(migration.MigrationError, match="not 'test'"):
            migration.phase_delete(influx, args)
        assert influx.statements == []

    def test_refuses_a_manifest_naming_no_old_series(self, tmp_path):
        """Without a scope there is no safe delete: the only statement left would take the
        migrated points too."""
        influx = self._recording_influx()
        args = self._args(tmp_path, {"database": "test", "old_series_hosts": []})
        with pytest.raises(migration.MigrationError, match="nothing\n?\\s*safe to delete|nothing safe"):
            migration.phase_delete(influx, args)
        assert influx.statements == []

    def test_scopes_the_drop_to_the_recorded_hosts(self, tmp_path):
        influx = self._recording_influx()
        args = self._args(
            tmp_path,
            {"database": "test", "old_series_hosts": ["mqtt.example.com"], "new_points": 4, "devices": {"a": 2}},
        )
        assert migration.phase_delete(influx, args) == 0
        assert influx.statements == ["""DROP SERIES FROM "nuki" WHERE "host" = 'mqtt.example.com'"""]

    def test_dry_run_drops_nothing(self, tmp_path):
        influx = self._recording_influx()
        args = self._args(
            tmp_path,
            {"database": "test", "old_series_hosts": ["mqtt.example.com"], "new_points": 4, "devices": {"a": 2}},
            dry_run=True,
        )
        assert migration.phase_delete(influx, args) == 0
        assert influx.statements == []

    def test_declining_the_confirmation_drops_nothing(self, tmp_path):
        """The prompt is the last guard before an irreversible delete, so anything other than
        the exact word must abort."""
        influx = self._recording_influx()
        args = self._args(
            tmp_path,
            {"database": "test", "old_series_hosts": ["mqtt.example.com"], "new_points": 4, "devices": {"a": 2}},
            yes=False,
        )
        with patch("builtins.input", return_value="yes"):
            assert migration.phase_delete(influx, args) == 1
        assert influx.statements == []
