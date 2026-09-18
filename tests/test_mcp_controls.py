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
        assert self._tools({"controls": {"enabled": True}}) == {"list_controls", "get_control", "get_control_schema"}

    def test_reading_is_not_gated_behind_the_write_flag(self):
        """A control document holds no secrets, and gating "what is this install controlling"
        behind the switch that permits changing a heating loop would mean nobody could look
        without also granting that. No write flag is set here and every read tool appears,
        `get_control_schema` included - so an installation where nothing may write controls can
        still be asked for a document to paste in, or to explain one written by hand."""
        assert self._tools({"controls": {"enabled": True}}) == {"list_controls", "get_control", "get_control_schema"}


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

    def test_the_example_it_hands_out_is_valid(self):
        """The whole reason the example lives in the store rather than in prose. The design
        note's copy of this example was invalid for as long as it existed - it read
        `outside` in `enable_when` and declared no such input - and nothing could see it
        until rules were validated. An example a client is told to start from must be one
        the validator accepts."""
        from toinflux.controls import validate_control

        example = _control_schema_result(self.SETTINGS)["example"]
        assert validate_control(example["name"], example) == []

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

    def test_hand_saving_advice_admits_a_restart_is_needed_when_nothing_is_supervising(self):
        """The wrong advice would land on precisely the person most likely to read it."""
        assert "restarted" in self._writing(False, None)["meanwhile"]

    def test_hand_saving_advice_says_no_restart_is_needed_when_something_is_supervising(self):
        assert "without a restart" in self._writing(False, _Reloading())["meanwhile"]

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
        assert result["changed"] == {"sections": [], "actuation": []}

    def test_changing_one_parameter_names_only_that_section(self, stored):
        """The case the report exists to make boring: an adjustment that touched nothing else."""
        result = self._save(stored, parameters={"target": 21.0})
        assert result["changed"]["sections"] == ["parameters"]
        assert result["changed"]["actuation"] == []

    def test_a_rewritten_stage_ladder_is_called_out_as_actuation(self, stored):
        """The case it exists to catch. This document validates; it simply is not the one
        that was there, and the rungs decide what the heaters do."""
        output = conservatory()["output"]
        output["stages"] = [stage for stage in output["stages"] if stage["level"] != 750]
        result = self._save(stored, output=output)
        assert "output" in result["changed"]["actuation"]

    def test_it_says_so_in_the_journal_too(self, stored, caplog):
        """The result reaches the client; the operator reads the journal."""
        output = conservatory()["output"]
        output["stages"] = [stage for stage in output["stages"] if stage["level"] != 750]
        with caplog.at_level(logging.WARNING):
            self._save(stored, output=output)
        assert "changed what its devices do" in caplog.text
        assert "conservatory" in caplog.text

    def test_a_parameter_change_does_not_warn(self, stored, caplog):
        """A warning on every ordinary edit is a warning nobody reads."""
        with caplog.at_level(logging.WARNING):
            self._save(stored, parameters={"target": 21.0})
        assert "changed what its devices do" not in caplog.text

    def test_an_unreadable_stored_document_reports_everything_as_changed(self, stored):
        """Not "nothing changed", which would be reassuring and wrong: going from a document
        that will not parse to one that does is the largest change there is."""
        with open(os.path.join(stored.state_dir, "controls", "conservatory.yaml"), "w", encoding="utf-8") as handle:
            handle.write("{{{ not yaml")
        result = self._save(stored)
        assert "output" in result["changed"]["actuation"]
        assert "parameters" in result["changed"]["sections"]
