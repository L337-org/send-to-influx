"""Unit tests for toinflux.credentials (systemd-creds substitution) and the
load_settings()-integrated permission check / sentinel clearing in toinflux.general.
"""

import copy
import os
import stat
import tempfile
from pathlib import Path
import pytest
import yaml
from toinflux.credentials import CREDENTIAL_FIELDS, apply_credential_substitution, sentinel_for
from toinflux.general import load_settings
from toinflux.exceptions import ConfigError


class TestApplyCredentialSubstitution:
    """Tests for apply_credential_substitution."""

    def test_noop_when_credentials_directory_unset(self, sample_settings, monkeypatch):
        """No substitution happens when CREDENTIALS_DIRECTORY is unset."""
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        before = copy.deepcopy(sample_settings)
        result = apply_credential_substitution(sample_settings)
        assert result == before

    def test_noop_ignores_stray_matching_file_when_env_unset(self, sample_settings, monkeypatch, tmp_path):
        """Even if a file matching a credential name exists elsewhere, nothing happens
        without CREDENTIALS_DIRECTORY pointing at it."""
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        (tmp_path / "influx-token").write_text("should-not-be-used")
        before = copy.deepcopy(sample_settings)
        result = apply_credential_substitution(sample_settings)
        assert result == before

    def test_substitutes_each_known_credential(self, sample_settings, monkeypatch, tmp_path):
        """Every known credential name present as a file gets overlaid into settings."""
        for name in CREDENTIAL_FIELDS:
            (tmp_path / name).write_text(f"value-for-{name}\n")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        result = apply_credential_substitution(sample_settings)
        assert result["influx"]["token"] == "value-for-influx-token"
        assert result["influx"]["user"] == "value-for-influx-user"
        assert result["influx"]["password"] == "value-for-influx-password"
        assert result["hue"]["user"] == "value-for-hue-user"
        assert result["myenergi"]["apikey"] == "value-for-myenergi-apikey"
        assert result["octopus"]["api_key"] == "value-for-octopus-api-key"

    def test_strips_only_trailing_newlines_not_meaningful_whitespace(self, sample_settings, monkeypatch, tmp_path):
        """Only a trailing line ending is stripped - a password can legitimately
        start/end with spaces, and blindly stripping all whitespace would silently
        corrupt one instead of preserving it exactly as encrypted."""
        (tmp_path / "hue-user").write_text("  secret-value  \n\n")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        result = apply_credential_substitution(sample_settings)
        assert result["hue"]["user"] == "  secret-value  "

    def test_non_utf8_credential_file_does_not_crash(self, sample_settings, monkeypatch, tmp_path, caplog):
        """systemd credentials are arbitrary bytes with no guarantee of being
        valid UTF-8 - a bad one must be logged and skipped, not crash
        load_settings() with an unhandled UnicodeDecodeError."""
        (tmp_path / "hue-user").write_bytes(b"\xff\xfe not valid utf-8")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        original = sample_settings["hue"]["user"]
        with caplog.at_level("WARNING"):
            result = apply_credential_substitution(sample_settings)
        assert result["hue"]["user"] == original  # left alone, not substituted
        assert any("Could not read credential" in r.message for r in caplog.records)

    def test_ignores_unrelated_files_in_directory(self, sample_settings, monkeypatch, tmp_path):
        """Extra, unrelated files in CREDENTIALS_DIRECTORY don't cause errors or changes."""
        (tmp_path / "some-other-credential").write_text("irrelevant")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        before = copy.deepcopy(sample_settings)
        result = apply_credential_substitution(sample_settings)
        assert result == before

    def test_leaves_field_alone_when_file_absent(self, sample_settings, monkeypatch, tmp_path):
        """A credential name with no corresponding file leaves that field untouched."""
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        result = apply_credential_substitution(sample_settings)
        assert result["influx"].get("token") is None

    def test_creates_missing_top_level_block(self, sample_settings, monkeypatch, tmp_path):
        """A credential for a block absent from settings.yaml creates that block."""
        del sample_settings["hue"]
        (tmp_path / "hue-user").write_text("secret-value")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        result = apply_credential_substitution(sample_settings)
        assert result["hue"]["user"] == "secret-value"

    def test_non_mapping_top_level_key_does_not_crash(self, sample_settings, monkeypatch, tmp_path, caplog):
        """A malformed settings.yaml (e.g. `hue: "oops"` instead of a mapping) must
        not crash substitution with a TypeError - that would happen before
        validate_settings() gets a chance to report it as a proper ConfigError."""
        sample_settings["hue"] = "oops"
        (tmp_path / "hue-user").write_text("secret-value")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        with caplog.at_level("WARNING"):
            result = apply_credential_substitution(sample_settings)
        assert result["hue"] == "oops"  # left alone for validate_settings() to catch
        assert any("is not a mapping" in r.message for r in caplog.records)

    def test_non_mapping_list_top_level_key_does_not_crash(self, sample_settings, monkeypatch, tmp_path):
        sample_settings["influx"] = []
        (tmp_path / "influx-token").write_text("secret-value")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        result = apply_credential_substitution(sample_settings)  # does not raise
        assert result["influx"] == []


class TestSettingsFilePermissionCheck:
    """Integration tests for _enforce_settings_file_permissions via load_settings()."""

    def _write_settings(self, settings, mode):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(settings, f)
            path = f.name
        os.chmod(path, mode)
        return path

    def test_0600_with_real_secret_loads_clean(self, sample_settings, caplog):
        """A 0600 file with a real secret loads with no warning, regardless of enforce_permissions."""
        sample_settings["enforce_permissions"] = True
        path = self._write_settings(sample_settings, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with caplog.at_level("WARNING"):
                result = load_settings(settings_file=path)
            assert result["influx"]["user"] == "influx_user"
            assert not any("readable by group/other" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_0644_with_only_placeholders_loads_clean(self, sample_settings, caplog):
        """A 0644 file containing only placeholder/sentinel values doesn't warn - this
        is what makes 644 safe as the fresh-install default mode."""
        sample_settings["hue"]["user"] = "your_hue_user"
        sample_settings["influx"]["user"] = "your_influx_user"
        sample_settings["influx"]["password"] = "your_influx_password"
        sample_settings["myenergi"]["apikey"] = "your_api_key"
        path = self._write_settings(sample_settings, 0o644)
        try:
            with caplog.at_level("WARNING"):
                load_settings(settings_file=path)
            assert not any("readable by group/other" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_0644_with_sentinel_values_does_not_warn_on_permissions(self, sample_settings, caplog):
        """A 0644 file whose credential fields hold systemd-creds sentinels doesn't
        trigger the permission warning - the sentinel-clearing step (tested
        separately below) then correctly fails validation afterward since, with
        CREDENTIALS_DIRECTORY unset in this test, nothing actually substitutes real
        values back in; that's a validation concern, not a permissions one, and this
        test only asserts the permissions side stayed quiet."""
        sample_settings["hue"]["user"] = sentinel_for("hue-user")
        sample_settings["influx"]["user"] = sentinel_for("influx-user")
        sample_settings["influx"]["password"] = sentinel_for("influx-password")
        sample_settings["myenergi"]["apikey"] = sentinel_for("myenergi-apikey")
        path = self._write_settings(sample_settings, 0o644)
        try:
            with caplog.at_level("WARNING"):
                with pytest.raises(ConfigError):
                    load_settings(settings_file=path)
            assert not any("readable by group/other" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_0644_with_real_secret_warns_but_loads(self, sample_settings, caplog):
        """A 0644 file with a real-looking secret and enforce_permissions unset (or
        false) logs a warning but still loads."""
        path = self._write_settings(sample_settings, 0o644)
        try:
            with caplog.at_level("WARNING"):
                result = load_settings(settings_file=path)
            assert result["influx"]["user"] == "influx_user"
            assert any("readable by group/other" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_0644_with_a_different_fields_placeholder_still_warns(self, sample_settings, caplog):
        """A real secret that happens to equal a *different* credential field's
        placeholder text (e.g. influx.user == myenergi.apikey's placeholder) must
        still be treated as a real secret - _contains_real_secret() must compare
        against each field's own placeholder, not the placeholder set as a whole.
        Every *other* credential field is set to its own genuine placeholder, so
        only the one deliberately-mismatched field under test can trigger this."""
        sample_settings["hue"]["user"] = "your_hue_user"
        sample_settings["influx"]["password"] = "your_influx_password"
        sample_settings["myenergi"]["apikey"] = "your_api_key"
        sample_settings["influx"]["user"] = "your_api_key"  # myenergi-apikey's placeholder, not influx-user's
        path = self._write_settings(sample_settings, 0o644)
        try:
            with caplog.at_level("WARNING"):
                load_settings(settings_file=path)
            assert any("readable by group/other" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_0644_with_falsy_but_real_value_still_warns(self, sample_settings, caplog):
        """A credential field holding a falsy-but-real value (e.g. an unquoted `0`
        in YAML, parsed as an int) must still be treated as a real secret - `not
        value` would incorrectly treat it the same as a genuinely absent value.
        Every *other* credential field is set to its own genuine placeholder, so
        only the deliberately-falsy field under test can trigger this."""
        sample_settings["influx"]["user"] = "your_influx_user"
        sample_settings["influx"]["password"] = "your_influx_password"
        sample_settings["myenergi"]["apikey"] = "your_api_key"
        sample_settings["hue"]["user"] = 0
        path = self._write_settings(sample_settings, 0o644)
        try:
            with caplog.at_level("WARNING"):
                load_settings(settings_file=path)
            assert any("readable by group/other" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_0644_with_real_secret_and_enforce_true_raises(self, sample_settings, caplog):
        """A 0644 file with a real secret and enforce_permissions: true refuses to load."""
        sample_settings["enforce_permissions"] = True
        path = self._write_settings(sample_settings, 0o644)
        try:
            with caplog.at_level("WARNING"):
                with pytest.raises(ConfigError):
                    load_settings(settings_file=path)
            assert any("readable by group/other" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_0644_with_real_secret_and_quoted_false_string_does_not_raise(self, sample_settings, caplog):
        """A mistakenly-quoted enforce_permissions: "false" (a truthy Python string,
        despite reading as "false") must not be treated as enforcement being enabled -
        only the real YAML boolean True should trigger a refusal to start."""
        sample_settings["enforce_permissions"] = "false"
        path = self._write_settings(sample_settings, 0o644)
        try:
            with caplog.at_level("WARNING"):
                result = load_settings(settings_file=path)
            assert result["influx"]["user"] == "influx_user"
            assert any("readable by group/other" in r.message for r in caplog.records)
        finally:
            Path(path).unlink(missing_ok=True)


class TestClearUnsubstitutedCredentialSentinels:
    """Integration tests via load_settings(): a sentinel left in place without a
    matching systemd credential file must not pass validation as if it were real."""

    def test_sentinel_without_substitution_fails_validation(self, sample_settings, monkeypatch):
        """A sentinel-valued required field, with CREDENTIALS_DIRECTORY unset (so it's
        never replaced), is blanked before validate_settings() runs and so is
        correctly reported as missing rather than accepted as real."""
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        sample_settings["influx"]["user"] = sentinel_for("influx-user")
        sample_settings["influx"]["password"] = sentinel_for("influx-password")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(sample_settings, f)
            path = f.name
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with pytest.raises(ConfigError):
                load_settings(settings_file=path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_influx_token_sentinel_raises_specific_error_not_misclassified_as_v1(self, sample_settings, monkeypatch):
        """influx.token specifically must not be silently blanked like the other
        sentinel fields - doing so would flip validate_settings()'s
        is_v2 = bool(token) check to False, misclassifying a broken v2 config as
        v1 and producing a confusing "<source>.db is required for v1" error for a
        source that only defines bucket (valid v2 shorthand) and was never using
        v1 authentication at all."""
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        sample_settings["influx"] = {
            "url": "https://influx.example.com:8086",
            "org": "myorg",
            "token": sentinel_for("influx-token"),
        }
        sample_settings["sources"] = ["zappi"]
        sample_settings["default_source"] = "zappi"
        sample_settings["zappi"] = {"interval": 300, "bucket": "zappi_bucket", "serial": "12345"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(sample_settings, f)
            path = f.name
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with pytest.raises(ConfigError, match="migrated to systemd-creds but could not be loaded"):
                load_settings(settings_file=path)
        finally:
            Path(path).unlink(missing_ok=True)
