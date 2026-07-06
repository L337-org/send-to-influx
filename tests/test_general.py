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

    def test_bucket_accepted_in_place_of_db(self, sample_settings):
        """validate_settings accepts bucket as an alternative to db."""
        del sample_settings["hue"]["db"]
        sample_settings["hue"]["bucket"] = "hue_bucket"
        validate_settings(sample_settings)


class TestGetClass:
    """Tests for get_class function."""

    def test_get_class_returns_hue_for_lowercase(self, sample_settings):
        """get_class('hue') returns Hue instance with source 'hue'."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.philipshue.Hue") as mock_hue:
                result = get_class("hue")
                mock_hue.assert_called_once_with("hue")
                assert result is mock_hue.return_value

    def test_get_class_returns_hue_for_uppercase(self, sample_settings):
        """get_class('Hue') uses capitalised class name and lower source."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.philipshue.Hue") as mock_hue:
                result = get_class("Hue")
                mock_hue.assert_called_once_with("hue")
                assert result is mock_hue.return_value

    def test_get_class_returns_zappi_for_lowercase(self, sample_settings):
        """get_class('zappi') returns Zappi instance."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.myenergi.Zappi") as mock_zappi:
                result = get_class("zappi")
                mock_zappi.assert_called_once_with("zappi")
                assert result is mock_zappi.return_value

    def test_get_class_returns_speedtest_for_lowercase(self, sample_settings):
        """get_class('speedtest') returns Speedtest instance."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.speedtest.Speedtest") as mock_speedtest:
                result = get_class("speedtest")
                mock_speedtest.assert_called_once_with("speedtest")
                assert result is mock_speedtest.return_value

    def test_get_class_returns_speedtest_for_uppercase(self, sample_settings):
        """get_class('Speedtest') uses capitalised class name and lower source."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch("toinflux.speedtest.Speedtest") as mock_speedtest:
                result = get_class("Speedtest")
                mock_speedtest.assert_called_once_with("speedtest")
                assert result is mock_speedtest.return_value

    def test_get_class_unknown_source_raises_config_error(self):
        """get_class with unknown source raises ConfigError."""
        with pytest.raises(ConfigError):
            get_class("nosuchsource")


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
