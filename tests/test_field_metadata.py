"""Guards on the per-field metadata every read tool, resource and generated document
is built from - ``MCP_FIELD_METADATA`` on each ``DataHandler`` subclass.

Coverage of it used to rest on prose in UNITS.md asking whoever added a source to keep
the two in step. That is the shape of thing this module replaces: a field with no
metadata is absent from ``get_documentation`` entirely and appears in ``list_fields``
with no unit and no kind, which is a silently incomplete answer rather than a visible
gap, and nobody notices until a chart is already wrong.

What is checked, and why each is a guard rather than a preference:

* Every declared entry says something - a unit, coded values, or a description. Not all
  three, and deliberately not "a unit" alone: a diagnostic flag, a text label and a
  status code genuinely have no unit, so demanding one would invite a made-up unit, and
  demanding prose everywhere would invite filler written to satisfy CI. Saying *nothing*
  is the only failure here.
* Every declared entry says how it may be aggregated. This is the one fact that cannot
  be recovered from the value: taking the mean of a cumulative counter produces a
  plausible chart that means nothing, and no unit, type or coded value distinguishes
  those fields from an instantaneous reading.
* No description merely restates its field name. One that does costs context on every
  detailed call and conveys nothing, so a self-describing field
  (``temperature_2m``, ``download``) is expected to carry none at all.
* UNITS.md and the metadata agree about every unit and every coded value.

The UNITS.md check is deliberately **one-way** - metadata implies a UNITS.md entry,
never the reverse. That file legitimately carries rows with no field-keyed counterpart:
Hue's rows are by device class, because its field keys are the operator's own device
names, and carbon intensity's ``gen_<fuel>`` is a pattern rather than a key. A reverse
check would fail on both, and the exclusion list that fixed it would be the thing
nobody maintained.

It compares units and coded values only, **never prose**. The MCP ``description`` and
UNITS.md's Notes column are written for different readers and are deliberately not the
same text: Notes carry caveats, conditional-collection rules and disagreements with
vendor documentation for a human maintainer, where a description exists solely to let a
model pick the right field. Neither is derived from the other, so comparing them would
only force them to converge on whichever reader was served worse.
"""

import pathlib
import re

import pytest

from toinflux.general import known_sources, source_class
from toinflux.mcp_read import FIELD_KINDS

UNITS_MD = pathlib.Path(__file__).resolve().parent.parent / "UNITS.md"

# A section heading naming its source in backticks, e.g. "## MyEnergi Zappi (`zappi`)".
_HEADING_RE = re.compile(r"^## .*\(`([^`]+)`\)\s*$")

# A backticked token inside a table cell - how UNITS.md writes a field key.
_BACKTICKED_RE = re.compile(r"`([^`]+)`")

# A table row's separator line, which carries no content.
_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")


def _sections():
    """Split UNITS.md into ``{source name: [lines]}`` by its ``## `` headings.

    :return: dict of source name to the lines under its heading
    """
    sections = {}
    current = None
    for line in UNITS_MD.read_text(encoding="utf-8").splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            current = heading.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _rows(lines):
    """Yield each table row in a section as its list of stripped cells.

    Separator rows are skipped; a header row is not, since it is indistinguishable from
    a content row by shape alone and matching "Field"/"Unit" as a key would simply never
    happen.

    :param lines: the section's lines
    :return: iterator of cell lists
    """
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|") or _SEPARATOR_RE.match(stripped):
            continue
        yield [cell.strip() for cell in stripped.strip("|").split("|")]


def _unit_cells(lines):
    """Map each documented field key to the unit cells of the rows naming it.

    A row may name several keys sharing one unit (``` `ectp1`, `ectp2`, `ectp3` ```), and
    a key may appear in more than one row, so the value is a list.

    :param lines: the section's lines
    :return: {field key: [unit cell text]}
    """
    out = {}
    for cells in _rows(lines):
        unit = cells[1] if len(cells) > 1 else ""
        for key in _BACKTICKED_RE.findall(cells[0]):
            out.setdefault(key, []).append(unit)
    return out


def _code_table(lines, field):
    """Parse the code table documenting one field's numeric values.

    Found by the line introducing it - the one naming the field in backticks alongside
    the word "codes" - and read as the *first* table after it. Anchored on that line
    rather than on position, so an edit that adds a paragraph between the two does not
    silently start reading the wrong table.

    Stopping at the end of that one table is load-bearing, and was found by this check
    failing: Nuki documents ``stateValue`` and ``doorsensorStateValue`` in consecutive
    tables, and reading to the end of the section merged them - so ``stateValue``'s code
    1 came back as the door sensor's "deactivated" and every shared code was reported as
    a disagreement that did not exist.

    :param lines: the section's lines
    :param field: the field key whose codes are wanted
    :return: {code: label}, empty when no such table is documented
    """
    marker = f"`{field}` codes"
    start = next((i for i, line in enumerate(lines) if marker in line), None)
    if start is None:
        return {}
    codes = {}
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            # Blank lines before the table are skipped; anything else after it ends it.
            if codes:
                break
            continue
        cells = next(_rows([line]), None)
        if cells and len(cells) > 1 and cells[0].isdigit():
            codes[int(cells[0])] = cells[1]
    return codes


def _declared():
    """Every declared field metadata entry, as ``(source, field, meta)`` triples.

    :return: list of triples, sorted for a stable failure order
    """
    out = []
    for source in known_sources():
        for field, meta in sorted(source_class(source).MCP_FIELD_METADATA.items()):
            out.append((source, field, meta))
    return out


DECLARED = _declared()
SECTIONS = _sections()

# Every source that declares any metadata at all, so a source shipping a table with no
# UNITS.md section behind it fails once rather than once per field.
DECLARING_SOURCES = sorted({source for source, _, _ in DECLARED})


def _ids(triples):
    """pytest ids of the form "zappi.che", so a failure names the field directly."""
    return [f"{source}.{field}" for source, field, _ in triples]


class TestEveryEntrySaysSomething:
    """An entry that carries no unit, no coded values and no description is a field key
    with a metadata entry that tells a caller nothing it did not already have."""

    @pytest.mark.parametrize("source,field,meta", DECLARED, ids=_ids(DECLARED))
    def test_entry_carries_a_unit_codes_or_a_description(self, source, field, meta):
        assert meta.get("unit") or meta.get("codes") or meta.get("description"), (
            f"{source}.{field} declares metadata that says nothing - give it a unit, a codes "
            f"map, or a description, or remove the entry"
        )

    @pytest.mark.parametrize("source,field,meta", DECLARED, ids=_ids(DECLARED))
    def test_entry_says_how_it_may_be_aggregated(self, source, field, meta):
        # Not derivable from the value, and the failure it prevents is invisible: a mean
        # of a cumulative counter is a number, drawn on a chart, that means nothing.
        assert meta.get("kind") in FIELD_KINDS, (
            f"{source}.{field} declares kind {meta.get('kind')!r}; it must be one of " f"{sorted(FIELD_KINDS)}"
        )

    def test_the_declared_surface_has_not_silently_shrunk(self):
        # A ratchet, not a target. Deleting entries to make the checks above pass is the
        # one way to satisfy them while making the answer worse, and it would otherwise
        # leave no trace.
        assert len(DECLARED) >= 56, f"only {len(DECLARED)} field metadata entries declared, down from 56"


class TestDescriptionsEarnTheirBytes:
    """A description is loaded on every detailed call, so one that restates the field
    name is worse than none: it costs context and conveys nothing."""

    DESCRIBED = [(s, f, m) for s, f, m in DECLARED if m.get("description")]

    @pytest.mark.parametrize("source,field,meta", DESCRIBED, ids=_ids(DESCRIBED))
    def test_description_says_more_than_the_field_name(self, source, field, meta):
        words = set(re.findall(r"[a-z0-9]+", meta["description"].lower()))
        from_name = set(re.findall(r"[a-z0-9]+", field.lower()))
        # Splitting a camelCase or snake_case key into its own words first, so
        # "doorsensorStateValue" cannot be "described" as "door sensor state value".
        from_name |= set(re.findall(r"[a-z0-9]+", re.sub(r"(?<!^)(?=[A-Z])", " ", field).lower()))
        assert words - from_name, (
            f"{source}.{field}'s description only restates its name; say what the name "
            f"cannot, or drop the description"
        )

    @pytest.mark.parametrize("source,field,meta", DESCRIBED, ids=_ids(DESCRIBED))
    def test_description_is_a_sentence(self, source, field, meta):
        # One voice across the advertised surface: every description a model reads is a
        # capitalised sentence ending in a full stop, so the generated reference and the
        # tool payload do not mix styles.
        description = meta["description"]
        assert description[0].isupper(), f"{source}.{field}'s description should start with a capital"
        assert description.endswith("."), f"{source}.{field}'s description should end with a full stop"


class TestUnitsDocumentationAgrees:
    """UNITS.md is what a human reads and the metadata is what the model reads. They are
    written for different readers, so they cannot be generated from one another - but
    they must not disagree about a fact."""

    @pytest.mark.parametrize("source", DECLARING_SOURCES)
    def test_every_declaring_source_has_a_units_section(self, source):
        assert source in SECTIONS, f"{source} declares field metadata but UNITS.md has no `{source}` section"

    @pytest.mark.parametrize("source,field,meta", DECLARED, ids=_ids(DECLARED))
    def test_declared_field_is_documented(self, source, field, meta):
        documented = _unit_cells(SECTIONS.get(source, []))
        assert field in documented, f"{source}.{field} is declared in MCP_FIELD_METADATA but absent from UNITS.md"

    @pytest.mark.parametrize("source,field,meta", DECLARED, ids=_ids(DECLARED))
    def test_declared_unit_appears_in_the_documented_unit_cell(self, source, field, meta):
        unit = meta.get("unit")
        if not unit:
            # No unit declared is not a disagreement: UNITS.md says "numeric status code"
            # or "bool" for exactly these fields, which is prose, not a unit.
            pytest.skip("no unit declared")
        cells = _unit_cells(SECTIONS.get(source, [])).get(field, [])
        assert any(unit in cell for cell in cells), (
            f"{source}.{field} declares unit {unit!r}, which appears in none of its UNITS.md " f"unit cells {cells}"
        )

    CODED = [(s, f, m) for s, f, m in DECLARED if m.get("codes")]

    @pytest.mark.parametrize("source,field,meta", CODED, ids=_ids(CODED))
    def test_every_declared_code_is_documented_with_the_same_label(self, source, field, meta):
        documented = _code_table(SECTIONS.get(source, []), field)
        assert documented, f"{source}.{field} declares coded values but UNITS.md documents no `{field}` codes table"
        mismatched = {
            code: (label, documented.get(code))
            for code, label in meta["codes"].items()
            if documented.get(code) != label
        }
        assert not mismatched, (
            f"{source}.{field}: UNITS.md and MCP_FIELD_METADATA disagree about these codes "
            f"(declared, documented): {mismatched}"
        )
