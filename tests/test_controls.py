"""Unit tests for toinflux.controls (the control store and its structural validation)."""

import copy
import os
import stat as stat_module
import pytest
import yaml
from toinflux.controls import (
    BUILT_IN_SAFE_STATES,
    control_dir,
    control_path,
    delete_control,
    list_controls,
    load_control,
    require_valid_control_name,
    save_control,
    rule_names,
    CONTROL_EXAMPLE,
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
