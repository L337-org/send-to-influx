"""Unit tests for toinflux.general (load_settings, get_class)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
import yaml
from toinflux.general import flatten_dict, load_settings, get_class, validate_settings
from toinflux.exceptions import ConfigError


class TestLoadSettings:
    """Tests for load_settings function."""

    def test_returns_parsed_yaml(self, sample_settings):
        """load_settings returns the parsed YAML dictionary."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(sample_settings, f)
            path = f.name
        try:
            result = load_settings(settings_file=path)
            assert result == sample_settings
        finally:
            Path(path).unlink(missing_ok=True)

    def test_returns_correct_values(self, sample_settings):
        """load_settings returns dictionary with expected keys and values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(sample_settings, f)
            path = f.name
        try:
            result = load_settings(settings_file=path)
            assert result["default_source"] == "hue"
        finally:
            Path(path).unlink(missing_ok=True)

    def test_file_not_found_raises_config_error(self):
        """load_settings raises ConfigError when file is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.yml")
            with pytest.raises(ConfigError):
                load_settings(settings_file=missing)

    def test_invalid_yaml_raises_config_error(self):
        """load_settings raises ConfigError on YAML error."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("invalid: yaml: [[[")
            path = f.name
        try:
            with pytest.raises(ConfigError):
                load_settings(settings_file=path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_empty_yaml_raises_config_error(self):
        """load_settings raises ConfigError on empty YAML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("")
            path = f.name
        try:
            with pytest.raises(ConfigError):
                load_settings(settings_file=path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_loads_from_yaml_extension(self, sample_settings):
        """load_settings reads a file with the .yaml extension."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(sample_settings, f)
            path = f.name
        try:
            result = load_settings(settings_file=path)
            assert result == sample_settings
        finally:
            Path(path).unlink(missing_ok=True)

    def test_falls_back_to_yml_when_yaml_missing(self, sample_settings):
        """load_settings falls back to .yml when the .yaml file does not exist."""
        with tempfile.TemporaryDirectory() as tmp:
            yml_path = os.path.join(tmp, "settings.yml")
            yaml_path = os.path.join(tmp, "settings.yaml")
            with open(yml_path, "w", encoding="utf8") as f:
                yaml.dump(sample_settings, f)
            result = load_settings(settings_file=yaml_path)
            assert result == sample_settings

    def test_no_fallback_when_yaml_exists(self, sample_settings):
        """load_settings uses .yaml and does not fall back when .yaml exists."""
        import copy

        yaml_settings = copy.deepcopy(sample_settings)
        yaml_settings["default_source"] = "from_yaml"
        with tempfile.TemporaryDirectory() as tmp:
            yml_path = os.path.join(tmp, "settings.yml")
            yaml_path = os.path.join(tmp, "settings.yaml")
            with open(yml_path, "w", encoding="utf8") as f:
                yaml.dump(sample_settings, f)
            with open(yaml_path, "w", encoding="utf8") as f:
                yaml.dump(yaml_settings, f)
            result = load_settings(settings_file=yaml_path)
            assert result["default_source"] == "from_yaml"

    def test_validation_error_log_uses_the_actual_resolved_path(self, sample_settings, caplog):
        """load_settings labels validation error logs with the real resolved path, not 'settings.yaml'."""
        del sample_settings["influx"]["url"]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(sample_settings, f)
            path = f.name
        try:
            with caplog.at_level("CRITICAL"):
                with pytest.raises(ConfigError):
                    load_settings(settings_file=path)
            assert any(f"{path}: influx.url is required" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_defaults_to_settings_yaml_when_omitted(self):
        """load_settings() with no argument resolves to settings.yaml in the project root."""
        with patch("toinflux.general.open", side_effect=FileNotFoundError):
            with pytest.raises(ConfigError):
                load_settings()


class TestValidateSettings:
    """Tests for validate_settings function."""

    def test_valid_v1_settings_passes(self, sample_settings):
        """validate_settings does not exit for valid v1 settings."""
        validate_settings(sample_settings)

    def test_valid_v2_settings_passes(self, sample_settings):
        """validate_settings does not exit for valid v2 settings."""
        sample_settings["influx"] = {"url": "http://influx.example.com:8086", "token": "tok", "org": "myorg"}
        sample_settings["hue"]["bucket"] = "hue_bucket"
        del sample_settings["hue"]["db"]
        validate_settings(sample_settings)

    def test_missing_influx_url_raises_config_error(self, sample_settings):
        """validate_settings raises ConfigError when influx.url is missing."""
        del sample_settings["influx"]["url"]
        with pytest.raises(ConfigError):
            validate_settings(sample_settings)

    def test_missing_influx_credentials_raises_config_error(self, sample_settings):
        """validate_settings raises ConfigError when neither v1 nor v2 credentials are present."""
        del sample_settings["influx"]["user"]
        del sample_settings["influx"]["password"]
        with pytest.raises(ConfigError):
            validate_settings(sample_settings)

    def test_v2_token_without_org_raises_config_error(self, sample_settings):
        """validate_settings raises ConfigError when token is present but org is missing."""
        sample_settings["influx"]["token"] = "mytoken"
        with pytest.raises(ConfigError):
            validate_settings(sample_settings)

    def test_duplicate_sources_raise_config_error(self, sample_settings):
        """validate_settings raises ConfigError when the sources list repeats an entry -
        two workers for one source name would share (and race on) one write buffer."""
        sample_settings["sources"] = ["hue", "hue", "zappi"]
        with pytest.raises(ConfigError, match="duplicate"):
            validate_settings(sample_settings)

    def test_missing_source_section_raises_config_error(self, sample_settings):
        """validate_settings raises ConfigError when a configured source has no settings section."""
        sample_settings["sources"] = ["hue", "nosuchsource"]
        with pytest.raises(ConfigError):
            validate_settings(sample_settings)

    def test_missing_interval_raises_config_error(self, sample_settings):
        """validate_settings raises ConfigError when a source is missing interval."""
        del sample_settings["hue"]["interval"]
        with pytest.raises(ConfigError):
            validate_settings(sample_settings)

    def test_missing_db_and_bucket_raises_config_error(self, sample_settings):
        """validate_settings raises ConfigError when a source has neither db nor bucket."""
        del sample_settings["hue"]["db"]
        with pytest.raises(ConfigError):
            validate_settings(sample_settings)

    def test_empty_token_falls_back_to_v1_validation(self, sample_settings):
        """validate_settings treats an empty token as absent and validates v1 user/password instead."""
        sample_settings["influx"]["token"] = ""
        validate_settings(sample_settings)

    def test_bucket_accepted_in_place_of_db_for_v2(self, sample_settings):
        """validate_settings accepts bucket as an alternative to db, for v2 (token) auth."""
        sample_settings["influx"] = {"url": "http://influx.example.com:8086", "token": "tok", "org": "myorg"}
        del sample_settings["hue"]["db"]
        sample_settings["hue"]["bucket"] = "hue_bucket"
        validate_settings(sample_settings)

    def test_bucket_without_db_raises_config_error_for_v1(self, sample_settings):
        """validate_settings rejects bucket-only source config under v1 (user/password) auth.

        v1's send_data() reads source_settings["db"] directly with no bucket fallback
        (unlike v2, which falls back from bucket to db) - a source configured with only
        bucket under v1 auth would otherwise pass validation and then KeyError at runtime.
        """
        del sample_settings["hue"]["db"]
        sample_settings["hue"]["bucket"] = "hue_bucket"
        with pytest.raises(ConfigError):
            validate_settings(sample_settings)

    def test_explicit_source_validated_even_if_not_in_sources_list(self, sample_settings):
        """validate_settings(source=...) also validates a source outside sources/default_source.

        Without the source= kwarg, a broken block for a source that isn't part of
        sources/default_source is never checked - passing it explicitly (as --check-config
        --source <x> now does) is what surfaces it.
        """
        sample_settings["octopus"] = {"db": "octopus_db"}  # missing interval; not in sources/default_source
        validate_settings(sample_settings)  # passes: octopus isn't checked without source=
        with pytest.raises(ConfigError):
            validate_settings(sample_settings, source="octopus")

    def test_explicit_source_not_double_reported_if_already_in_sources_list(self, sample_settings):
        """validate_settings(source=...) doesn't duplicate a source already covered by sources/default_source."""
        # default_source is "hue" per sample_settings; passing it explicitly shouldn't
        # cause it to be validated (and thus reported) twice.
        validate_settings(sample_settings, source="hue")

    def test_explicit_source_validated_case_insensitively(self, sample_settings):
        """validate_settings(source=...) matches the runtime path's case-insensitivity:
        --check-config --source Hue must not fail while --source Hue runs fine."""
        validate_settings(sample_settings, source="Hue")

    def test_duplicate_sources_detected_across_case_variants(self, sample_settings):
        """['Hue', 'hue'] is the same source twice - the duplicate check must see it."""
        sample_settings["sources"] = ["Hue", "hue"]
        with pytest.raises(ConfigError, match="duplicate"):
            validate_settings(sample_settings)

    def test_mqtt_source_requires_mqtt_block(self, sample_settings):
        """An enabled MQTT-based source without the shared mqtt block fails --check-config
        up front, instead of reporting OK and letting the collector ConfigError at runtime."""
        sample_settings["nuki"] = {"db": "nuki_db", "interval": 300}
        with pytest.raises(ConfigError, match="mqtt.broker_host"):
            validate_settings(sample_settings, source="nuki")

    def test_mqtt_source_passes_with_broker_host_configured(self, sample_settings):
        """The same config validates once mqtt.broker_host is present."""
        sample_settings["nuki"] = {"db": "nuki_db", "interval": 300}
        sample_settings["mqtt"] = {"broker_host": "mqtt.example.com"}
        validate_settings(sample_settings, source="nuki")

    def test_mqtt_block_not_required_without_mqtt_sources(self, sample_settings):
        """Installs with no MQTT-based source configured don't need an mqtt block at all."""
        validate_settings(sample_settings)  # sample_settings has no nuki and no mqtt block

    def test_error_log_uses_given_settings_path_not_hardcoded_settings_yaml(self, sample_settings, caplog):
        """validate_settings labels log messages with settings_path, not a hard-coded 'settings.yaml'.

        Settings can come from a location other than settings.yaml (--settings, or the .yml
        fallback), so the log output shouldn't claim it's always settings.yaml.
        """
        del sample_settings["influx"]["url"]
        with caplog.at_level("CRITICAL"):
            with pytest.raises(ConfigError):
                validate_settings(sample_settings, settings_path="/etc/send-to-influx/settings.yaml")
        assert any("/etc/send-to-influx/settings.yaml: influx.url is required" in r.message for r in caplog.records)
        assert not any("settings.yaml: influx.url is required" == r.message for r in caplog.records)


class TestGetClass:
    """Tests for get_class function."""

    def test_get_class_returns_hue_for_lowercase(self, sample_settings):
        """get_class('hue') returns Hue instance with source 'hue'."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.philipshue.Hue") as mock_hue:
                result = get_class("hue")
                mock_hue.assert_called_once_with("hue", settings_file=None)
                assert result is mock_hue.return_value

    def test_get_class_returns_hue_for_uppercase(self, sample_settings):
        """get_class('Hue') uses capitalised class name and lower source."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.philipshue.Hue") as mock_hue:
                result = get_class("Hue")
                mock_hue.assert_called_once_with("hue", settings_file=None)
                assert result is mock_hue.return_value

    def test_get_class_returns_zappi_for_lowercase(self, sample_settings):
        """get_class('zappi') returns Zappi instance."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.myenergi.Zappi") as mock_zappi:
                result = get_class("zappi")
                mock_zappi.assert_called_once_with("zappi", settings_file=None)
                assert result is mock_zappi.return_value

    def test_get_class_returns_speedtest_for_lowercase(self, sample_settings):
        """get_class('speedtest') returns Speedtest instance."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.speedtest.Speedtest") as mock_speedtest:
                result = get_class("speedtest")
                mock_speedtest.assert_called_once_with("speedtest", settings_file=None)
                assert result is mock_speedtest.return_value

    def test_get_class_returns_speedtest_for_uppercase(self, sample_settings):
        """get_class('Speedtest') uses capitalised class name and lower source."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.speedtest.Speedtest") as mock_speedtest:
                result = get_class("Speedtest")
                mock_speedtest.assert_called_once_with("speedtest", settings_file=None)
                assert result is mock_speedtest.return_value

    def test_get_class_unknown_source_raises_config_error(self):
        """get_class with unknown source raises ConfigError."""
        with pytest.raises(ConfigError):
            get_class("nosuchsource")

    def test_get_class_threads_settings_file_through(self, sample_settings):
        """get_class passes an explicit settings_file through to the handler constructor."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.philipshue.Hue") as mock_hue:
                get_class("hue", settings_file="/etc/send-to-influx/settings.yaml")
                mock_hue.assert_called_once_with("hue", settings_file="/etc/send-to-influx/settings.yaml")

    def test_get_class_datahandler_is_not_selectable(self):
        """get_class('DataHandler') raises ConfigError - it's the abstract base, not a real source."""
        with pytest.raises(ConfigError):
            get_class("DataHandler")


class TestFlattenDict:
    """Tests for flatten_dict function."""

    def test_flat_dict_is_unchanged(self):
        """flatten_dict keeps flat dictionaries unchanged."""
        data = {"a": 1, "b": 2}
        assert flatten_dict(data) == {"a": 1, "b": 2}

    def test_nested_dict_is_flattened_with_default_separator(self):
        """flatten_dict flattens nested dictionaries using the default separator."""
        data = {"a": {"b": {"c": 1}}, "d": 2}
        assert flatten_dict(data) == {"a_b_c": 1, "d": 2}

    def test_nested_dict_with_custom_separator(self):
        """flatten_dict supports a custom key separator."""
        data = {"a": {"b": 1}}
        assert flatten_dict(data, sep=".") == {"a.b": 1}

    def test_mixed_values_are_preserved(self):
        """flatten_dict preserves non-dict values in nested structures."""
        data = {"a": {"b": [1, 2]}, "c": None, "d": {"e": True}}
        assert flatten_dict(data) == {"a_b": [1, 2], "c": None, "d_e": True}

    def test_empty_dict_returns_empty_dict(self):
        """flatten_dict returns empty dict for empty input."""
        assert not flatten_dict({})

    def test_invalid_mqtt_broker_port_raises_config_error(self, sample_settings):
        """A non-integer or out-of-range broker_port fails --check-config up front."""
        sample_settings["nuki"] = {"db": "nuki_db", "interval": 300}
        sample_settings["mqtt"] = {"broker_host": "mqtt.example.com", "broker_port": "1883"}
        with pytest.raises(ConfigError, match="broker_port"):
            validate_settings(sample_settings, source="nuki")
        sample_settings["mqtt"]["broker_port"] = 70000
        with pytest.raises(ConfigError, match="broker_port"):
            validate_settings(sample_settings, source="nuki")

    def test_non_string_sources_entry_raises_config_error_not_typeerror(self, sample_settings):
        """A malformed sources list (e.g. a YAML mapping entry) reports a clear
        ConfigError instead of raising a raw TypeError from membership tests."""
        sample_settings["sources"] = ["hue", {"oops": "mapping"}]
        with pytest.raises(ConfigError, match="must be strings"):
            validate_settings(sample_settings)
