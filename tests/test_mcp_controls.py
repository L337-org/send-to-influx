"""The read-only control tools: what they say, and what they refuse to guess.

Most of these are about the three-valued answer. "Is this control running" has a third
answer - nothing is supervising, so nothing knows - and collapsing it into `false` would
send a caller looking for a crash that never happened.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import os

import anyio
import pytest
import yaml
from mcp.server.mcpserver import MCPServer

from tests.harness.installation import conservatory
from toinflux.exceptions import ConfigError
from toinflux.mcp_controls import _get_control_result, _list_controls_result, register_control_tools
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

    def test_both_tools_are_registered_when_controls_are_on(self):
        assert self._tools({"controls": {"enabled": True}}) == {"list_controls", "get_control"}

    def test_reading_is_not_gated_behind_the_write_flag(self):
        """A control document holds no secrets, and gating "what is this install controlling"
        behind the switch that permits changing a heating loop would mean nobody could look
        without also granting that. No write flag is set here and both tools appear."""
        assert self._tools({"controls": {"enabled": True}}) == {"list_controls", "get_control"}
