"""Unit tests for toinflux.controls (the control store and its structural validation)."""

import copy
import os
import stat as stat_module
import pytest
import pathlib
import re

import yaml
from toinflux.controls import (
    actuators_may_be_one,
    shared_actuator_problems,
    BUILT_IN_SAFE_STATES,
    CONTROL_EXAMPLE,
    control_warnings,
    CONTROL_EXAMPLES,
    control_dir,
    control_path,
    control_writes_enabled,
    delete_control,
    list_controls,
    load_control,
    require_valid_control_name,
    rule_names,
    save_control,
    validate_control,
    validate_control_rules,
    validate_control_sources,
    validate_control_structure,
    validate_stored_controls,
)
from toinflux.exceptions import ConfigError


def a_valid_control():
    """Return a structurally sound control document.

    A deep copy of the example the store ships, so the shape the documentation hands out
    and the shape the code accepts are one thing rather than two that drift. Copied because
    almost every test below mutates what it is given.

    Returns:
        dict: a document that ``validate_control`` finds no fault with
    """
    return copy.deepcopy(CONTROL_EXAMPLE)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the store at a temporary state directory.

    Args:
        tmp_path (pathlib.Path): pytest's per-test directory
        monkeypatch (pytest.MonkeyPatch): used to set STATE_DIRECTORY

    Returns:
        pathlib.Path: the directory the store will resolve to
    """
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    return tmp_path


class TestWhereControlsLive:
    def test_the_store_sits_under_the_systemd_state_directory(self, state_dir):
        assert control_dir() == os.path.join(str(state_dir), "controls")

    def test_off_systemd_it_falls_back_beside_the_settings_file(self, monkeypatch):
        # A source checkout is treated as first class, and there is no StateDirectory
        # there. Anchored on the settings file rather than the working directory so the
        # answer does not depend on where the process happened to be started.
        monkeypatch.delenv("STATE_DIRECTORY", raising=False)
        assert control_dir("/etc/send-to-influx/settings.yaml").startswith("/etc/send-to-influx")

    def test_a_colon_separated_state_directory_takes_the_first(self, monkeypatch, tmp_path):
        # systemd joins several StateDirectory= entries with the path separator. Taking
        # the first means adding a second later cannot silently move the store.
        first, second = tmp_path / "one", tmp_path / "two"
        monkeypatch.setenv("STATE_DIRECTORY", f"{first}{os.pathsep}{second}")
        assert control_dir().startswith(str(first))


class TestControlNames:
    @pytest.mark.parametrize("name", ["conservatory", "a", "hall-lamp", "zone_2", "x9"])
    def test_accepts_ordinary_names(self, name):
        require_valid_control_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "../escape",
            "/etc/passwd",
            "nested/name",
            "..",
            ".",
            "",
            "Conservatory",
            "has space",
            "-leading",
            "_leading",
            "x" * 64,
            None,
            17,
            # `$` matches before a trailing newline, so an anchored pattern checked with
            # match() is not a whole-string test - this passed the allow-list until the
            # check became fullmatch. It matters here more than most places: the name
            # becomes a filename and an MCP client chooses it.
            "conservatory\n",
            "conservatory\nrm -rf",
        ],
    )
    def test_refuses_anything_that_could_choose_a_different_file(self, name):
        # A control name arrives from an MCP client and is concatenated into a path, so
        # this is the boundary that stops a caller picking which file gets written.
        with pytest.raises(ConfigError):
            require_valid_control_name(name)

    def test_a_traversing_name_never_reaches_the_filesystem(self, state_dir):
        with pytest.raises(ConfigError):
            control_path("../../etc/cron.d/evil")

    def test_the_refusal_says_what_is_allowed(self, state_dir):
        with pytest.raises(ConfigError) as exc:
            require_valid_control_name("Not Valid")
        message = str(exc.value)
        assert "Not Valid" in message
        assert "lower-case" in message


class TestSaveAndLoad:
    def test_a_document_survives_a_round_trip(self, state_dir):
        save_control("conservatory", a_valid_control())
        assert load_control("conservatory") == a_valid_control()

    def test_saving_creates_the_directory_restrictively(self, state_dir):
        save_control("conservatory", a_valid_control())
        mode = stat_module.S_IMODE(os.stat(control_dir()).st_mode)
        assert mode == stat_module.S_IRWXU

    def test_a_stored_control_is_not_world_readable(self, state_dir):
        # These documents name devices and can carry a rule referencing anything the
        # estate collects. Not secret, but no reason for every local user to read them.
        save_control("conservatory", a_valid_control())
        mode = stat_module.S_IMODE(os.stat(control_path("conservatory")).st_mode)
        assert mode == stat_module.S_IRUSR | stat_module.S_IWUSR

    def test_saving_again_replaces_rather_than_appends(self, state_dir):
        save_control("conservatory", a_valid_control())
        changed = a_valid_control()
        changed["parameters"]["target"] = 21.0
        save_control("conservatory", changed)
        assert load_control("conservatory")["parameters"]["target"] == 21.0

    def test_a_failed_write_leaves_no_temporary_file_behind(self, state_dir):
        # yaml.safe_dump refuses an object it has no representer for. The partial file
        # must not survive, or a failing MCP call would litter the store on every retry.
        with pytest.raises(ConfigError):
            save_control("conservatory", {"inputs": object()})
        leftovers = [entry for entry in os.listdir(control_dir()) if entry.startswith(".")]
        assert leftovers == []

    def test_loading_something_that_does_not_exist_says_so(self, state_dir):
        with pytest.raises(ConfigError, match="no control named"):
            load_control("conservatory")

    def test_an_empty_document_is_distinguished_from_a_malformed_one(self, state_dir):
        os.makedirs(control_dir(), exist_ok=True)
        with open(control_path("conservatory"), "w", encoding="utf-8") as handle:
            handle.write("")
        with pytest.raises(ConfigError, match="is empty"):
            load_control("conservatory")

    def test_a_document_that_is_not_a_mapping_is_refused(self, state_dir):
        os.makedirs(control_dir(), exist_ok=True)
        with open(control_path("conservatory"), "w", encoding="utf-8") as handle:
            handle.write("- just\n- a list\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_control("conservatory")

    def test_invalid_yaml_is_reported_as_such(self, state_dir):
        os.makedirs(control_dir(), exist_ok=True)
        with open(control_path("conservatory"), "w", encoding="utf-8") as handle:
            handle.write("inputs: [unclosed\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_control("conservatory")


class TestListAndDelete:
    def test_no_store_yet_is_not_a_fault(self, state_dir):
        # The whole subsystem is optional, so an installation that has never made a
        # control must not report an error for the directory being absent.
        assert list_controls() == []

    def test_lists_stored_controls_in_order(self, state_dir):
        for name in ("landing", "conservatory", "porch"):
            save_control(name, a_valid_control() | {"name": name})
        assert list_controls() == ["conservatory", "landing", "porch"]

    def test_ignores_files_that_are_not_control_documents(self, state_dir):
        save_control("conservatory", a_valid_control())
        with open(os.path.join(control_dir(), "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("scratch")
        assert list_controls() == ["conservatory"]

    def test_a_file_with_an_unusable_name_is_skipped_with_a_warning(self, state_dir, caplog):
        # One bad file must not make every other control invisible, but it cannot pass
        # silently either - the operator has a document that is being ignored.
        save_control("conservatory", a_valid_control())
        with open(os.path.join(control_dir(), "Not Valid.yaml"), "w", encoding="utf-8") as handle:
            handle.write("inputs: {}\n")
        with caplog.at_level("WARNING"):
            assert list_controls() == ["conservatory"]
        assert "Not Valid.yaml" in caplog.text

    def test_deleting_removes_the_document(self, state_dir):
        save_control("conservatory", a_valid_control())
        delete_control("conservatory")
        assert list_controls() == []

    def test_deleting_something_absent_says_so(self, state_dir):
        with pytest.raises(ConfigError, match="no control named"):
            delete_control("conservatory")


class TestStructuralValidation:
    def test_the_documented_example_is_accepted(self):
        assert validate_control("conservatory", a_valid_control()) == []

    def test_an_unknown_key_is_reported_rather_than_ignored(self):
        # A silently ignored key leaves the operator looking at a setting they believe
        # is in force and is not, which is worse than a typo that fails loudly.
        document = a_valid_control() | {"cycle_secconds": 900}
        assert any("cycle_secconds" in error for error in validate_control("conservatory", document))

    @pytest.mark.parametrize("key", ["inputs", "pid", "output", "devices"])
    def test_a_missing_required_section_is_reported(self, key):
        document = a_valid_control()
        del document[key]
        assert any(error.startswith(f"{key}:") for error in validate_control("conservatory", document))

    def test_a_name_disagreeing_with_the_filename_is_refused(self):
        document = a_valid_control() | {"name": "somewhere-else"}
        assert any("but the file is named" in error for error in validate_control("conservatory", document))

    def test_every_problem_is_collected_rather_than_only_the_first(self):
        # An operator writing one of these by hand would otherwise fix a single typo per
        # run and come back four times.
        document = a_valid_control() | {"enabled": "yes", "safe_state": "off", "timezone": "Mars/Olympus"}
        assert len(validate_control("conservatory", document)) >= 3

    def test_a_bogus_timezone_is_caught_at_load_not_at_a_dst_boundary(self):
        document = a_valid_control() | {"timezone": "Europe/Narnia"}
        assert any("time zone" in error for error in validate_control("conservatory", document))

    def test_safe_state_must_be_one_of_the_two(self):
        document = a_valid_control() | {"safe_state": "off"}
        errors = validate_control("conservatory", document)
        assert any(all(state in error for state in BUILT_IN_SAFE_STATES) for error in errors)

    def test_a_non_numeric_parameter_is_reported(self):
        document = a_valid_control()
        document["parameters"]["target"] = "eighteen"
        assert any("parameters['target']" in error for error in validate_control("conservatory", document))


class TestTheStageLadder:
    def test_a_stage_that_forgets_a_device_is_refused(self):
        # The rule that matters most here. A stage saying nothing about a device is not
        # the same as a stage turning it off, and reading the ladder an operator would
        # assume it was off - which is exactly how a heater silently stays on.
        document = a_valid_control()
        del document["output"]["stages"][0]["set"]["heater_near"]
        errors = validate_control("conservatory", document)
        assert any("heater_near" in error and "every stage must assign every device" in error for error in errors)

    def test_a_stage_naming_a_device_the_control_does_not_own_is_refused(self):
        document = a_valid_control()
        document["output"]["stages"][0]["set"]["heater_middle"] = False
        assert any("no such device" in error for error in validate_control("conservatory", document))

    def test_a_level_must_be_a_number(self):
        document = a_valid_control()
        document["output"]["stages"][1]["level"] = "high"
        assert any("level" in error for error in validate_control("conservatory", document))

    def test_a_boolean_level_is_not_a_number(self):
        # bool subclasses int in Python, so `level: true` would otherwise validate and
        # then sort as 1, quietly placing a rung between 0 and 750.
        document = a_valid_control()
        document["output"]["stages"][1]["level"] = True
        assert any("level" in error for error in validate_control("conservatory", document))

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param("false", id="a-quoted-false"),
            pytest.param("no", id="a-quoted-no"),
            pytest.param("off", id="a-quoted-off"),
            pytest.param(0, id="an-integer-zero"),
            pytest.param(1, id="an-integer-one"),
            pytest.param([], id="a-list"),
            pytest.param(None, id="a-null"),
        ],
    )
    def test_a_stage_state_that_is_not_a_boolean_is_refused(self, state):
        """The loop commands `on=bool(state)`, and every non-empty string is truthy - so a
        quoted `"false"`, which is what somebody writes when they are being careful with
        YAML, switched the device **on**. On the level 0 rung that turned the everything-off
        rung into an everything-on rung, and it passed --check-config clean.

        Unquoted `off`/`no`/`false` are YAML booleans and were always right, which is what
        made this invisible: the wrong version looks more careful than the right one.
        Integers are refused too rather than tolerated through `bool(0)`, because working by
        accident is what this whole check is about.
        """
        document = a_valid_control()
        document["output"]["stages"][0]["set"]["heater_far"] = state
        errors = validate_control("conservatory", document)
        assert any("must be true or false" in error and "heater_far" in error for error in errors), errors

    def test_real_booleans_are_accepted(self):
        """The check must not refuse the documents everybody actually has."""
        document = a_valid_control()
        document["output"]["stages"][0]["set"] = dict.fromkeys(document["devices"], False)
        assert validate_control("conservatory", document) == []

    def test_an_empty_ladder_is_refused(self):
        document = a_valid_control()
        document["output"]["stages"] = []
        assert any("output.stages" in error for error in validate_control("conservatory", document))

    def test_a_negative_cycle_time_is_refused(self):
        document = a_valid_control()
        document["output"]["cycle_seconds"] = -1
        assert any("cycle_seconds" in error for error in validate_control("conservatory", document))


class TestInputsAndDevices:
    def test_an_input_without_a_field_is_refused(self):
        document = a_valid_control()
        del document["inputs"]["dew"]["field"]
        assert any("inputs['dew']" in error for error in validate_control("conservatory", document))

    def test_a_device_without_a_source_is_refused(self):
        document = a_valid_control()
        del document["devices"]["heater_far"]["source"]
        assert any("devices['heater_far']" in error for error in validate_control("conservatory", document))

    def test_a_negative_max_age_is_refused(self):
        document = a_valid_control()
        document["inputs"]["dew"]["max_age"] = 0
        assert any("max_age" in error for error in validate_control("conservatory", document))


class TestActivePeriod:
    def test_a_well_formed_period_is_accepted(self):
        assert validate_control("conservatory", a_valid_control()) == []

    @pytest.mark.parametrize("value", ["25:00", "7:30", "0730", "half past", "23:60", ""])
    def test_a_malformed_time_is_refused(self, value):
        document = a_valid_control()
        document["active_period"]["from"] = value
        assert any("active_period.from" in error for error in validate_control("conservatory", document))

    def test_an_unknown_end_state_is_refused(self):
        document = a_valid_control()
        document["active_period"]["end_state"] = "dim"
        assert any("active_period.end_state" in error for error in validate_control("conservatory", document))


class TestARequiredSectionWithNothingUnderIt:
    """`inputs:` with nothing indented under it parses to None, so the key is present and
    the section is not. Every shape check treats None as absent and says nothing, and the
    rule check skips because there are no names to resolve against - so the document passed
    with no complaint at all while naming inputs that can never exist."""

    @pytest.mark.parametrize("key", ["inputs", "pid", "output", "devices"])
    def test_it_is_refused(self, key):
        document = a_valid_control()
        document[key] = None
        errors = validate_control_structure("conservatory", document)
        assert any(key in error and "nothing under it" in error for error in errors), errors

    def test_the_whole_document_does_not_pass_silently(self):
        """Read from YAML rather than built as a dict, because the shape only arises from
        someone writing `inputs:` and forgetting to indent the block under it."""
        document = yaml.safe_load(
            "name: conservatory\n"
            "inputs:\n"
            'pid: {input: "inside", setpoint: "max(target, dew + 5)"}\n'
            "output: {cycle_seconds: 900, min_transition_seconds: 60, "
            "stages: [{level: 0, set: {far: false}}]}\n"
            'devices: {far: {source: hue, device: "Far"}}\n'
        )
        assert document["inputs"] is None
        assert validate_control("conservatory", document)


class TestValidatingTheRules:
    """The half that only ran when a control process started. A document with a malformed
    expression passed every structural check, was written, and killed the control at
    startup - so the operator learned about a typo from a dead heater rather than from the
    command whose whole job is to say whether the configuration is usable."""

    @pytest.mark.parametrize(
        "slot,put",
        [
            pytest.param("pid.setpoint", lambda d, v: d["pid"].__setitem__("setpoint", v), id="setpoint"),
            pytest.param("pid.input", lambda d, v: d["pid"].__setitem__("input", v), id="input"),
            pytest.param("output.max_level", lambda d, v: d["output"].__setitem__("max_level", v), id="max-level"),
            pytest.param("enable_when", lambda d, v: d.__setitem__("enable_when", v), id="enable-when"),
        ],
    )
    def test_every_slot_the_runtime_parses_is_checked(self, slot, put):
        document = a_valid_control()
        put(document, "max(target, dew +")
        errors = validate_control_rules(document)
        assert [error for error in errors if error.startswith(f"{slot}:")], errors

    def test_a_name_nothing_declares_is_reported_with_what_is_declared(self):
        """The fault this found on its first run, in this file's own fixture and in the
        design note's worked example: an `enable_when` reading a name no input declares."""
        document = a_valid_control()
        document["enable_when"] = "nosuchreading < 15"
        (error,) = validate_control_rules(document)
        assert "nosuchreading" in error and "enable_when" in error

    def test_an_absent_optional_slot_is_not_a_problem(self):
        document = a_valid_control()
        del document["output"]["max_level"]
        del document["enable_when"]
        assert validate_control_rules(document) == []

    def test_a_slot_that_is_not_text_is_left_to_the_structural_check(self):
        """Reported once, by the half that understands it. Twice would have an operator
        looking for two faults."""
        document = a_valid_control()
        document["pid"]["setpoint"] = 18.0
        assert validate_control_rules(document) == []
        assert any("pid.setpoint" in error for error in validate_control_structure("conservatory", document))

    def test_a_missing_required_name_source_stops_it_too(self):
        """`inputs` is required, so a document without it has none of the names its rules
        will read - every one of them would be reported as undeclared on top of the single
        structural error saying the section is missing."""
        document = a_valid_control()
        del document["inputs"]
        assert validate_control_rules(document) == []
        assert any("inputs" in error for error in validate_control_structure("conservatory", document))

    def test_an_absent_optional_name_source_suppresses_nothing(self):
        """`parameters` is optional, so an absent one is ordinary rather than a fault, and
        must not stop the rules being checked - which is the other half of the same rule and
        the one a blanket "not a mapping" test would get wrong."""
        document = a_valid_control()
        del document["parameters"]
        document["pid"]["setpoint"] = "max(nosuchname, dew + 5)"
        assert any("nosuchname" in error for error in validate_control_rules(document))

    def test_a_broken_name_source_stops_the_rule_check_rather_than_cascading(self):
        """With `inputs` not a mapping there are no declared names, so every rule would
        report every name it uses as undeclared - a cascade of consequences from the one
        fault the structural check already names precisely."""
        document = a_valid_control()
        document["inputs"] = 7
        assert validate_control_rules(document) == []
        assert any("inputs" in error for error in validate_control_structure("conservatory", document))

    def test_the_wrapper_reports_both_halves_at_once(self):
        document = a_valid_control()
        document["enabled"] = "yes"
        document["enable_when"] = "nosuchreading < 15"
        errors = validate_control("conservatory", document)
        assert any("enabled" in error for error in errors)
        assert any("enable_when" in error for error in errors)

    def test_the_names_a_rule_may_use_are_the_inputs_then_the_parameters(self):
        """Shared with the controller and the gate, which both used to build it themselves.
        The order matters even though the parser only needs the set: the store refuses a
        name declared as both, and this ordering is what would decide the winner if that
        were ever relaxed."""
        document = {"inputs": {"inside": {}, "dew": {}}, "parameters": {"target": 1}}
        assert rule_names(document) == ("inside", "dew", "target")

    def test_no_names_where_a_section_is_the_wrong_shape(self):
        assert rule_names({"inputs": 7, "parameters": None}) == ()


class TestValidatingTheSources:
    """Whether a source exists, and whether it can switch a device, are facts about the
    code rather than about the document - so the file cannot answer them about itself, and
    nothing did until a control failed on its first command."""

    def test_a_device_source_that_cannot_actuate_is_refused(self):
        document = a_valid_control()
        document["devices"]["heater_far"]["source"] = "speedtest"
        (error,) = validate_control_sources(document)
        assert "devices['heater_far']" in error
        assert "cannot switch a device" in error

    def test_a_writable_source_is_not_enough(self):
        """The distinction the whole check exists for: Speedtest offers a write path, and it
        triggers a speed test. `MCP_WRITABLE` is the wrong question for a devices entry."""
        from toinflux.general import source_class

        assert source_class("speedtest").MCP_WRITABLE is True
        assert getattr(source_class("speedtest"), "MCP_ACTUATES_DEVICES", False) is False

    def test_a_source_this_build_does_not_collect_from_is_refused(self):
        document = a_valid_control()
        document["inputs"]["inside"]["source"] = "nosuchsource"
        (error,) = validate_control_sources(document)
        assert "inputs['inside']" in error and "nosuchsource" in error

    def test_an_input_source_need_not_actuate(self):
        """Only a devices entry needs the narrow capability. An input is read, not switched,
        so every collected source is fair game - and requiring otherwise would leave a
        control unable to read the weather."""
        document = a_valid_control()
        assert any(spec["source"] == "openmeteo" for spec in document["inputs"].values())
        assert validate_control_sources(document) == []

    def test_a_non_string_entry_name_is_left_to_the_structural_check(self):
        """The structural check already reports a key that is not a string, in its own
        vocabulary. A source fault against the same key would name one broken thing twice."""
        document = a_valid_control()
        document["devices"] = {1: {"source": "speedtest", "device": "x"}}
        assert validate_control_sources(document) == []
        assert any("entry names must be strings" in error for error in validate_control_structure("c", document))

    def test_structure_is_not_reported_twice(self):
        """Each of these is named precisely by the structural check. Said twice, an operator
        looks for two faults."""
        for broken in ({"devices": 7}, {"devices": {"far": 7}}, {"devices": {"far": {"device": "x"}}}):
            document = a_valid_control() | broken
            assert validate_control_sources(document) == [], broken

    def test_the_wrapper_runs_all_three_halves(self):
        document = a_valid_control()
        document["enabled"] = "yes"
        document["enable_when"] = "nosuchreading < 15"
        document["devices"]["heater_far"]["source"] = "speedtest"
        errors = validate_control("conservatory", document)
        assert any("enabled" in error for error in errors)
        assert any("enable_when" in error for error in errors)
        assert any("cannot switch a device" in error for error in errors)


class TestValidatingTheWholeStore:
    def test_a_store_with_nothing_in_it_passes(self, state_dir):
        validate_stored_controls()

    def test_a_sound_store_passes(self, state_dir):
        save_control("conservatory", a_valid_control())
        validate_stored_controls()

    def test_one_broken_control_fails_the_check_and_names_it(self, state_dir):
        save_control("conservatory", a_valid_control())
        broken = a_valid_control() | {"name": "landing"}
        del broken["output"]["stages"][0]["set"]["heater_near"]
        save_control("landing", broken)
        with pytest.raises(ConfigError) as exc:
            validate_stored_controls()
        assert "landing" in str(exc.value)
        assert "conservatory" not in str(exc.value)

    def test_every_broken_control_is_reported_in_one_run(self, state_dir):
        for name in ("landing", "porch"):
            document = a_valid_control() | {"name": name, "enabled": "yes"}
            save_control(name, document)
        with pytest.raises(ConfigError) as exc:
            validate_stored_controls()
        assert "landing" in str(exc.value) and "porch" in str(exc.value)

    def test_a_control_whose_rule_will_not_parse_fails_the_check(self, state_dir):
        """The acceptance behaviour the design note has always claimed and the code did
        not do: until the rule check existed this document passed --check-config, was
        written, and killed the control at startup instead."""
        document = a_valid_control()
        document["pid"]["setpoint"] = "max(target, dew +"
        save_control("conservatory", document)
        with pytest.raises(ConfigError) as exc:
            validate_stored_controls()
        assert "pid.setpoint" in str(exc.value)

    def test_a_control_naming_an_undeclared_input_fails_the_check(self, state_dir):
        document = a_valid_control()
        document["enable_when"] = "nosuchreading < 15"
        save_control("conservatory", document)
        with pytest.raises(ConfigError) as exc:
            validate_stored_controls()
        assert "nosuchreading" in str(exc.value)

    def test_an_unparseable_document_is_reported_rather_than_crashing_the_check(self, state_dir):
        os.makedirs(control_dir(), exist_ok=True)
        with open(control_path("conservatory"), "w", encoding="utf-8") as handle:
            yaml.safe_dump(["not", "a", "mapping"], handle)
        with pytest.raises(ConfigError, match="must be a mapping"):
            validate_stored_controls()


class TestDocumentsWrittenToBreakTheValidator:
    """A control document is external input: an MCP client writes one, and YAML permits
    shapes a hand-written example never shows. Validation has to report on those, not
    fall over on them - a validator that crashes tells the operator nothing about the
    document that caused it."""

    def test_non_string_keys_are_reported_rather_than_crashing_the_check(self):
        # YAML mapping keys need not be strings. Sorting a mixed set raises TypeError and
        # joining non-strings raises too, so validation used to die here instead of
        # describing the document.
        document = yaml.safe_load(
            "1: stray\n"
            "inputs: {}\n"
            "pid: {input: a, setpoint: b}\n"
            "devices:\n"
            "  2: {source: hue, device: x}\n"
            "  heater: {source: hue, device: y}\n"
            "output:\n"
            "  stages:\n"
            "    - level: 0\n"
            "      set: {3: false, heater: false}\n"
        )
        errors = validate_control("conservatory", document)
        assert any("unknown key" in error for error in errors)
        assert any("entry names must be strings" in error for error in errors)

    def test_a_mix_of_string_and_numeric_keys_still_sorts(self):
        # The specific crash: sorted() over {1, 'heater'} has no total order in Python 3.
        document = a_valid_control()
        document["output"]["stages"][0]["set"] = {1: False, "heater_far": False, "heater_near": False}
        assert any("no such device" in error for error in validate_control("conservatory", document))

    def test_a_newline_in_a_stage_device_name_cannot_forge_a_line_either(self):
        # The list-rendering path, which is separate from the per-entry one below: an
        # unknown device in a stage is reported as a joined list, and that join is where
        # a raw str() would let the newline through.
        document = a_valid_control()
        document["output"]["stages"][0]["set"]["ghost\nWARNING forged"] = False
        offending = [error for error in validate_control("conservatory", document) if "forged" in error]
        assert offending, "the unknown device was not reported at all"
        assert "\n" not in offending[0]
        assert "\\n" in offending[0]

    def test_a_newline_in_a_key_cannot_forge_a_second_diagnostic_line(self):
        # The same rule the collectors follow for a lock name arriving over MQTT: an
        # external value goes into a message quoted, or one containing a newline writes
        # its own line into the journal and into any connected MCP client's output.
        document = a_valid_control()
        document["inputs"]["bad\nWARNING forged"] = {"source": "hue"}
        errors = validate_control("conservatory", document)
        offending = [error for error in errors if "forged" in error]
        assert offending, "the malformed input was not reported at all"
        assert "\n" not in offending[0]
        assert "\\n" in offending[0]


class TestWhetherWritingControlsIsPermitted:
    """`control_writes_enabled` answers for both switches, not just its own.

    The registration path checks `controls_enabled` before it ever asks this, so these are
    about the predicate's own contract rather than about today's behaviour: a helper that
    half-answers its own question is one a later call site gets wrong, and it is the kind of
    wrong that grants a capability rather than withholding one.
    """

    @pytest.mark.parametrize(
        "controls",
        [
            pytest.param({"enabled": True, "mcp_write": True}, id="both-on"),
        ],
    )
    def test_both_switches_on_permits_writing(self, controls):
        assert control_writes_enabled({"controls": controls}) is True

    @pytest.mark.parametrize(
        "controls",
        [
            pytest.param({"mcp_write": True}, id="write-without-the-subsystem"),
            pytest.param({"enabled": False, "mcp_write": True}, id="subsystem-explicitly-off"),
            pytest.param({"enabled": True}, id="no-write-key"),
            pytest.param({"enabled": True, "mcp_write": False}, id="write-off"),
            pytest.param({"enabled": True, "mcp_write": "true"}, id="a-quoted-yaml-boolean-is-a-string"),
            pytest.param({"enabled": "true", "mcp_write": True}, id="the-subsystem-switch-is-a-string-too"),
            pytest.param({}, id="empty-block"),
        ],
    )
    def test_anything_short_of_both_does_not(self, controls):
        assert control_writes_enabled({"controls": controls}) is False

    @pytest.mark.parametrize("settings", [{}, {"controls": None}, {"controls": "yes"}, {"controls": []}])
    def test_a_block_that_is_not_a_mapping_does_not_permit_writing(self, settings):
        assert control_writes_enabled(settings) is False


class TestTwoKeysForOneActuator:
    """Two device keys naming one physical light are not two devices, and the ladder treats
    them as though they were: a stage may set one true and the other false, both commands go
    out, and which one lands last is decided by mapping order. That validated cleanly."""

    def test_two_keys_for_the_same_device_are_refused(self):
        document = a_valid_control()
        document["devices"] = {
            "a": {"source": "hue", "device": "heater_far"},
            "b": {"source": "hue", "device": "heater_far"},
        }
        document["output"]["stages"] = [
            {"level": 0, "set": {"a": False, "b": False}},
            {"level": 750, "set": {"a": True, "b": False}},
        ]
        assert any("same actuator" in error for error in validate_control("conservatory", document))

    def test_the_message_names_both_keys_and_the_device(self):
        """So the fix is obvious from the error rather than from reading the ladder."""
        document = a_valid_control()
        document["devices"] = {
            "a": {"source": "hue", "device": "heater_far"},
            "b": {"source": "hue", "device": "heater_far"},
        }
        document["output"]["stages"] = [{"level": 0, "set": {"a": False, "b": False}}]
        error = next(e for e in validate_control("conservatory", document) if "same actuator" in e)
        assert "'a'" in error and "'b'" in error and "heater_far" in error

    def test_the_same_device_name_on_different_sources_is_fine(self):
        """Identity is the whole triple. Two sources may each have a device called `far` and
        they are not the same actuator."""
        document = a_valid_control()
        document["devices"] = {
            "a": {"source": "hue", "device": "heater_far"},
            "b": {"source": "philipshue", "device": "heater_far"},
        }
        document["output"]["stages"] = [{"level": 0, "set": {"a": False, "b": False}}]
        assert not [e for e in validate_control("conservatory", document) if "same actuator" in e]

    def test_different_instances_of_one_device_name_are_fine(self):
        """Two bridges each with a light called `heater_far` are two actuators."""
        document = a_valid_control()
        document["devices"] = {
            "a": {"source": "hue", "device": "heater_far", "instance": "bridge1"},
            "b": {"source": "hue", "device": "heater_far", "instance": "bridge2"},
        }
        document["output"]["stages"] = [{"level": 0, "set": {"a": False, "b": False}}]
        assert not [e for e in validate_control("conservatory", document) if "same actuator" in e]

    def test_a_source_spelled_differently_is_the_same_actuator(self):
        """`source_class` resolves a source name case-insensitively, so `Hue` and `hue` are
        one handler commanding one device. Left verbatim they were two identities and slipped
        past this check entirely."""
        document = a_valid_control()
        document["devices"] = {
            "a": {"source": "hue", "device": "heater_far"},
            "b": {"source": "HUE", "device": "heater_far"},
        }
        document["output"]["stages"] = [{"level": 0, "set": {"a": False, "b": False}}]
        assert any("same actuator" in error for error in validate_control("conservatory", document))

    def test_a_device_spelled_differently_is_not(self):
        """The opposite direction, and the worse failure: a device name is the bridge's own
        and the bridge tells two lights apart by case, so folding it would refuse a
        configuration that works."""
        document = a_valid_control()
        document["devices"] = {
            "a": {"source": "hue", "device": "heater_far"},
            "b": {"source": "hue", "device": "Heater_Far"},
        }
        document["output"]["stages"] = [{"level": 0, "set": {"a": False, "b": False}}]
        assert not [e for e in validate_control("conservatory", document) if "same actuator" in e]

    def test_an_ordinary_document_is_unaffected(self):
        assert validate_control("conservatory", a_valid_control()) == []


class TestOnlyOneEnabledControlPerActuator:
    """Two loops commanding one heater fight: each runs its own PID against its own setpoint
    and each applies its own safe state, so whichever commanded last wins until the other's
    next cycle. Neither can detect the other, and nothing inside a single document can see
    it, which is why this is checked across them."""

    @staticmethod
    def _pair(**second):
        """Return two controls that both command the example's devices.

        Args:
            **second: top-level keys for the second control

        Returns:
            dict: name to document
        """
        return {"conservatory": a_valid_control(), "spare": dict(a_valid_control(), name="spare", **second)}

    def test_two_enabled_controls_sharing_an_actuator_is_a_problem(self):
        problems = shared_actuator_problems(self._pair())
        assert problems, "two enabled controls on one heater was accepted"
        assert "conservatory" in problems[0] and "spare" in problems[0]

    def test_the_problem_names_the_actuator(self):
        """The physical device as the bridge names it, not the document's key for it - an
        operator chasing a hot room looks at the heater, not at somebody's YAML."""
        assert "Conservatory heater far" in " ".join(shared_actuator_problems(self._pair()))

    def test_a_disabled_duplicate_is_allowed(self):
        """Preparing a replacement before switching over is a workflow, not a fault - and it
        is exactly what `save_control` does when it stores a clashing control disabled."""
        assert shared_actuator_problems(self._pair(enabled=False)) == []

    def test_a_source_spelled_differently_still_clashes_across_documents(self):
        """The same canonicalisation, checked at the other scope - the identity is built in
        one place now precisely so these two cannot drift apart again."""
        documents = self._pair()
        documents["spare"]["devices"] = {
            key: dict(spec, source=spec["source"].upper()) for key, spec in documents["spare"]["devices"].items()
        }
        assert shared_actuator_problems(documents), "a differently-spelled source bypassed the rule"

    def test_one_control_alone_is_fine(self):
        assert shared_actuator_problems({"conservatory": a_valid_control()}) == []

    def test_controls_on_different_actuators_are_fine(self):
        documents = self._pair()
        documents["spare"]["devices"] = {"porch": {"source": "hue", "device": "porch-heater"}}
        documents["spare"]["output"]["stages"] = [{"level": 0, "set": {"porch": False}}]
        assert shared_actuator_problems(documents) == []

    def test_the_answer_does_not_depend_on_listing_order(self):
        """The same pair must give the same answer on every machine and every start."""
        documents = self._pair()
        forwards = shared_actuator_problems(documents)
        backwards = shared_actuator_problems(dict(reversed(list(documents.items()))))
        assert forwards == backwards

    def test_check_config_actually_refuses_the_pair(self, state_directory):
        """Through `validate_stored_controls`, not through the helper.

        The tests above call `shared_actuator_problems` directly, which would pass whether or
        not anything in the product called it - and removing the call from
        `validate_stored_controls` did pass them. This is the one that fails if the check is
        computed and then dropped on the floor.
        """
        shared = {"heater_far": {"source": "hue", "device": "Conservatory heater far"}}
        for name in ("aaa", "bbb"):
            document = a_valid_control()
            document["name"] = name
            document["devices"] = dict(shared)
            document["output"]["stages"] = [
                {"level": 0, "set": {"heater_far": False}},
                {"level": 1500, "set": {"heater_far": True}},
            ]
            state_directory.write_control(document, name=name)
        with pytest.raises(ConfigError, match="both command"):
            validate_stored_controls(state_directory.settings_file)

    def test_check_config_accepts_them_when_one_is_disabled(self, state_directory):
        shared = {"heater_far": {"source": "hue", "device": "Conservatory heater far"}}
        for name, enabled in (("aaa", True), ("bbb", False)):
            document = a_valid_control()
            document["name"] = name
            document["enabled"] = enabled
            document["devices"] = dict(shared)
            document["output"]["stages"] = [
                {"level": 0, "set": {"heater_far": False}},
                {"level": 1500, "set": {"heater_far": True}},
            ]
            state_directory.write_control(document, name=name)
        validate_stored_controls(state_directory.settings_file)


class TestAnOmittedInstanceIsAmbiguousNotDistinct:
    """`None` means "the first configured target" (see `Hue.bridge`), so an entry omitting
    `instance` and one naming that bridge explicitly are the same actuator while comparing
    unequal. Resolving the default would need the settings document, which validation does
    not have - so the comparison treats an absent instance as possibly matching any."""

    def test_an_omitted_instance_clashes_with_an_explicit_one_in_one_document(self):
        document = a_valid_control()
        document["devices"] = {
            "a": {"source": "hue", "device": "heater_far"},
            "b": {"source": "hue", "device": "heater_far", "instance": "bridge1"},
        }
        document["output"]["stages"] = [{"level": 0, "set": {"a": False, "b": False}}]
        assert any("same actuator" in error for error in validate_control("conservatory", document))

    def test_it_clashes_across_documents_too(self):
        first = a_valid_control()
        second = dict(a_valid_control(), name="spare")
        second["devices"] = {key: dict(spec, instance="bridge1") for key, spec in second["devices"].items()}
        assert shared_actuator_problems({"conservatory": first, "spare": second})

    def test_two_different_explicit_instances_do_not_clash(self):
        """The refusal errs toward safety, but not so far that two real bridges collide."""
        document = a_valid_control()
        document["devices"] = {
            "a": {"source": "hue", "device": "heater_far", "instance": "bridge1"},
            "b": {"source": "hue", "device": "heater_far", "instance": "bridge2"},
        }
        document["output"]["stages"] = [{"level": 0, "set": {"a": False, "b": False}}]
        assert not [e for e in validate_control("conservatory", document) if "same actuator" in e]

    def test_the_comparison_is_symmetric(self):
        """Which of the pair is read first must not decide the answer."""
        omitted, explicit = ("hue", None, "far"), ("hue", "bridge1", "far")
        assert actuators_may_be_one(omitted, explicit) is actuators_may_be_one(explicit, omitted) is True


class TestATransitionMinimumLongerThanTheWindow:
    """Accepted, and kept by the transition log rather than by the planner.

    This was refused for most of 6.0's development, on the reasoning that the minimum binds
    inside a window and the planner keeps nothing from the last one - so a minimum longer
    than the cycle would be broken at every boundary and the document was incoherent. The
    stated objection to fixing it properly was that cross-window state "must then survive a
    restart, or the guarantee lapses at exactly the moment a control is restarted".

    That turned out to be an argument for where to put the state rather than against having
    it: it is on disk, in epoch seconds, and a restarted control reads it back. The two
    settings are unrelated - how often the loop recomputes, and how often a relay may be
    switched - and coupling them meant protecting one slow device slowed the whole loop.
    """

    def test_an_output_minimum_longer_than_the_cycle_is_accepted(self):
        document = a_valid_control()
        document["output"]["cycle_seconds"] = 300
        document["output"]["min_transition_seconds"] = 600
        assert validate_control("conservatory", document) == []

    def test_a_per_device_minimum_longer_than_the_cycle_is_accepted(self):
        """The likelier place to put a long one, since it is where a specific slow heater
        would be described - and the case the whole change exists for."""
        document = a_valid_control()
        document["output"]["cycle_seconds"] = 60
        key = sorted(document["devices"])[0]
        document["devices"][key]["min_transition_seconds"] = 900
        assert validate_control("conservatory", document) == []

    def test_it_is_still_refused_when_it_is_not_a_positive_number(self):
        """Relaxing the comparison against the cycle does not relax the shape: zero, a
        string and a negative are still not durations."""
        for bad in (0, -60, "600", float("nan")):
            document = a_valid_control()
            document["output"]["min_transition_seconds"] = bad
            assert any(
                "min_transition_seconds" in error for error in validate_control("conservatory", document)
            ), f"{bad!r} was accepted as a transition minimum"


class TestAnInputInstanceThatIsNotAName:
    """The same key, the same handler, and for a while only one of the two was checked.

    `inputs` and `devices` both take an optional `instance` and both hand it to
    `source_handler`, so the wrong shape fails the same way - one cycle later, while
    resolving the handler or quoting the value into a query. Checking devices and not inputs
    made one mistake report itself two different ways depending on where it was written.
    """

    @pytest.mark.parametrize("bad", [["bridge1"], {"host": "bridge1"}, 7, "", "   ", None])
    def test_it_is_refused(self, bad):
        document = a_valid_control()
        document["inputs"]["inside"]["instance"] = bad
        errors = validate_control("conservatory", document)
        assert any("inputs.inside.instance" in error for error in errors), f"{bad!r} was accepted"

    def test_a_real_name_is_accepted(self):
        document = a_valid_control()
        document["inputs"]["inside"]["instance"] = "bridge1"
        assert validate_control("conservatory", document) == []

    def test_an_omitted_one_is_still_how_you_mean_the_first_target(self):
        document = a_valid_control()
        document["inputs"]["inside"].pop("instance", None)
        assert validate_control("conservatory", document) == []

    def test_the_message_names_the_section_it_was_found_in(self):
        """`devices.inside.instance` for a fault in `inputs` would send somebody to the wrong
        half of their document."""
        document = a_valid_control()
        document["inputs"]["inside"]["instance"] = 7
        document["devices"]["heater_far"]["instance"] = 7
        errors = validate_control("conservatory", document)
        assert any("inputs.inside.instance" in error for error in errors)
        assert any("devices.heater_far.instance" in error for error in errors)


class TestADeviceInstanceThatIsNotAName:
    """`instance` is optional and was never shape-checked, so a list or a mapping validated
    cleanly and then reached `command_devices`, which groups by `(source, instance)` - and a
    tuple containing a list cannot be hashed, so the control died with a TypeError rather
    than the "cannot run" a configuration fault should produce."""

    @pytest.mark.parametrize(
        "instance",
        [
            pytest.param([], id="a-list"),
            pytest.param({}, id="a-mapping"),
            pytest.param(3, id="a-number"),
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace"),
        ],
    )
    def test_it_is_refused(self, instance):
        document = a_valid_control()
        key = sorted(document["devices"])[0]
        document["devices"][key]["instance"] = instance
        assert any("instance" in error for error in validate_control("conservatory", document))

    def test_an_unhashable_one_would_have_broken_the_grouping(self):
        """The failure this prevents, stated as the thing it actually is."""
        with pytest.raises(TypeError):
            dict.fromkeys([("hue", [])])

    def test_a_real_name_is_accepted(self):
        document = a_valid_control()
        key = sorted(document["devices"])[0]
        document["devices"][key]["instance"] = "bridge1"
        assert validate_control("conservatory", document) == []

    def test_omitting_it_is_still_fine(self):
        """It means the first configured target, which is a legitimate thing to mean."""
        assert validate_control("conservatory", a_valid_control()) == []


class TestAMisspeltKeyInANestedSection:
    """A key outside a section's schema is refused rather than ignored.

    The top-level validator already refused unknown keys; the nested sections did not, so
    `output.cycle_secconds` validated cleanly and then ran the default window, and
    `pid.kp_typo` validated cleanly and then ran a proportional gain of zero. In both cases
    the operator has a document in front of them saying one thing and a control doing
    another, with nothing anywhere reporting a fault.
    """

    @pytest.mark.parametrize(
        "place, mutate",
        [
            pytest.param("inputs", lambda d: d["inputs"]["inside"].update({"max_gae": 900}), id="inputs-entry"),
            pytest.param("devices", lambda d: d["devices"]["heater_far"].update({"min_transition": 60}), id="devices"),
            pytest.param("pid", lambda d: d["pid"].update({"kp_typo": 3.0}), id="pid"),
            pytest.param("output", lambda d: d["output"].update({"cycle_secconds": 60}), id="output"),
            pytest.param("stages[0]", lambda d: d["output"]["stages"][0].update({"levle": 0}), id="stage"),
            pytest.param("active_period", lambda d: d["active_period"].update({"form": "01:00"}), id="active-period"),
        ],
    )
    def test_it_is_refused_and_named(self, place, mutate):
        document = a_valid_control()
        mutate(document)
        errors = validate_control("conservatory", document)
        assert any("unknown key" in error for error in errors), f"{place} accepted a key it does not define"
        assert any(place.split("[")[0] in error for error in errors), "the message must say which section"

    def test_the_operators_own_names_are_still_theirs(self):
        """`parameters` keys and a stage's `set` keys are named by the operator, not by the
        schema, so the rule must not reach them. `set` has its own check against the device
        list, which is the one that should speak for a name that is wrong there."""
        document = a_valid_control()
        document["parameters"] = {"target": 19.0, "whatever_they_called_it": 2.0}
        assert not [error for error in validate_control("conservatory", document) if "unknown key" in error]

    def test_the_examples_the_store_ships_use_no_key_they_would_refuse(self):
        """The documented examples are what an MCP client copies. If the key sets and an
        example ever disagree, the thing following our own documentation is the one that
        gets the error."""
        for key, entry in CONTROL_EXAMPLES.items():
            document = entry["document"]
            assert validate_control(document["name"], document) == [], key


class TestASourceThisInstallationHasNotConfigured:
    """Knowing the class is not knowing the installation.

    `source_class` proves the build can collect from `hue`; it says nothing about whether
    this machine has a `hue` block. Without the settings check a control naming one passed
    --check-config, started, and died on its first safe-state command - then again on every
    restart the backoff allowed, reporting a device fault that was really a missing section.

    Inputs as well as devices, because an input is read through a source handler too: the
    database it is queried from is resolved out of that source's own settings block.
    """

    @staticmethod
    def _without(installation, section):
        """Return the settings with one source section removed.

        Args:
            installation (Installation): the installation to read
            section (str): the source section to drop

        Returns:
            dict: settings with that section absent
        """
        return {key: value for key, value in installation.settings.items() if key != section}

    def test_a_device_source_with_no_settings_section_is_refused(self, state_directory):
        errors = validate_control("conservatory", a_valid_control(), self._without(state_directory, "hue"))
        assert any("no configuration section found for source 'hue'" in error for error in errors)
        assert any("devices" in error for error in errors), "the message must say which entry"

    def test_an_input_source_with_no_settings_section_is_refused(self, state_directory):
        errors = validate_control("conservatory", a_valid_control(), self._without(state_directory, "openmeteo"))
        assert any("no configuration section found for source 'openmeteo'" in error for error in errors)

    def test_a_fully_configured_installation_is_accepted(self, state_directory):
        assert validate_control("conservatory", a_valid_control(), state_directory.settings) == []

    def test_without_settings_it_is_structure_only(self):
        """Callers that have no settings still get the other two thirds, rather than an
        error about a check they did not ask for."""
        assert validate_control("conservatory", a_valid_control()) == []

    def test_check_config_actually_applies_it(self, state_directory):
        """Through `validate_stored_controls` with settings, which is how --check-config
        calls it - the one that fails if the check is computed and then never reached."""
        state_directory.write_control(a_valid_control(), name="conservatory")
        settings = self._without(state_directory, "hue")
        with pytest.raises(ConfigError, match="no configuration section found for source 'hue'"):
            validate_stored_controls(state_directory.settings_file, settings)
        validate_stored_controls(state_directory.settings_file, state_directory.settings)

    def test_an_unknown_source_is_not_reported_twice(self, state_directory):
        """`source_block_problem` has its own "not a known source" branch. Reaching it here
        would print two messages for one fault in two different vocabularies, so it is only
        asked about sources the build already recognised."""
        document = a_valid_control()
        document["devices"]["heater_far"]["source"] = "nosuchsource"
        errors = [e for e in validate_control("conservatory", document, state_directory.settings) if "nosuch" in e]
        assert len(errors) == 1, f"one fault, {len(errors)} messages: {errors}"


class TestEveryShippedExample:
    """Each scenario document is handed to a client that will copy it, so each one is held
    to the same bar as the canonical example rather than only the one tests are built on.

    An example nothing exercises is a document that stops working the first time the format
    moves, and nobody finds out until somebody follows the documentation.
    """

    @pytest.mark.parametrize("scenario", sorted(CONTROL_EXAMPLES))
    def test_it_is_valid_against_a_real_installation(self, scenario, state_directory):
        document = CONTROL_EXAMPLES[scenario]["document"]
        assert validate_control(document["name"], document, state_directory.settings) == []

    @pytest.mark.parametrize("scenario", sorted(CONTROL_EXAMPLES))
    def test_it_does_not_trip_a_warning_of_our_own(self, scenario):
        """An example that the product complains about teaches the thing it complains about.

        Validation only asks whether a document is refused, and a warning is by definition
        not a refusal, so every example passed the checks above while all four told a reader
        to do what `--check-config` then told them off for.  It went unnoticed from the day
        the stale-feedback warning was added until somebody read the two side by side.
        """
        assert control_warnings(CONTROL_EXAMPLES[scenario]["document"]) == []

    @pytest.mark.parametrize("scenario", sorted(CONTROL_EXAMPLES))
    def test_it_says_when_to_use_it(self, scenario):
        """The reason there are three. Without it a model picks the first, or averages
        across them, which is how a 600-second minimum arrived between a 300 and a 900."""
        assert CONTROL_EXAMPLES[scenario]["use_when"].strip()

    @pytest.mark.parametrize("scenario", sorted(CONTROL_EXAMPLES))
    def test_it_can_actually_be_stored_and_read_back(self, scenario, state_directory):
        """Validation is not storage: a name the store refuses would make an example that
        validates and cannot be written under the name it carries."""
        document = CONTROL_EXAMPLES[scenario]["document"]
        save_control(document["name"], document, state_directory.settings_file)
        assert load_control(document["name"], state_directory.settings_file) == document

    def test_no_setting_carries_two_values_a_reader_could_average(self):
        """The defect that produced all this. Where an example sets `min_transition_seconds`
        at the output level and again on a device, the two must differ for a stated reason -
        which only the slow_response example has. Anywhere else, one value.
        """
        for scenario, entry in CONTROL_EXAMPLES.items():
            document = entry["document"]
            overrides = {
                name: spec["min_transition_seconds"]
                for name, spec in document["devices"].items()
                if "min_transition_seconds" in spec
            }
            if scenario != "slow_response":
                assert not overrides, f"{scenario} shows an override with nothing to justify it: {overrides}"


class TestANameDeclaredAsBothAnInputAndAParameter:
    """Two docstrings said the store already refused this, and it did not.

    `gather` builds the bindings as parameters and then overwrites them with inputs, so the
    constant is shadowed permanently by the live reading: every rule evaluates, nothing is
    logged, and adjusting the parameter at runtime changes nothing - which is the point at
    which somebody starts doubting the machinery rather than the document.
    """

    def test_it_is_refused(self):
        document = a_valid_control()
        document["parameters"]["inside"] = 5.0
        errors = validate_control("conservatory", document)
        assert any("both declare" in error for error in errors)

    def test_the_message_names_the_offending_name(self):
        document = a_valid_control()
        document["parameters"]["inside"] = 5.0
        assert any("'inside'" in error for error in validate_control("conservatory", document))

    def test_several_are_all_named_at_once(self):
        """One error per run, not one per exchange with whoever is fixing it."""
        document = a_valid_control()
        document["parameters"]["inside"] = 5.0
        document["parameters"]["dew"] = 5.0
        errors = [error for error in validate_control("conservatory", document) if "both declare" in error]
        assert len(errors) == 1
        assert "'dew'" in errors[0] and "'inside'" in errors[0]

    def test_distinct_names_are_fine(self):
        assert validate_control("conservatory", a_valid_control()) == []

    def test_a_section_of_the_wrong_shape_is_left_to_its_own_check(self):
        """A comparison against something that is not a mapping would invent a second
        complaint about a fault already reported precisely."""
        document = a_valid_control()
        document["parameters"] = "not a mapping"
        errors = [error for error in validate_control("conservatory", document) if "both declare" in error]
        assert errors == []


class TestADeviceDrivenByAParameter:
    """A device that names a parameter is set to a value rather than switched. Whether a
    *particular* lamp is dimmable is a question for the bridge, asked where the refusal can
    name the device; whether the source drives brightness at all is a fact about the code and
    is answered by `--check-config`."""

    @staticmethod
    def _driving(parameter, low=0, high=100):
        """Return a control whose first device is driven by `parameter`.

        Args:
            parameter (object): what to put in the device's parameter key
            low (object): its value on the bottom rung
            high (object): its value on the top rung

        Returns:
            tuple: (the document, the device's key)
        """
        document = a_valid_control()
        key = sorted(document["devices"])[0]
        document["devices"][key]["parameter"] = parameter
        rungs = sorted({stage["level"] for stage in document["output"]["stages"]})
        document["output"]["stages"] = [
            dict(stage, set={**stage["set"], key: low if stage["level"] == rungs[0] else high})
            for stage in document["output"]["stages"]
        ]
        return document, key

    def test_a_parameter_the_source_drives_is_accepted(self, state_directory):
        document, _key = self._driving("brightness_pct")
        assert validate_control("conservatory", document, state_directory.settings) == []

    def test_one_it_cannot_drive_is_refused_and_says_what_it_can(self, state_directory):
        document, key = self._driving("fan_speed")
        errors = validate_control("conservatory", document, state_directory.settings)
        assert any("cannot drive 'fan_speed'" in error for error in errors)
        assert any("brightness_pct" in error for error in errors), "it must say what is available"

    @pytest.mark.parametrize("bad", ["", "   ", 7, ["brightness_pct"]])
    def test_a_parameter_that_is_not_a_name_is_refused(self, bad, state_directory):
        document, _key = self._driving(bad)
        errors = validate_control("conservatory", document, state_directory.settings)
        assert any("parameter" in error for error in errors)

    def test_its_stage_entries_must_be_numbers(self, state_directory):
        document, key = self._driving("brightness_pct", low=False, high=True)
        errors = validate_control("conservatory", document, state_directory.settings)
        assert any("must be a number" in error and key in error for error in errors)

    def test_while_a_switched_device_must_still_be_true_or_false(self, state_directory):
        document, _key = self._driving("brightness_pct")
        other = sorted(set(document["devices"]) - {_key})[0]
        document["output"]["stages"][0]["set"][other] = 50
        errors = validate_control("conservatory", document, state_directory.settings)
        assert any("must be true or false" in error and other in error for error in errors)

    def test_a_negative_value_is_refused(self, state_directory):
        document, key = self._driving("brightness_pct", low=-10)
        errors = validate_control("conservatory", document, state_directory.settings)
        assert any("must be a number" in error and key in error for error in errors)

    def test_the_parameter_key_is_permitted_on_a_device(self):
        """It has to be in DEVICE_KEYS or the unknown-key check refuses it before anything
        else gets a look."""
        from toinflux.controls import DEVICE_KEYS

        assert "parameter" in DEVICE_KEYS

    def test_parameter_devices_reports_only_the_driven_ones(self):
        from toinflux.controls import parameter_devices

        devices = {
            "lamp": {"source": "hue", "device": "L", "parameter": "brightness_pct"},
            "heater": {"source": "hue", "device": "H"},
            "broken": {"source": "hue", "device": "B", "parameter": "  "},
        }
        assert parameter_devices(devices) == {"lamp": "brightness_pct"}


class TestTheExamplesGainsMatchTheirOwnLadders:
    """`level` is a scale the operator chooses, so a gain has to be in that scale per unit of
    input. The examples shipped kp=12 against a ladder topping out at 1500 - a 125-unit error
    to reach the top, and nothing delivered below four - which is not a tuning that suits a
    different plant, it is one that is wrong by three orders of magnitude.

    Copied numbers are the whole point of an example, so these are pinned: a future edit that
    changes a ladder without its gains fails here rather than on somebody's heating.
    """

    @staticmethod
    def _full_output_error(document):
        """Return the input error at which proportional action alone reaches the top rung.

        Args:
            document (dict): a control document

        Returns:
            float: the error, in the units of the control's input
        """
        top = max(stage["level"] for stage in document["output"]["stages"])
        return top / document["pid"]["kp"]

    @pytest.mark.parametrize("scenario", sorted(CONTROL_EXAMPLES))
    def test_full_output_arrives_within_a_plausible_error(self, scenario):
        """Wide, because what is plausible depends on the input - degrees for a heater, lux
        for a lamp. Narrow enough to catch a gain that is out by orders of magnitude, which
        is the failure this exists for."""
        document = CONTROL_EXAMPLES[scenario]["document"]
        error = self._full_output_error(document)
        assert 0.5 <= error <= 500, f"{scenario} reaches full output only at an error of {error:g}"

    @pytest.mark.parametrize("scenario", sorted(CONTROL_EXAMPLES))
    def test_a_small_error_actually_delivers_something(self, scenario):
        """The measure that matters is the level delivered over a window, not the top rung
        touched: with a short transition minimum a demand of 36 still buys a brief burst at a
        rung far above it, which looks like action and is not."""
        from toinflux.controller import Controller

        document = CONTROL_EXAMPLES[scenario]["document"]
        controller = Controller(document)
        error = self._full_output_error(document)
        bindings = {name: 0.0 for name in rule_names(document)}
        setpoint = controller._setpoint_rule.evaluate(bindings)
        bindings[document["pid"]["input"]] = setpoint - error / 2
        plan = controller.step(bindings, dt=document["output"]["cycle_seconds"])
        cycle = document["output"]["cycle_seconds"]
        top = max(stage["level"] for stage in document["output"]["stages"])
        if controller.driven:
            # A driven device's output is its value, not the rung: the window collapses onto
            # the lower rung and carries the value in the states, so measuring the level here
            # would read zero however bright the lamp was.
            name = next(iter(controller.driven))
            full = max(stage["set"][name] for stage in document["output"]["stages"])
            delivered = plan[0].stage.states[name] / full
        else:
            delivered = sum(dwell.stage.level * dwell.seconds for dwell in plan) / cycle / top
        assert delivered > 0.1, f"{scenario} delivered {delivered:.0%} of full output at half its full-output error"


class TestAControlThatActsFasterThanItCanSee:
    """A warning rather than an error: the control runs, and for a slow plant against a slow
    input this may be exactly what its operator meant.

    It earns its place because the symptom is misleading. A loop commanding several times on
    one reading overshoots and swings back, which looks like too much gain, so the usual
    response is to detune until it stops - buying stability by making the control slow rather
    than informed. Found on a real install, where a light loop cycling every 60s read a lux
    value up to 300s old and had already been detuned once to live with it.
    """

    @staticmethod
    def _document(max_age, cycle=60, feedback="lux"):
        """Return a control whose PID input has the given max_age.

        Args:
            max_age (float): the feedback input's max_age
            cycle (float): the cycle window
            feedback (str): which input the PID reads

        Returns:
            dict: the control document
        """
        return {
            "pid": {"input": feedback, "setpoint": "target", "kp": 0.05, "ki": 0.0007, "kd": 0},
            "parameters": {"target": 1000},
            "inputs": {
                "lux": {"source": "hue", "field": "L", "max_age": max_age},
                "outside": {"source": "openmeteo", "field": "temperature_2m", "max_age": 3600},
            },
            "output": {"cycle_seconds": cycle, "stages": [{"level": 0, "set": {"lamp": 0}}]},
            "devices": {"lamp": {"source": "hue", "device": "L"}},
        }

    @pytest.mark.parametrize("max_age", [120, 300, 1800])
    def test_it_warns_where_the_feedback_outlives_the_window(self, max_age):
        warnings = control_warnings(self._document(max_age), {})
        assert warnings and "inputs.lux" in warnings[0]
        assert "max_age" in warnings[0], "it must say which setting to change"

    @pytest.mark.parametrize("max_age", [60, 90, 30])
    def test_and_stays_quiet_where_it_does_not(self, max_age):
        """A few seconds over the window is a rounding difference, not a problem. A warning
        that fires on those is the noise that teaches people to skim warnings."""
        assert control_warnings(self._document(max_age), {}) == []

    def test_it_says_how_many_times_it_would_command_blind(self):
        """The number is the argument: "five times before seeing the first" is actionable in
        a way that "max_age is large" is not."""
        assert "5 times" in control_warnings(self._document(300), {})[0]

    def test_only_the_feedback_input_is_judged(self):
        """An outdoor temperature or a dew point is an observation, not feedback, and is
        legitimately hours old. Warning about those would be noise."""
        document = self._document(60, feedback="lux")
        assert document["inputs"]["outside"]["max_age"] == 3600
        assert control_warnings(document, {}) == []

    def test_a_document_with_no_pid_input_says_nothing(self):
        document = self._document(300)
        document["pid"].pop("input")
        assert control_warnings(document, {}) == []

    def test_an_unusable_max_age_is_left_to_validation(self):
        """Reported precisely there; a second complaint here would name one fault twice."""
        document = self._document("soon")
        assert control_warnings(document, {}) == []

    def test_validate_stored_controls_returns_it(self, state_directory):
        """Whether --check-config actually prints it is a separate question, asked in
        tests/test_sendtoinflux.py - a warning that is computed and never printed is the same
        as no warning, and naming this test after that would have hidden it."""
        document = a_valid_control()
        document["output"]["cycle_seconds"] = 60
        document["inputs"][document["pid"]["input"]]["max_age"] = 900
        state_directory.write_control(document, name="conservatory")
        notes = validate_stored_controls(state_directory.settings_file, state_directory.settings)
        assert notes and "conservatory" in notes[0]


class TestAnUnquotedClockTime:
    """YAML 1.1 reads a colon-separated value with no leading zero as sexagesimal, so an
    unquoted `23:35` is the integer 1415 while `05:25` survives as a string.

    Both are refused, and always were. What this adds is the reason: "got 1415" against a
    document that plainly says 23:35 is a message the reader can only decode by already
    knowing the trap, which is the opposite of what an error is for.
    """

    @pytest.mark.parametrize(
        "text, number, shown",
        [
            pytest.param("23:35", 1415, "23:35", id="the-shipped-example"),
            pytest.param("9:00", 540, "09:00", id="single-digit-hour"),
            pytest.param("17:00", 1020, "17:00", id="afternoon"),
        ],
    )
    def test_the_error_names_the_cause(self, text, number, shown):
        document = a_valid_control()
        document["active_period"] = yaml.safe_load(f"from: {text}\nto: '05:25'\nend_state: unenergised")
        assert document["active_period"]["from"] == number, "YAML did not mangle it; the premise is wrong"
        errors = [e for e in validate_control("conservatory", document) if "active_period.from" in e]
        assert errors and "sexagesimal" in errors[0]
        assert shown in errors[0], "it must show the time the author meant, not only the number"

    def test_a_leading_zero_survives_and_is_accepted(self):
        """Which is exactly why the shipped examples look inconsistently quoted."""
        assert yaml.safe_load("t: 05:25")["t"] == "05:25"
        document = a_valid_control()
        document["active_period"] = yaml.safe_load("from: '23:35'\nto: 05:25\nend_state: unenergised")
        assert [e for e in validate_control("conservatory", document) if "active_period" in e] == []

    def test_a_number_that_is_not_a_plausible_time_gets_no_hint(self):
        """1500 is 25:00, which nobody wrote by accident. Explaining sexagesimal there would
        be a guess dressed as a diagnosis."""
        document = a_valid_control()
        document["active_period"] = {"from": 1500, "to": "05:25", "end_state": "unenergised"}
        errors = [e for e in validate_control("conservatory", document) if "active_period.from" in e]
        assert errors and "sexagesimal" not in errors[0]

    def test_the_documented_examples_quote_every_boundary(self):
        """They are what gets copied, and copying the asymmetry is how somebody writes an
        unquoted 23:30 and gets a number."""
        reference = (pathlib.Path(__file__).resolve().parent.parent / "CONTROLS.md").read_text(encoding="utf-8")
        bare = re.findall(r"(?m)^\s*(?:from|to): (?!')(\d{1,2}:\d{2})\s*$", reference)
        assert not bare, f"unquoted clock times in the reference: {bare}"
