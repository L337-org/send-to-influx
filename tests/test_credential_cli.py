"""Unit tests for toinflux.credential_cli (send-to-influx-set-credential)."""

from unittest.mock import MagicMock, patch
import pytest
import yaml
from toinflux import credential_cli
from toinflux.credential_cli import (
    CredentialCliError,
    _cmd_ensure_influx_storage,
    _cmd_remove,
    _cmd_set,
    _cmd_set_field,
    _detect_influx_version,
    _parse_systemd_creds_version,
    _regenerate_dropin,
    _require_systemd_creds,
    _rewrite_settings_field,
    _validate_secret_value,
    main,
)

# --------------------------------------------------------------------------- #
# systemd-creds version check
# --------------------------------------------------------------------------- #


class TestParseSystemdCredsVersion:
    def test_parses_leading_version_number(self):
        assert _parse_systemd_creds_version("systemd 255 (255.4-1ubuntu8.4)\n+PAM +AUDIT\n") == 255

    def test_returns_none_for_unrecognised_output(self):
        assert _parse_systemd_creds_version("garbage") is None


class TestRequireSystemdCreds:
    def test_raises_specific_message_when_binary_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(CredentialCliError, match="systemd-creds not found"):
                _require_systemd_creds()

    def test_raises_specific_message_when_version_too_old(self):
        result = MagicMock(stdout="systemd 249 (249.11-0ubuntu3.20)\n")
        with patch("subprocess.run", return_value=result):
            with pytest.raises(CredentialCliError, match="requires systemd >= 250"):
                _require_systemd_creds()

    def test_passes_when_version_new_enough(self):
        result = MagicMock(stdout="systemd 257 (257.13-1~deb13u1)\n")
        with patch("subprocess.run", return_value=result):
            _require_systemd_creds()  # does not raise


# --------------------------------------------------------------------------- #
# Secret validation
# --------------------------------------------------------------------------- #


class TestValidateSecretValue:
    def test_rejects_empty(self):
        with pytest.raises(CredentialCliError):
            _validate_secret_value("influx-token", "")

    def test_rejects_whitespace_only(self):
        with pytest.raises(CredentialCliError):
            _validate_secret_value("influx-token", "   ")

    def test_rejects_placeholder(self):
        with pytest.raises(CredentialCliError):
            _validate_secret_value("influx-token", "your_influx_token")

    def test_rejects_embedded_newline(self):
        with pytest.raises(CredentialCliError):
            _validate_secret_value("influx-token", "line1\nline2")

    def test_accepts_real_value(self):
        _validate_secret_value("influx-token", "a-real-token-value")  # does not raise


# --------------------------------------------------------------------------- #
# _rewrite_settings_field
# --------------------------------------------------------------------------- #


class TestRewriteSettingsField:
    def _write(self, tmp_path, content):
        path = tmp_path / "settings.yaml"
        path.write_text(content)
        return str(path)

    def test_replaces_value_preserving_comments(self, tmp_path):
        content = "# a comment\n" "influx:\n" "  # the token\n" '  token: "old_value"\n' '  org: "myorg"\n'
        path = self._write(tmp_path, content)
        _rewrite_settings_field(path, "influx", "token", "new_value")
        result = open(path, encoding="utf8").read()
        assert "# the token" in result
        assert "# a comment" in result
        assert 'token: "new_value"' in result
        assert 'org: "myorg"' in result

    def test_writes_only_the_target_line(self, tmp_path):
        content = 'influx:\n  token: "old_value"\n  org: "myorg"\nhue:\n  user: "someone"\n'
        path = self._write(tmp_path, content)
        _rewrite_settings_field(path, "influx", "token", "new_value")
        result = open(path, encoding="utf8").read()
        expected = content.replace('token: "old_value"', 'token: "new_value"')
        assert result == expected

    def test_raises_when_section_missing(self, tmp_path):
        content = 'hue:\n  user: "someone"\n'
        path = self._write(tmp_path, content)
        with pytest.raises(CredentialCliError, match="no 'influx:' section"):
            _rewrite_settings_field(path, "influx", "token", "new_value")

    def test_raises_on_block_scalar_value(self, tmp_path):
        content = "influx:\n  token: |\n    multi\n    line\n"
        path = self._write(tmp_path, content)
        with pytest.raises(CredentialCliError, match="could not safely rewrite"):
            _rewrite_settings_field(path, "influx", "token", "new_value")

    def test_preserves_file_permissions(self, tmp_path):
        import os
        import stat

        content = 'influx:\n  token: "old_value"\n'
        path = self._write(tmp_path, content)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        _rewrite_settings_field(path, "influx", "token", "new_value")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == (stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------------------- #
# _regenerate_dropin
# --------------------------------------------------------------------------- #


class TestRegenerateDropin:
    def test_includes_only_existing_cred_files(self, tmp_path):
        credstore = tmp_path / "credstore"
        credstore.mkdir()
        (credstore / "influx-token.cred").write_text("ciphertext")
        dropin = tmp_path / "dropin.conf"
        _regenerate_dropin(credstore_dir=str(credstore), dropin_path=str(dropin))
        content = dropin.read_text()
        assert "LoadCredentialEncrypted=influx-token:" in content
        assert "hue-user" not in content

    def test_excludes_name_even_if_file_still_present(self, tmp_path):
        """Simulates the _cmd_remove ordering: the drop-in must exclude a name even
        while its .cred file still exists on disk, since it's about to be deleted."""
        credstore = tmp_path / "credstore"
        credstore.mkdir()
        (credstore / "influx-token.cred").write_text("ciphertext")
        dropin = tmp_path / "dropin.conf"
        _regenerate_dropin(credstore_dir=str(credstore), dropin_path=str(dropin), exclude="influx-token")
        assert not dropin.exists()

    def test_removes_dropin_entirely_when_nothing_configured(self, tmp_path):
        credstore = tmp_path / "credstore"
        credstore.mkdir()
        dropin = tmp_path / "dropin.conf"
        dropin.write_text("[Service]\nLoadCredentialEncrypted=stale:whatever\n")
        _regenerate_dropin(credstore_dir=str(credstore), dropin_path=str(dropin))
        assert not dropin.exists()


# --------------------------------------------------------------------------- #
# _cmd_set / _cmd_remove happy paths
# --------------------------------------------------------------------------- #


class TestCmdSet:
    def test_secret_passed_via_stdin_not_argv(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text('influx:\n  token: "your_influx_token"\n')
        credstore = tmp_path / "credstore"
        dropin = tmp_path / "dropin.conf"

        monkeypatch.setattr(credential_cli, "CREDSTORE_DIR", str(credstore))
        monkeypatch.setattr(credential_cli, "DROPIN_PATH", str(dropin))
        monkeypatch.setattr(credential_cli.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(credential_cli.sys.stdin, "read", lambda: "real-secret-value\n")

        version_result = MagicMock(stdout="systemd 257\n")
        encrypt_calls = []

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["systemd-creds", "--version"]:
                return version_result
            if cmd[:2] == ["systemd-creds", "encrypt"]:
                encrypt_calls.append((cmd, kwargs))
                (credstore / "influx-token.cred").parent.mkdir(parents=True, exist_ok=True)
                (credstore / "influx-token.cred").write_text("ciphertext")
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            _cmd_set("influx-token", str(settings_path))

        assert len(encrypt_calls) == 1
        cmd, kwargs = encrypt_calls[0]
        assert "real-secret-value" not in cmd  # never in argv
        assert kwargs["input"] == b"real-secret-value"

        result_yaml = yaml.safe_load(settings_path.read_text())
        assert "stored in systemd-creds" in result_yaml["influx"]["token"]

    def test_settings_diff_is_only_the_one_line(self, tmp_path, monkeypatch):
        original = 'influx:\n  # a comment that must survive\n  token: "your_influx_token"\n  org: "myorg"\n'
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text(original)
        credstore = tmp_path / "credstore"
        dropin = tmp_path / "dropin.conf"

        monkeypatch.setattr(credential_cli, "CREDSTORE_DIR", str(credstore))
        monkeypatch.setattr(credential_cli, "DROPIN_PATH", str(dropin))
        monkeypatch.setattr(credential_cli.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(credential_cli.sys.stdin, "read", lambda: "real-secret-value")

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["systemd-creds", "--version"]:
                return MagicMock(stdout="systemd 257\n")
            if cmd[:2] == ["systemd-creds", "encrypt"]:
                credstore.mkdir(parents=True, exist_ok=True)
                (credstore / "influx-token.cred").write_text("ciphertext")
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            _cmd_set("influx-token", str(settings_path))

        result_lines = settings_path.read_text().splitlines()
        original_lines = original.splitlines()
        diffs = [i for i, (a, b) in enumerate(zip(original_lines, result_lines)) if a != b]
        assert diffs == [2]  # only the token: line changed
        assert "  # a comment that must survive" in result_lines
        assert '  org: "myorg"' in result_lines


class TestCmdRemove:
    def test_removes_cred_file_and_reverts_settings(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text('influx:\n  token: "<stored in systemd-creds - x>"\n')
        credstore = tmp_path / "credstore"
        credstore.mkdir()
        (credstore / "influx-token.cred").write_text("ciphertext")
        dropin = tmp_path / "dropin.conf"
        dropin.parent.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(credential_cli, "CREDSTORE_DIR", str(credstore))
        monkeypatch.setattr(credential_cli, "DROPIN_PATH", str(dropin))

        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            _cmd_remove("influx-token", str(settings_path))

        assert not (credstore / "influx-token.cred").exists()
        result_yaml = yaml.safe_load(settings_path.read_text())
        assert result_yaml["influx"]["token"] == "your_influx_token"


# --------------------------------------------------------------------------- #
# --set-field
# --------------------------------------------------------------------------- #


class TestCmdSetField:
    def test_writes_non_secret_field(self, tmp_path):
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text('hue:\n  host: "old.example.com"\n')
        _cmd_set_field("hue.host", "new.example.com", str(settings_path))
        result_yaml = yaml.safe_load(settings_path.read_text())
        assert result_yaml["hue"]["host"] == "new.example.com"

    def test_rejects_malformed_path(self, tmp_path):
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text('hue:\n  host: "old.example.com"\n')
        with pytest.raises(CredentialCliError, match=r"<section>"):
            _cmd_set_field("nofield", "value", str(settings_path))


# --------------------------------------------------------------------------- #
# --detect-influx-version
# --------------------------------------------------------------------------- #


class TestDetectInfluxVersion:
    def test_v2_from_health_endpoint(self):
        health_resp = MagicMock(status_code=200)
        health_resp.json.return_value = {"version": "2.7.1"}
        with patch("requests.get", return_value=health_resp):
            assert _detect_influx_version("http://localhost:8086") == "v2"

    def test_v1_from_ping_header_when_health_absent(self):
        def fake_get(url, **kwargs):
            if url.endswith("/health"):
                return MagicMock(status_code=404)
            return MagicMock(status_code=204, headers={"X-Influxdb-Version": "1.8.10"})

        with patch("requests.get", side_effect=fake_get):
            assert _detect_influx_version("http://localhost:8086") == "v1"

    def test_unknown_when_unreachable(self):
        import requests

        with patch("requests.get", side_effect=requests.exceptions.ConnectionError):
            assert _detect_influx_version("http://unreachable.example.com") == "unknown"

    def test_unknown_when_health_returns_non_json(self):
        health_resp = MagicMock(status_code=200)
        health_resp.json.side_effect = ValueError
        ping_resp = MagicMock(status_code=404, headers={})

        def fake_get(url, **kwargs):
            return health_resp if url.endswith("/health") else ping_resp

        with patch("requests.get", side_effect=fake_get):
            assert _detect_influx_version("http://localhost:8086") == "unknown"


# --------------------------------------------------------------------------- #
# --ensure-influx-storage
# --------------------------------------------------------------------------- #


class TestEnsureInfluxStorage:
    def test_v1_create_database_idempotent_success(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text('influx:\n  url: "http://localhost:8086"\n  user: "admin"\n  password: "sentinel"\n')
        credstore = tmp_path / "credstore"
        credstore.mkdir()
        (credstore / "influx-password.cred").write_text("ciphertext")
        monkeypatch.setattr(credential_cli, "CREDSTORE_DIR", str(credstore))

        decrypt_result = MagicMock(stdout=b"real-password\n")
        post_result = MagicMock(status_code=200)
        post_result.raise_for_status.return_value = None

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["systemd-creds", "decrypt"]:
                return decrypt_result
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("subprocess.run", side_effect=fake_run), patch("requests.post", return_value=post_result) as post:
            _cmd_ensure_influx_storage("speedtest_db", str(settings_path))

        _, kwargs = post.call_args
        assert kwargs["auth"] == ("admin", "real-password")
        assert kwargs["params"]["q"] == 'CREATE DATABASE "speedtest_db"'

    def test_v1_failure_does_not_raise(self, tmp_path, monkeypatch):
        import requests

        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text('influx:\n  url: "http://localhost:8086"\n  user: "admin"\n  password: "sentinel"\n')
        credstore = tmp_path / "credstore"
        credstore.mkdir()
        (credstore / "influx-password.cred").write_text("ciphertext")
        monkeypatch.setattr(credential_cli, "CREDSTORE_DIR", str(credstore))

        with patch("subprocess.run", return_value=MagicMock(stdout=b"real-password\n")):
            with patch("requests.post", side_effect=requests.exceptions.ConnectionError):
                _cmd_ensure_influx_storage("speedtest_db", str(settings_path))  # does not raise

    def test_v2_lists_before_creating_and_skips_if_present(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text('influx:\n  url: "http://localhost:8086"\n  org: "myorg"\n  token: "sentinel"\n')
        credstore = tmp_path / "credstore"
        credstore.mkdir()
        (credstore / "influx-token.cred").write_text("ciphertext")
        monkeypatch.setattr(credential_cli, "CREDSTORE_DIR", str(credstore))

        list_resp = MagicMock(status_code=200)
        list_resp.raise_for_status.return_value = None
        list_resp.json.return_value = {"buckets": [{"name": "speedtest_db"}]}

        with patch("subprocess.run", return_value=MagicMock(stdout=b"real-token\n")):
            with patch("requests.get", return_value=list_resp) as get, patch("requests.post") as post:
                _cmd_ensure_influx_storage("speedtest_db", str(settings_path))

        assert get.called
        post.assert_not_called()


# --------------------------------------------------------------------------- #
# main() - root check, --list, arg wiring
# --------------------------------------------------------------------------- #


class TestMain:
    def test_non_root_set_exits_without_touching_any_file(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text('influx:\n  token: "your_influx_token"\n')
        original = settings_path.read_text()

        monkeypatch.setattr("os.geteuid", lambda: 1000)
        with patch("subprocess.run") as run:
            code = main(["influx-token", "--settings", str(settings_path)])

        assert code == 1
        assert settings_path.read_text() == original
        run.assert_not_called()

    def test_list_does_not_require_root(self, tmp_path, capsys):
        code = main(["--list"])
        assert code == 0
        out = capsys.readouterr().out
        assert "influx-token" in out
