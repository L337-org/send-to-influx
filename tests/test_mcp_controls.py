"""The read-only control tools: what they say, and what they refuse to guess.

Most of these are about the three-valued answer. "Is this control running" has a third
answer - nothing is supervising, so nothing knows - and collapsing it into `false` would
send a caller looking for a crash that never happened.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import logging
import os

import anyio
import pytest
import yaml
from mcp.server.mcpserver import MCPServer

from tests.harness.installation import conservatory
from toinflux.exceptions import ConfigError
from toinflux.exceptions import ToolParamError
from toinflux.mcp_controls import (
    _control_schema_result,
    _control_state_result,
    _delete_control_result,
    _get_control_result,
    _list_controls_result,
    _save_control_result,
    _set_enabled_result,
    register_control_tools,
)
from toinflux.supervision import ControlStatus


class _Supervising:
    """A stand-in holding exactly the statuses a test wants reported.

    Not a MagicMock: one invents a truthy value for any attribute not set, and the thing
    under test here reads attributes off whatever it is handed. A stub that answers only
    what was asked for is the point.
    """

    def __init__(self, *statuses):
        """Hold the statuses to report.

        Args:
            *statuses (ControlStatus): what ``status()`` should return
        """
        self._statuses = tuple(statuses)

    def status(self):
        """Return the statuses this stub was built with.

        Returns:
            tuple: the statuses
        """
        return self._statuses


def _status(name, **overrides):
    """Return one control status with sensible defaults.

    Args:
        name (str): the control's name
        **overrides: fields to change

    Returns:
        ControlStatus: the status
    """
    fields = {"running": True, "pid": 4321, "failures": 0, "silent_for": 2.0, "restart_in": None}
    fields.update(overrides)
    return ControlStatus(name=name, **fields)


@pytest.fixture
def stored(state_directory):
    """Write one valid control and yield the installation.

    Yields:
        Installation: with a `conservatory` control stored
    """
    state_directory.write_control(conservatory())
    yield state_directory


class TestWhetherAControlIsRunning:
    def test_nothing_supervising_is_not_the_same_as_stopped(self, stored):
        """The whole reason the supervisor is passed in rather than assumed. With controls
        switched off, or every stored control unusable, there is no supervisor at all - and
        a caller told the control is stopped would go looking for a crash."""
        result = _list_controls_result(stored.settings_file, None)
        assert result["supervisor_running"] is False
        assert result["controls"][0]["running"] is None

    def test_a_supervised_control_reports_its_process(self, stored):
        result = _list_controls_result(stored.settings_file, _Supervising(_status("conservatory")))
        entry = result["controls"][0]
        assert result["supervisor_running"] is True
        assert entry["running"] is True
        assert entry["supervised"] is True
        assert entry["pid"] == 4321

    def test_a_control_the_supervisor_has_not_taken_on_is_not_running(self, stored):
        """What a control saved since the collector started looks like before a reload has
        reached the supervisor. Stopped, and known to be stopped, which is a different
        answer again from nobody watching."""
        result = _list_controls_result(stored.settings_file, _Supervising())
        entry = result["controls"][0]
        assert entry["supervised"] is False
        assert entry["running"] is False

    def test_a_control_waiting_out_a_backoff_says_how_long(self, stored):
        result = _list_controls_result(
            stored.settings_file,
            _Supervising(_status("conservatory", running=False, pid=None, failures=3, restart_in=12.34)),
        )
        entry = result["controls"][0]
        assert entry["running"] is False
        assert entry["failures"] == 3
        assert entry["restart_in_seconds"] == 12.3


class TestAControlThatWillNotRead:
    def test_it_is_listed_rather_than_omitted(self, stored):
        """Left out, a name somebody had just been given would read as "no such control",
        and the next question would be about the wrong thing entirely."""
        path = os.path.join(stored.state_dir, "controls", "bent.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("output: [not a control\n")
        entries = {entry["name"]: entry for entry in _list_controls_result(stored.settings_file, None)["controls"]}
        assert entries["bent"]["readable"] is False
        assert "not valid YAML" in entries["bent"]["error"]
        assert entries["conservatory"]["readable"] is True

    def test_the_reason_is_quoted_rather_than_interpolated_raw(self, stored):
        """A YAML parser quotes the offending line from the document in its own message, so
        the bare string carries newlines and whatever was in the file - and this value is
        handed to a client."""
        path = os.path.join(stored.state_dir, "controls", "bent.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("output: [not a control\n")
        entries = {entry["name"]: entry for entry in _list_controls_result(stored.settings_file, None)["controls"]}
        assert "\n" not in entries["bent"]["error"]


class TestADocumentOfTheWrongShape:
    """A document that parses as YAML can still be any shape at all, and the store
    guarantees only that it is a mapping. These are the shapes the supervisor already
    learned to refuse; the difference here is that one of them must not take out the
    listing for every other control as well."""

    @pytest.mark.parametrize(
        "broken",
        [
            pytest.param({"output": "soon"}, id="output-is-a-string"),
            pytest.param({"devices": 7}, id="devices-is-a-number"),
            pytest.param({"devices": ["far"]}, id="devices-is-a-list"),
            pytest.param({"inputs": 7}, id="inputs-is-a-number"),
        ],
    )
    def test_one_bad_document_does_not_take_out_the_listing(self, stored, broken):
        path = os.path.join(stored.state_dir, "controls", "bent.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(conservatory(), name="bent", **broken), handle)
        entries = {entry["name"]: entry for entry in _list_controls_result(stored.settings_file, None)["controls"]}
        assert entries["bent"]["readable"] is True
        assert entries["bent"]["valid"] is False
        assert entries["bent"]["errors"]
        # The point of the test: the good one is still described.
        assert entries["conservatory"]["valid"] is True
        assert entries["conservatory"]["devices"]

    def test_the_shaped_fields_are_absent_rather_than_guessed(self, stored):
        """`devices: 7` has no device list to report. Reporting one anyway - an empty list,
        say - would be the wrong answer rather than no answer."""
        path = os.path.join(stored.state_dir, "controls", "bent.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(conservatory(), name="bent", devices=7), handle)
        entries = {entry["name"]: entry for entry in _list_controls_result(stored.settings_file, None)["controls"]}
        assert "devices" not in entries["bent"]
        assert "cycle_seconds" not in entries["bent"]

    def test_it_still_says_whether_the_control_is_running(self, stored):
        """A control whose file was edited into nonsense a minute ago is still running the
        document it started with, and that is the more urgent of the two facts."""
        path = os.path.join(stored.state_dir, "controls", "bent.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(conservatory(), name="bent", output="soon"), handle)
        result = _list_controls_result(stored.settings_file, _Supervising(_status("bent")))
        entries = {entry["name"]: entry for entry in result["controls"]}
        assert entries["bent"]["valid"] is False
        assert entries["bent"]["running"] is True
        assert entries["bent"]["pid"] == 4321


class TestGettingOneControl:
    def test_it_returns_the_document_as_stored(self, stored):
        result = _get_control_result("conservatory", stored.settings_file)
        assert result["name"] == "conservatory"
        assert result["document"]["pid"]["kp"] == conservatory()["pid"]["kp"]

    def test_an_unknown_control_is_an_error_naming_it(self, stored):
        with pytest.raises(ConfigError, match="nosuchcontrol"):
            _get_control_result("nosuchcontrol", stored.settings_file)

    def test_a_name_that_could_choose_another_file_is_refused(self, stored):
        """The name becomes a filename and arrives from an MCP client."""
        with pytest.raises(ConfigError):
            _get_control_result("../../etc/passwd", stored.settings_file)

    def test_a_document_that_will_not_parse_is_an_error_rather_than_a_guess(self, stored):
        path = os.path.join(stored.state_dir, "controls", "conservatory.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("output: [not a control\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            _get_control_result("conservatory", stored.settings_file)


class TestRegistration:
    @staticmethod
    def _tools(settings):
        """Register against those settings and return the advertised tool names.

        Args:
            settings (dict): the settings to register from

        Returns:
            set: the registered tool names
        """
        server = MCPServer(name="controls-test")
        register_control_tools(server, settings, None)
        return {tool.name for tool in anyio.run(server.list_tools)}

    @pytest.mark.parametrize(
        "settings",
        [
            pytest.param({}, id="no-controls-block"),
            pytest.param({"controls": {}}, id="no-enabled-key"),
            pytest.param({"controls": {"enabled": False}}, id="off"),
            pytest.param({"controls": {"enabled": "true"}}, id="a-quoted-yaml-boolean-is-a-string"),
            pytest.param({"controls": "yes"}, id="not-even-a-mapping"),
        ],
    )
    def test_nothing_is_registered_unless_controls_are_on(self, settings):
        """Absent rather than refusing. A tool a model can see is a tool it will try, and a
        refusal costs a round trip to learn what the tool list could have said for free."""
        assert self._tools(settings) == set()

    def test_every_read_tool_is_registered_when_controls_are_on(self):
        assert self._tools({"controls": {"enabled": True}}) == {
            "list_controls",
            "get_control",
            "get_control_state",
            "get_control_schema",
        }

    def test_reading_is_not_gated_behind_the_write_flag(self):
        """A control document holds no secrets, and gating "what is this install controlling"
        behind the switch that permits changing a heating loop would mean nobody could look
        without also granting that. No write flag is set here and every read tool appears,
        `get_control_schema` included - so an installation where nothing may write controls can
        still be asked for a document to paste in, or to explain one written by hand.

        `get_control_state` belongs on this side of the line for the same reason: being able
        to see why a loop is doing what it does must not require permission to change it."""
        assert self._tools({"controls": {"enabled": True}}) == {
            "list_controls",
            "get_control",
            "get_control_state",
            "get_control_schema",
        }


class TestTheControlSchema:
    """What a client is handed when it has to *write* a control rather than read one.

    Every part is read from the constant that governs it. A description of a format
    maintained separately from the format is wrong the first time somebody changes the
    format and does not think to look - and this one is handed to a client that will then
    write a document from it.
    """

    SETTINGS = {"sources": ["hue", "openmeteo", "speedtest"], "controls": {"enabled": True}}

    def test_every_permitted_key_is_described(self):
        from toinflux.controls import CONTROL_KEYS

        schema = _control_schema_result(self.SETTINGS)
        assert set(schema["document"]["keys"]) == set(CONTROL_KEYS)

    def test_it_names_which_keys_are_required(self):
        from toinflux.controls import REQUIRED_CONTROL_KEYS

        schema = _control_schema_result(self.SETTINGS)
        assert set(schema["document"]["required_keys"]) == set(REQUIRED_CONTROL_KEYS)

    def test_the_rule_language_comes_from_the_parser_s_own_tables(self):
        """Not a second list written out beside the parser. A rule the parser accepts and
        the documentation has never heard of is the cheaper half of that failure; the
        expensive half is a model told about a function that does not exist."""
        from toinflux.rules import FUNCTION_ARITY, KEYWORDS, MAX_NESTING_DEPTH, MAX_RULE_LENGTH, OPERATORS

        rules = _control_schema_result(self.SETTINGS)["rules"]
        assert set(rules["functions"]) == set(FUNCTION_ARITY)
        assert rules["operators"] == list(OPERATORS)
        assert set(rules["keywords"]) == set(KEYWORDS)
        assert rules["max_length"] == MAX_RULE_LENGTH
        assert rules["max_nesting_depth"] == MAX_NESTING_DEPTH

    def test_it_describes_every_rule_slot_the_runtime_parses(self):
        from toinflux.controls import CONTROL_RULE_SLOTS

        slots = _control_schema_result(self.SETTINGS)["rules"]["slots"]
        assert [slot["where"] for slot in slots] == [where for _path, where, _optional in CONTROL_RULE_SLOTS]
        assert [slot["required"] for slot in slots] == [not optional for _p, _w, optional in CONTROL_RULE_SLOTS]

    def test_every_example_it_hands_out_is_valid_and_says_when_to_use_it(self):
        """The whole reason the examples live in the store rather than in prose. The design
        note's copy of this example was invalid for as long as it existed - it read
        `outside` in `enable_when` and declared no such input - and nothing could see it
        until rules were validated. An example a client is told to start from must be one
        the validator accepts.

        Asserted through the tool's own result rather than the constant, because the thing
        that matters is what a client is handed. `use_when` is part of that: three documents
        with no way to choose between them is how a model averages across them instead of
        picking one.
        """
        from toinflux.controls import validate_control

        examples = _control_schema_result(self.SETTINGS)["examples"]
        assert len(examples) >= 3, f"expected a scenario set, got {sorted(examples)}"
        for scenario, entry in examples.items():
            document = entry["document"]
            assert validate_control(document["name"], document) == [], scenario
            assert entry["use_when"].strip(), f"{scenario} does not say when it applies"

    def test_it_says_which_sources_can_be_read_and_which_can_switch(self):
        """Read from the registry rather than from a list written here: a source added to
        the build appears without anyone remembering to add it, and one that cannot actuate
        cannot be named as a device however writable it is."""
        sources = _control_schema_result(self.SETTINGS)["sources"]
        assert "openmeteo" in sources["readable_as_inputs"]
        assert "hue" in sources["can_switch_devices"]
        # Writable - it can trigger a run - and unable to switch a named device.
        assert "speedtest" in sources["readable_as_inputs"]
        assert "speedtest" not in sources["can_switch_devices"]

    def test_an_install_collecting_nothing_offers_nothing(self):
        """An input is read from stored data, so a source nothing collects has nothing to
        read. Falling back to "every source this build knows" would describe a different
        installation, and a control written from it would fail at its first read."""
        schema = _control_schema_result({"controls": {"enabled": True}})
        assert schema["sources"] == {"readable_as_inputs": [], "can_switch_devices": []}
        assert _control_schema_result({"sources": "hue", "controls": {}})["sources"]["readable_as_inputs"] == []

    def test_a_source_named_in_any_case_is_reported_lowercased(self):
        """The same list the collectors run and the other MCP tools expose, so the name a
        client is given here is the name it will see everywhere else."""
        schema = _control_schema_result({"sources": ["Hue", "OpenMeteo"], "controls": {"enabled": True}})
        assert schema["sources"]["readable_as_inputs"] == ["hue", "openmeteo"]

    def test_a_source_this_build_does_not_know_is_left_out_rather_than_guessed(self):
        schema = _control_schema_result({"sources": ["hue", "nosuchsource"], "controls": {"enabled": True}})
        assert schema["sources"]["readable_as_inputs"] == ["hue"]

    def test_the_safe_states_are_the_ones_the_code_accepts(self):
        from toinflux.controls import BUILT_IN_SAFE_STATES

        assert _control_schema_result(self.SETTINGS)["safe_states"] == list(BUILT_IN_SAFE_STATES)


class _Reloading:
    """A supervisor stand-in that records what it was asked to reconcile.

    Records rather than acts: what matters at this layer is that the write told the
    supervisor, and in which order relative to the file changing. What the supervisor then
    does with the name is `tests/test_supervision.py`'s question.
    """

    def __init__(self, on_request=None):
        """Hold the requests, and optionally observe each one as it arrives.

        Args:
            on_request (collections.abc.Callable or None): called with the name at the
                moment of the request, for asserting on the state of the disk *then*
        """
        self.requests = []
        self._on_request = on_request

    def request_reload(self, name) -> None:
        """Record a reload request.

        Args:
            name (str): the control whose document changed
        """
        self.requests.append(name)
        if self._on_request is not None:
            self._on_request(name)


class TestWriteToolsAreOnTheirOwnSwitch:
    """`controls.enabled` runs the loops an operator wrote; `controls.mcp_write` hands a
    model authorship of them. Neither implies the other, so the tool list has to prove it."""

    @staticmethod
    def _tools(settings):
        """Register against those settings and return the advertised tool names.

        Args:
            settings (dict): the settings to register from

        Returns:
            set: the registered tool names
        """
        server = MCPServer(name="controls-write-test")
        register_control_tools(server, settings, None)
        return {tool.name for tool in anyio.run(server.list_tools)}

    WRITE_TOOLS = {"save_control", "set_control_enabled", "delete_control"}

    @pytest.mark.parametrize(
        "controls",
        [
            pytest.param({"enabled": True}, id="no-write-key"),
            pytest.param({"enabled": True, "mcp_write": False}, id="off"),
            pytest.param({"enabled": True, "mcp_write": "true"}, id="a-quoted-yaml-boolean-is-a-string"),
            pytest.param({"enabled": True, "mcp_write": 1}, id="truthy-but-not-true"),
        ],
    )
    def test_no_write_tool_is_registered_without_the_switch(self, controls):
        """Strict `is True`, the same as the switch above it: a quoted boolean is a truthy
        string, and a loose check would hand out authorship to somebody who quoted YAML."""
        assert self._tools({"controls": controls}) & self.WRITE_TOOLS == set()

    def test_the_write_switch_alone_grants_nothing(self):
        """`mcp_write` without `enabled` is an install that has not asked for controls at
        all. Writing documents nothing will ever run is not a capability worth advertising."""
        assert self._tools({"controls": {"mcp_write": True}}) == set()

    def test_every_write_tool_appears_when_both_are_on(self):
        registered = self._tools({"controls": {"enabled": True, "mcp_write": True}})
        assert self.WRITE_TOOLS <= registered

    def test_the_read_tools_are_still_there(self):
        """Granting writes must not quietly replace the read surface it builds on."""
        registered = self._tools({"controls": {"enabled": True, "mcp_write": True}})
        assert {"list_controls", "get_control", "get_control_schema"} <= registered


class TestTheSchemaSaysWhetherWritingIsAvailable:
    """The whole nudge. With the write tools unregistered a model cannot tell "this install
    has not enabled writing" from "this build cannot write controls", and the two call for
    different answers to the user. So the tool it must call before composing says which."""

    @staticmethod
    def _writing(mcp_write, settings_file=None):
        """Return the schema's writing section for that switch setting.

        Args:
            mcp_write (bool): what `controls.mcp_write` is set to
            settings_file (str or None): the settings path to report

        Returns:
            dict: the `writing` section
        """
        settings = {"sources": ["hue"], "controls": {"enabled": True, "mcp_write": mcp_write}}
        return _control_schema_result(settings, settings_file)["writing"]

    def test_it_says_so_when_writing_is_not_available(self):
        assert self._writing(False)["available"] is False

    def test_it_names_the_setting_that_would_enable_it(self):
        """Without the exact key, "ask your operator to turn on writes" is a dead end for
        both of them."""
        writing = self._writing(False)
        assert writing["setting"] == "controls.mcp_write"
        assert writing["set_it_to"] is True

    def test_it_names_the_settings_file_it_is_running_from(self):
        """The file in effect, not a guess at where one usually lives: an operator editing
        the wrong settings.yaml sees no change and has nothing to blame."""
        assert self._writing(False, "/etc/send-to-influx/settings.yaml")["in_file"] == (
            "/etc/send-to-influx/settings.yaml"
        )

    def test_it_says_where_a_document_would_go_if_saved_by_hand(self):
        """Off is a working state: compose it and hand it over. That is only actionable
        with somewhere to put it."""
        assert "controls" in self._writing(False)["meanwhile"]

    def test_it_says_the_two_switches_are_separate(self):
        """The trap this whole section exists for: `controls.enabled` is on, so the model can
        see and read controls, and writing still refuses. Nothing about that is guessable."""
        assert "neither implies the other" in self._writing(False)["note"]

    def test_it_says_so_when_writing_is_available(self):
        writing = self._writing(True)
        assert writing["available"] is True
        assert set(writing["tools"]) == {"save_control", "set_control_enabled", "delete_control"}

    def test_it_does_not_name_a_setting_that_is_already_set(self):
        """Advice to enable what is enabled would send a model to the operator for nothing."""
        assert "setting" not in self._writing(True)


class TestSavingAControl:
    """Validation happens here, because this is the door an unchecked document arrives at:
    `controls.save_control` writes whatever it is given, deliberately."""

    def test_a_valid_document_is_stored_and_applied(self, state_directory):
        supervisor = _Reloading()
        document = conservatory()
        result = _save_control_result("conservatory", document, state_directory.settings_file, supervisor)
        assert result["saved"] == "conservatory"
        assert supervisor.requests == ["conservatory"]
        assert _get_control_result("conservatory", state_directory.settings_file)["document"] == document

    def test_it_says_whether_it_replaced_something(self, stored):
        """A model that thinks it created a control when it overwrote a running one will
        report the wrong thing to the operator."""
        created = _save_control_result(
            "spare_room", conservatory(name="spare_room"), stored.settings_file, _Reloading()
        )
        replaced = _save_control_result("conservatory", conservatory(), stored.settings_file, _Reloading())
        assert created["replaced_existing"] is False
        assert replaced["replaced_existing"] is True

    def test_an_invalid_document_writes_nothing(self, stored):
        """The control that was there keeps running the document it already had, which
        matters because it may be holding a room at temperature."""
        before = _get_control_result("conservatory", stored.settings_file)
        broken = conservatory()
        broken["stages"] = "not a ladder"
        with pytest.raises(ToolParamError):
            _save_control_result("conservatory", broken, stored.settings_file, _Reloading())
        assert _get_control_result("conservatory", stored.settings_file) == before

    def test_an_invalid_document_is_not_applied_either(self, stored):
        """Nothing written means nothing to reconcile. A reload request here would stop a
        working control and start it again from the document it already had."""
        supervisor = _Reloading()
        with pytest.raises(ToolParamError):
            _save_control_result("conservatory", {"name": "conservatory"}, stored.settings_file, supervisor)
        assert supervisor.requests == []

    def test_every_problem_is_reported_at_once(self, state_directory):
        """One fault per round trip is one exchange per fault, and they are independent."""
        broken = conservatory()
        broken["stages"] = "not a ladder"
        del broken["inputs"]
        with pytest.raises(ToolParamError) as raised:
            _save_control_result("conservatory", broken, state_directory.settings_file, _Reloading())
        assert "stages" in str(raised.value) and "inputs" in str(raised.value)

    def test_the_refusal_points_at_the_format(self, state_directory):
        """A model told only "invalid" guesses again; told where the format is, it reads it."""
        with pytest.raises(ToolParamError, match="get_control_schema"):
            _save_control_result("conservatory", {"name": "conservatory"}, state_directory.settings_file, _Reloading())

    @pytest.mark.parametrize("document", [[], "name: conservatory", None, 7])
    def test_something_that_is_not_a_document_is_refused_by_type(self, state_directory, document):
        """A YAML string is the likely mistake: a model that has just been handed an example
        as text may send it back as text."""
        with pytest.raises(ToolParamError, match="mapping"):
            _save_control_result("conservatory", document, state_directory.settings_file, _Reloading())

    def test_a_name_that_could_choose_another_file_is_refused(self, state_directory):
        """The name is concatenated into a path and arrives from an MCP client.

        The document names itself the same thing deliberately. A mismatch between the two is
        caught by validation, which would mask the question being asked here: `validate_control`
        does *not* check that a name is usable as a filename, so with both agreeing, the only
        thing standing between a traversal and a write is the store's own guard. Asserting on
        the weaker case would have passed whether that guard existed or not."""
        evil = "../../etc/cron.d/x"
        with pytest.raises(ConfigError, match="invalid control name"):
            _save_control_result(evil, conservatory(name=evil), state_directory.settings_file, _Reloading())
        assert not os.path.exists("/etc/cron.d/x")

    def test_a_name_disagreeing_with_the_document_is_refused(self, state_directory):
        """The other half: saving under one name a document that calls itself another would
        store a control whose `name` does not match the file the supervisor finds it in."""
        with pytest.raises(ToolParamError, match="but the file is named"):
            _save_control_result("somewhere_else", conservatory(), state_directory.settings_file, _Reloading())

    def test_it_reports_whether_the_control_will_actually_start(self, state_directory):
        """Stored is not running. With nothing supervising - the subsystem on but no control
        startable - a client told its save took effect waits for a heater nobody will start."""
        result = _save_control_result("conservatory", conservatory(), state_directory.settings_file, None)
        assert result["reload"]["in_effect"] is False
        assert "restart" in result["reload"]["detail"]


class TestEnablingAndDisabling:
    def test_it_turns_a_control_on(self, state_directory):
        state_directory.write_control(conservatory(enabled=False))
        supervisor = _Reloading()
        result = _set_enabled_result("conservatory", True, state_directory.settings_file, supervisor)
        assert result["changed"] is True
        assert _get_control_result("conservatory", state_directory.settings_file)["document"]["enabled"] is True
        assert supervisor.requests == ["conservatory"]

    def test_it_leaves_the_rest_of_the_document_alone(self, state_directory):
        """The reason this is a separate tool from save: it cannot change what a control
        does, only whether it does it."""
        state_directory.write_control(conservatory(enabled=False))
        before = _get_control_result("conservatory", state_directory.settings_file)["document"]
        _set_enabled_result("conservatory", True, state_directory.settings_file, _Reloading())
        after = _get_control_result("conservatory", state_directory.settings_file)["document"]
        assert {key: value for key, value in after.items() if key != "enabled"} == (
            {key: value for key, value in before.items() if key != "enabled"}
        )

    def test_already_in_that_state_is_success_and_writes_nothing(self, stored):
        """Not an error: a model reconciling to a desired state should not have to check
        first. Writing anyway would restart a running control for no change."""
        supervisor = _Reloading()
        result = _set_enabled_result("conservatory", True, stored.settings_file, supervisor)
        assert result["changed"] is False
        assert supervisor.requests == []

    @pytest.mark.parametrize("enabled", ["true", 1, None])
    def test_a_non_boolean_is_refused(self, stored, enabled):
        """The same trap as the settings switch, at the other end of the same idea."""
        with pytest.raises(ToolParamError, match="true or false"):
            _set_enabled_result("conservatory", enabled, stored.settings_file, _Reloading())

    def test_an_unknown_control_is_an_error_naming_it(self, state_directory):
        with pytest.raises(ConfigError, match="nowhere"):
            _set_enabled_result("nowhere", True, state_directory.settings_file, _Reloading())


class TestDeletingAControl:
    def test_it_removes_the_document(self, stored):
        _delete_control_result("conservatory", stored.settings_file, _Reloading())
        with pytest.raises(ConfigError):
            _get_control_result("conservatory", stored.settings_file)

    def test_the_supervisor_is_told_after_the_file_is_gone(self, stored):
        """The supervisor decides what a reload means by looking at the document. Asked
        while the file still existed, a deletion reads as a restart - so it would stop the
        control and start it again from the document that is about to vanish."""
        seen = {}

        def _look(name):
            seen[name] = os.path.exists(os.path.join(stored.state_dir, "controls", f"{name}.yaml"))

        _delete_control_result("conservatory", stored.settings_file, _Reloading(on_request=_look))
        assert seen == {"conservatory": False}

    def test_deleting_something_that_is_not_there_is_an_error(self, state_directory):
        """A mistyped name reported rather than appearing to have worked."""
        with pytest.raises(ConfigError, match="nowhere"):
            _delete_control_result("nowhere", state_directory.settings_file, _Reloading())

    def test_a_name_that_could_choose_another_file_is_refused(self, state_directory):
        with pytest.raises(ConfigError):
            _delete_control_result("../../etc/cron.d/x", state_directory.settings_file, _Reloading())


class TestWhatTheToolAnnotationsClaim:
    """`idempotent_hint` means "calling it repeatedly with the same arguments will have no
    additional effect on its environment" (the SDK's own words). A client may retry on that
    basis, so a wrong hint here is a wrong retry against real heaters."""

    @staticmethod
    def _annotations(name):
        """Return one registered tool's annotations.

        Args:
            name (str): the tool name

        Returns:
            ToolAnnotations: what it advertises
        """
        server = MCPServer(name="annotations-test")
        register_control_tools(server, {"controls": {"enabled": True, "mcp_write": True}}, None)
        tools = {tool.name: tool for tool in anyio.run(server.list_tools)}
        return tools[name].annotations

    def test_saving_is_not_idempotent(self):
        """The file would end up identical, but a second save requests another reload, and a
        reload stops a running control, makes its devices safe and starts it again. Repeating
        the call moves heaters, which is an additional effect by any reading."""
        assert self._annotations("save_control").idempotent_hint is False

    def test_setting_enabled_is_idempotent(self):
        """The opposite case, and the reason this is not a blanket rule: it returns early
        when nothing would change, so a repeat writes nothing and asks for no reload."""
        assert self._annotations("set_control_enabled").idempotent_hint is True

    def test_deleting_is_not_idempotent(self):
        """A second delete raises rather than succeeding quietly."""
        assert self._annotations("delete_control").idempotent_hint is False

    @pytest.mark.parametrize("tool", ["save_control", "set_control_enabled", "delete_control"])
    def test_every_write_tool_says_it_writes(self, tool):
        assert self._annotations(tool).read_only_hint is False


class TestWhetherANewControlWillActuallyStart:
    """There is no supervisor when nothing was supervisable at startup, and the commonest
    way to be in that state is to have no controls stored at all - which is exactly the
    person being told how to write their first one by hand."""

    @staticmethod
    def _writing(mcp_write, supervisor):
        """Return the schema's writing section.

        Args:
            mcp_write (bool): what `controls.mcp_write` is set to
            supervisor (object or None): the supervisor to report against

        Returns:
            dict: the `writing` section
        """
        settings = {"sources": ["hue"], "controls": {"enabled": True, "mcp_write": mcp_write}}
        return _control_schema_result(settings, "/etc/send-to-influx/settings.yaml", supervisor)["writing"]

    @pytest.mark.parametrize("supervisor", [None, "running"], ids=["no-supervisor", "supervising"])
    def test_hand_saving_advice_always_says_a_restart_is_needed(self, supervisor):
        """Nothing watches the control directory, so a file written by hand is picked up by
        nothing - a reload is queued by the write tools and by no other path.

        This previously asserted the opposite while a supervisor was running, which was a
        test defending the wrong behaviour: the advice it pinned is given to the one reader
        for whom "the service picks it up" is false, because they are being told to save the
        file themselves.
        """
        advice = self._writing(False, _Reloading() if supervisor else None)["meanwhile"]
        assert "restart" in advice, advice
        assert "does not watch" in advice, advice

    def test_the_write_tools_carry_the_same_caveat(self):
        """A model told its save takes effect would otherwise wait for a heater that nothing
        is going to start - the same distinction the save result itself reports."""
        assert "restarted" in self._writing(True, None)["takes_effect"]

    def test_and_do_not_carry_it_when_it_does_not_apply(self):
        assert "restarted" not in self._writing(True, _Reloading())["takes_effect"]


class TestSavingReportsWhatItChanged:
    """`save_control` replaces the whole document, so a client that re-composes one from
    memory instead of reading it, changing a key and writing it back can produce something
    that validates and is not what was there. A stage ladder with different rungs is legal,
    so nothing else catches it. Naming what moved makes that visible instead of silent.
    """

    @staticmethod
    def _save(stored, **changes):
        """Save the conservatory with those top-level changes and return the result.

        Args:
            stored (Installation): the installation holding the original
            **changes: top-level keys to replace

        Returns:
            dict: the tool's result
        """
        return _save_control_result("conservatory", conservatory(**changes), stored.settings_file, _Reloading())

    def test_creating_reports_no_changes_at_all(self, state_directory):
        """There is nothing to compare against, and an empty change list would read as
        "nothing moved" for a control that did not exist a moment ago."""
        result = _save_control_result("fresh", conservatory(name="fresh"), state_directory.settings_file, _Reloading())
        assert "changed" not in result
        assert result["replaced_existing"] is False

    def test_an_identical_save_reports_nothing_changed(self, stored):
        """The round-trip a well-behaved client performs when it decides not to edit."""
        result = self._save(stored)
        assert result["changed"] == {"sections": [], "device_plan": []}

    def test_changing_one_parameter_names_only_that_section(self, stored):
        """The case the report exists to make boring: an adjustment that touched nothing else."""
        result = self._save(stored, parameters={"target": 21.0})
        assert result["changed"]["sections"] == ["parameters"]
        assert result["changed"]["device_plan"] == []

    def test_a_rewritten_stage_ladder_is_called_out_as_a_device_plan_change(self, stored):
        """The case it exists to catch. This document validates; it simply is not the one
        that was there, and the rungs decide what the heaters do."""
        output = conservatory()["output"]
        output["stages"] = [stage for stage in output["stages"] if stage["level"] != 750]
        result = self._save(stored, output=output)
        assert "output" in result["changed"]["device_plan"]

    def test_it_says_so_in_the_journal_too(self, stored, caplog):
        """The result reaches the client; the operator reads the journal."""
        output = conservatory()["output"]
        output["stages"] = [stage for stage in output["stages"] if stage["level"] != 750]
        with caplog.at_level(logging.WARNING):
            self._save(stored, output=output)
        assert "changing which devices it commands and when" in caplog.text
        assert "conservatory" in caplog.text

    def test_a_parameter_change_does_not_warn(self, stored, caplog):
        """A warning on every ordinary edit is a warning nobody reads."""
        with caplog.at_level(logging.WARNING):
            self._save(stored, parameters={"target": 21.0})
        assert "changing which devices it commands and when" not in caplog.text

    def test_an_unreadable_stored_document_reports_everything_as_changed(self, stored):
        """Not "nothing changed", which would be reassuring and wrong: going from a document
        that will not parse to one that does is the largest change there is."""
        with open(os.path.join(stored.state_dir, "controls", "conservatory.yaml"), "w", encoding="utf-8") as handle:
            handle.write("{{{ not yaml")
        result = self._save(stored)
        assert "output" in result["changed"]["device_plan"]
        assert "parameters" in result["changed"]["sections"]

    def test_a_tuning_change_is_reported_but_not_as_a_device_plan_change(self, stored):
        """`device_plan` is a subset, not a "behaviour changed" flag, and the distinction is
        the whole reason it is useful.

        Rewriting `pid` gains changes behaviour dramatically - `kp` of 1200 makes the loop
        unstable - and it belongs in `sections`, not here. `device_plan` answers a narrower
        question: did which-devices-and-when change? That is the one somebody alters by
        accident while meaning to alter a target, and a flag that fired on every ordinary
        tuning edit would be a flag nobody read.
        """
        pid = dict(conservatory()["pid"], kp=1200.0)
        result = self._save(stored, pid=pid)
        assert result["changed"]["sections"] == ["pid"]
        assert result["changed"]["device_plan"] == []

    def test_sections_is_the_complete_answer(self, stored):
        """Whatever `device_plan` says, nothing that differs is left out of `sections`."""
        output = conservatory()["output"]
        output["cycle_seconds"] = 600
        result = self._save(stored, parameters={"target": 21.0}, output=output)
        assert result["changed"]["sections"] == ["output", "parameters"]
        assert result["changed"]["device_plan"] == ["output"]


class TestEachToolReachesItsOwnWorker:
    """The six registered callables are one-line wrappers around the `_*_result` helpers, and
    every other test in this module calls those helpers directly.

    So a wrapper pointed at the wrong helper, or passing its arguments in the wrong order,
    would satisfy the registration tests, the annotation tests, the description guards and
    every behaviour test here. Driving them through `server.call_tool` is the only thing that
    reads the wiring itself.
    """

    @staticmethod
    def _call(name, arguments, settings_file, supervisor=None):
        """Invoke one registered control tool the way a client would.

        Args:
            name (str): the tool name
            arguments (dict): the call arguments
            settings_file (str or None): the settings path to register with
            supervisor (object or None): the supervisor to register with

        Returns:
            CallToolResult: what the server returned
        """
        server = MCPServer(name="wiring-test")
        settings = {"sources": ["hue"], "controls": {"enabled": True, "mcp_write": True}}
        register_control_tools(server, settings, settings_file, supervisor=supervisor)
        return anyio.run(server.call_tool, name, arguments)

    def test_list_controls_returns_this_installation_s_controls(self, stored):
        result = self._call("list_controls", {}, stored.settings_file)
        assert "conservatory" in str(result.content)

    def test_get_control_returns_the_named_document(self, stored):
        result = self._call("get_control", {"name": "conservatory"}, stored.settings_file)
        assert "Europe/London" in str(result.content)

    def test_get_control_schema_returns_the_format(self, stored):
        result = self._call("get_control_schema", {}, stored.settings_file)
        assert "safe_states" in str(result.content)

    def test_save_control_writes_through_the_wrapper(self, state_directory):
        document = conservatory(name="wired")
        self._call("save_control", {"name": "wired", "document": document}, state_directory.settings_file)
        assert _get_control_result("wired", state_directory.settings_file)["document"] == document

    def test_set_control_enabled_writes_through_the_wrapper(self, state_directory):
        state_directory.write_control(conservatory(enabled=False))
        self._call("set_control_enabled", {"name": "conservatory", "enabled": True}, state_directory.settings_file)
        assert _get_control_result("conservatory", state_directory.settings_file)["document"]["enabled"] is True

    def test_delete_control_removes_through_the_wrapper(self, stored):
        self._call("delete_control", {"name": "conservatory"}, stored.settings_file)
        with pytest.raises(ConfigError):
            _get_control_result("conservatory", stored.settings_file)

    def test_a_refusal_reaches_the_client_with_its_message(self, state_directory):
        """The wrapper must not swallow what the helper raised.

        `translate_failures` is what keeps a ToolParamError's text on the wire rather than
        flattening it to "Error executing tool", and the registrar applies it - so this also
        asserts the wrapper went through `register_tool` rather than around it.
        """
        with pytest.raises(Exception) as raised:
            self._call("save_control", {"name": "broken", "document": {}}, state_directory.settings_file)
        assert "get_control_schema" in str(raised.value)
        assert "nothing has been written" in str(raised.value)


class TestTheEnabledDefaultAgreesWithTheGate:
    """`enabled` is optional and an omitted key means enabled - `Gate` and `_describe` both
    read it as `get(..., True)`. The write tools read it as `is True`, so a document without
    the key was reported disabled while the supervisor started actuating it. A caller told a
    control is off does not go and turn it off, which is the one direction this must not be
    wrong in."""

    def test_saving_a_document_with_no_enabled_key_reports_it_enabled(self, state_directory):
        document = conservatory()
        del document["enabled"]
        result = _save_control_result("conservatory", document, state_directory.settings_file, _Reloading())
        assert result["enabled"] is True

    def test_the_gate_agrees(self, state_directory):
        """The assertion above is only worth anything against what actually runs it."""
        from toinflux.gating import Gate

        document = conservatory()
        del document["enabled"]
        assert Gate(document).enabled is True

    def test_enabling_a_document_with_no_key_is_a_no_op(self, state_directory):
        """It was already enabled, so there is nothing to write and nothing to restart."""
        document = conservatory()
        del document["enabled"]
        state_directory.write_control(document)
        supervisor = _Reloading()
        result = _set_enabled_result("conservatory", True, state_directory.settings_file, supervisor)
        assert result["changed"] is False
        assert supervisor.requests == []


class TestTheWriteToolsDoNotInterleave:
    """All three are read-modify-writes and the SDK runs them on worker threads, so two calls
    can interleave: a `set_control_enabled` that loaded before a `save_control` stored would
    write its stale copy afterwards and silently undo the edit."""

    def test_the_store_happens_under_the_write_lock(self, state_directory, monkeypatch):
        """Asserted at the moment of writing rather than by racing two threads and hoping:
        a timing test that passes is not evidence the window is closed."""
        import toinflux.mcp_controls as module

        held = []
        real = module.store_control

        def watched(name, document, settings_file=None):
            """Record whether the lock is held while the store runs.

            Args:
                name (str): the control's name
                document (dict): the document being written
                settings_file (str or None): the settings path
            """
            held.append(module._WRITE_LOCK.locked())
            return real(name, document, settings_file)

        monkeypatch.setattr(module, "store_control", watched)
        _save_control_result("conservatory", conservatory(), state_directory.settings_file, _Reloading())
        assert held == [True], "save_control stored without holding the write lock"

    def test_setting_enabled_holds_it_across_the_read_and_the_write(self, state_directory):
        """The whole read-modify-write, not just the store - a lock around the write alone
        leaves exactly the window this exists to close."""
        import toinflux.mcp_controls as module

        state_directory.write_control(conservatory(enabled=False))
        held = []
        real = module.load_control

        def watched(name, settings_file=None):
            """Record whether the lock is held while the document is read.

            Args:
                name (str): the control's name
                settings_file (str or None): the settings path

            Returns:
                dict: the loaded document
            """
            held.append(module._WRITE_LOCK.locked())
            return real(name, settings_file)

        module.load_control = watched
        try:
            _set_enabled_result("conservatory", True, state_directory.settings_file, _Reloading())
        finally:
            module.load_control = real
        assert held == [True], "the document was read outside the lock, so an edit can land between"


class TestOneEnabledControlPerActuator:
    """Two loops commanding one heater fight, and neither can tell. The rule is that only one
    *enabled* control may own an actuator - a duplicate stored disabled is how somebody
    prepares a replacement before switching over."""

    def test_a_clashing_control_is_stored_disabled_rather_than_refused(self, stored):
        """Refusing would throw away a document just composed and make the caller ask again
        with one key changed. Disabling keeps the work and keeps the invariant."""
        result = _save_control_result("spare", conservatory(name="spare"), stored.settings_file, _Reloading())
        assert result["enabled"] is False
        assert result["stored_disabled"]["because"].startswith("control 'conservatory' is enabled")

    def test_it_says_what_to_do_about_it(self, stored):
        """A refusal that does not say which control is in the way leaves somebody reading
        every document to find out."""
        result = _save_control_result("spare", conservatory(name="spare"), stored.settings_file, _Reloading())
        assert "disable 'conservatory'" in result["stored_disabled"]["to_enable_this_one"]

    def test_the_document_on_disk_is_the_disabled_one(self, stored):
        """The result saying `enabled: false` is worth nothing if the file says otherwise."""
        _save_control_result("spare", conservatory(name="spare"), stored.settings_file, _Reloading())
        assert _get_control_result("spare", stored.settings_file)["document"]["enabled"] is False

    def test_enabling_it_is_refused_and_names_the_holder(self, stored):
        _save_control_result("spare", conservatory(name="spare"), stored.settings_file, _Reloading())
        with pytest.raises(ToolParamError, match="conservatory"):
            _set_enabled_result("spare", True, stored.settings_file, _Reloading())

    def test_disabling_the_holder_first_lets_it_be_enabled(self, stored):
        """The workflow the messages describe has to actually work end to end."""
        _save_control_result("spare", conservatory(name="spare"), stored.settings_file, _Reloading())
        _set_enabled_result("conservatory", False, stored.settings_file, _Reloading())
        assert _set_enabled_result("spare", True, stored.settings_file, _Reloading())["changed"] is True

    def test_a_control_on_its_own_actuators_is_unaffected(self, stored):
        """The rule must not refuse an ordinary second control."""
        other = conservatory(name="porch")
        other["devices"] = {"porch": {"source": "hue", "device": "porch-heater"}}
        other["output"]["stages"] = [{"level": 0, "set": {"porch": False}}, {"level": 1500, "set": {"porch": True}}]
        result = _save_control_result("porch", other, stored.settings_file, _Reloading())
        assert result["enabled"] is True
        assert "stored_disabled" not in result

    def test_replacing_a_control_does_not_clash_with_itself(self, stored):
        """It owns those actuators already - excluding itself is what makes an edit possible."""
        result = _save_control_result("conservatory", conservatory(), stored.settings_file, _Reloading())
        assert result["enabled"] is True
        assert "stored_disabled" not in result


class TestAnOmittedInstanceIsNotADifferentActuator:
    """Saving a control that names the first bridge explicitly, while an enabled one omits
    `instance`, must not slip past the ownership rule - `None` means that same bridge."""

    def test_it_is_stored_disabled_like_any_other_clash(self, stored):
        explicit = conservatory(name="spare")
        explicit["devices"] = {key: dict(spec, instance="bridge1") for key, spec in explicit["devices"].items()}
        result = _save_control_result("spare", explicit, stored.settings_file, _Reloading())
        assert result["enabled"] is False
        assert "conservatory" in result["stored_disabled"]["because"]


class TestReportingWhatAControlHasWorkedOut:
    """The loop's own state, which device output alone cannot show.

    Read from the file the control writes rather than asked of the control: the PID lives in
    a child process and the MCP server does not, and the child already writes this down every
    cycle so a restart does not lose it. The persistence and the tool are the same mechanism.
    """

    @staticmethod
    def _ran(installation, cycles=3, minimum=1):
        """Store a lamp control, run it, and return its name.

        Args:
            installation (Installation): the installation to write into
            cycles (int): how many cycles to spend
            minimum (float): its min_transition_seconds

        Returns:
            str: the control's name
        """
        from tests.harness.bridge import bulb
        from toinflux.control_process import ControlProcess
        from toinflux.controls import save_control

        installation.bridge.lights["9"] = bulb("office-lamp")
        document = conservatory(name="lamp")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["parameters"] = {"target": 1000}
        document["inputs"] = {"lux": {"source": "hue", "field": "L", "max_age": 60}}
        document["pid"] = {"input": "lux", "setpoint": "target", "kp": 0.05, "ki": 0.0007, "kd": 0}
        document["devices"] = {"lamp": {"source": "hue", "device": "office-lamp", "parameter": "brightness_pct"}}
        document["output"] = {
            "cycle_seconds": 60,
            "min_transition_seconds": minimum,
            "stages": [{"level": 0, "set": {"lamp": 0}}, {"level": 100, "set": {"lamp": 100}}],
        }
        save_control("lamp", document, installation.settings_file)
        control = ControlProcess("lamp", settings_file=installation.settings_file)
        try:
            for _ in range(cycles):
                control.controller.step({"lux": 300.0, "target": 1000}, dt=60)
                control.transitions.record_loop(control.controller.capture(), control.controller.fingerprint)
            control._apply({"lamp": 94})
        finally:
            control.guard.stop("the test is finished")
            control.close()
        return "lamp"

    def test_it_reports_the_integral(self, state_directory, bridge):
        """The number that explains an output moving against its input, and the one thing
        nothing else advertised here can show."""
        name = self._ran(state_directory)
        result = _control_state_result(name, state_directory.settings_file)
        assert result["loop"]["integral"] > 0
        assert result["loop"]["age_seconds"] is not None

    def test_it_says_whether_the_memory_still_matches_the_document(self, state_directory, bridge):
        from toinflux.controls import load_control, save_control

        name = self._ran(state_directory)
        assert _control_state_result(name, state_directory.settings_file)["loop"]["matches_document"] is True
        document = load_control(name, state_directory.settings_file)
        document["pid"]["kp"] = 0.2
        save_control(name, document, state_directory.settings_file)
        assert _control_state_result(name, state_directory.settings_file)["loop"]["matches_document"] is False

    def test_it_reports_what_each_device_was_last_set_to(self, state_directory, bridge):
        name = self._ran(state_directory)
        lamp = _control_state_result(name, state_directory.settings_file)["devices"]["lamp"]
        assert lamp["state"] is not None
        assert lamp["age_seconds"] is not None
        assert lamp["forced"] is True, "the guard's exit command is a safe state"

    def test_it_names_devices_held_by_their_minimum(self, state_directory, bridge):
        """Which is the other thing that makes a control look like it is ignoring its input."""
        name = self._ran(state_directory, minimum=3600)
        result = _control_state_result(name, state_directory.settings_file)
        assert result["held_by_minimum"] == [], "a forced move is exempt in both directions"
        from toinflux.transitions import TransitionLog

        log = TransitionLog(name, state_directory.settings_file)
        # With the parameter, as `command_devices` records it: a value whose scale no longer
        # matches the document is not one the loop will hold, so it is not reported as held.
        log.record(
            {"lamp": 55},
            parameters={"lamp": "brightness_pct"},
            targets={"lamp": ("hue", None, "office-lamp")},
        )
        assert _control_state_result(name, state_directory.settings_file)["held_by_minimum"] == ["lamp"]

    def test_a_device_recorded_on_another_scale_is_not_reported_as_held(self, state_directory, bridge):
        """`_hold` refuses to pin a value whose parameter is not the one the document drives
        the device by now, so the next cycle will move it whatever its timer says. Reporting
        it as held would describe a restraint that is not going to happen."""
        name = self._ran(state_directory, minimum=3600)
        from toinflux.transitions import TransitionLog

        log = TransitionLog(name, state_directory.settings_file)
        log.record({"lamp": 2700}, parameters={"lamp": "color_temp_k"})
        result = _control_state_result(name, state_directory.settings_file)
        assert result["held_by_minimum"] == []
        assert result["devices"]["lamp"]["parameter"] == "color_temp_k", "the scale was not reported"

    def test_it_reports_the_scale_a_state_is_on(self, state_directory, bridge):
        """A driven device's 40 is forty percent or forty kelvin depending on this, and a
        client reading the state has no other way to tell."""
        name = self._ran(state_directory, minimum=3600)
        from toinflux.transitions import TransitionLog

        TransitionLog(name, state_directory.settings_file).record({"lamp": 55}, parameters={"lamp": "brightness_pct"})
        lamp = _control_state_result(name, state_directory.settings_file)["devices"]["lamp"]
        assert lamp == {**lamp, "state": 55, "parameter": "brightness_pct"}

    def test_a_hand_edited_document_is_reported_rather_than_crashing(self, state_directory):
        """This tool reads whatever is on disk and validates only to decide whether a
        fingerprint means anything, so a `devices:` holding a list reached `parameter_devices`
        and came back as an AttributeError - an internal error where the tool documents a
        ToolParamError, which is the difference between a client being told and a client
        seeing the server fall over."""
        import yaml

        from toinflux.controls import control_path

        document = conservatory(name="broken")
        document["devices"] = ["heater1", "heater2"]
        path = control_path("broken", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(document, handle)
        result = _control_state_result("broken", state_directory.settings_file)
        assert result["control"] == "broken"
        assert result["held_by_minimum"] == []

    def test_a_control_that_has_never_run_reports_no_loop(self, state_directory):
        state_directory.write_control(conservatory())
        result = _control_state_result("conservatory", state_directory.settings_file)
        assert result["loop"] is None
        assert result["devices"] == {}

    def test_a_name_that_does_not_exist_is_refused(self, state_directory):
        with pytest.raises(ToolParamError, match="nosuchcontrol"):
            _control_state_result("nosuchcontrol", state_directory.settings_file)
