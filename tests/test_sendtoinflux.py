"""Unit tests for sendtoinflux (signal_handler, main, helper functions)."""

import signal
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch
import pytest
import sendtoinflux
from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.influx import DataHandler


class TestSignalHandler:
    """Tests for signal_handler."""

    def test_signal_handler_exits_with_zero(self):
        """signal_handler prints message and exits with 0."""
        with patch("sendtoinflux.sys.exit") as mock_exit:
            sendtoinflux.signal_handler(2, None)
            mock_exit.assert_called_once_with(0)

    def test_signal_handler_accepts_frame(self):
        """signal_handler accepts frame argument (no crash)."""
        with patch("sendtoinflux.sys.exit"):
            sendtoinflux.signal_handler(2, object())


class TestMain:
    """Tests for main."""

    def test_main_dump_mode_prints_json_and_exits(self, mock_main_deps):
        """main with -d/--dump gets data, prints JSON, and exits 0."""
        mock_handler, _ = mock_main_deps
        mock_handler.get_data.return_value = {"temp": 21}
        with (
            patch("sendtoinflux.print") as mock_print,
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-d"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(0)) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_exit.assert_called_once_with(0)
            mock_handler.get_data.assert_called_once()
            mock_print.assert_called_once()
            call_arg = mock_print.call_args[0][0]
            assert "temp" in call_arg

    def test_main_dump_mode_exits_two_on_source_connection_error(self, mock_main_deps):
        """main with -d/--dump exits 2 (not an unhandled traceback) on a SourceConnectionError."""
        mock_handler, _ = mock_main_deps
        mock_handler.get_data.side_effect = SourceConnectionError("401 Unauthorized")
        with (
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-d"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(2)) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_exit.assert_called_once_with(2)

    def test_main_print_mode_one_iteration(self, mock_main_deps):
        """main with --print runs one loop iteration then we break via sleep."""
        mock_handler, _ = mock_main_deps
        mock_handler.get_data.return_value = {"x": 1}
        with (
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.strftime", return_value="Thu, 01 Jan 1970 00:00:00 UTC"),
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-p"]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            assert mock_handler.get_data.called

    def test_main_send_mode_one_iteration(self, mock_main_deps):
        """main without --print sends data once then we break via sleep."""
        mock_handler, _ = mock_main_deps
        mock_handler.get_data.return_value = {"x": 1}
        with (
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_handler.send_data.assert_called()

    def test_main_uses_source_arg(self, mock_main_deps):
        """main with -s source passes source to get_class."""
        _, mock_get_class = mock_main_deps
        with (
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-s", "zappi"]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_get_class.assert_called_once_with("zappi", None)

    def test_main_uses_settings_arg(self, mock_main_deps):
        """main with --settings passes the path through to load_settings and get_class."""
        _, mock_get_class = mock_main_deps
        with (
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch(
                "sendtoinflux.sys.argv",
                ["sendtoinflux", "-s", "zappi", "--settings", "/etc/send-to-influx/settings.yaml"],
            ),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_get_class.assert_called_once_with("zappi", "/etc/send-to-influx/settings.yaml")

    def test_main_registers_sigterm_handler(self, mock_main_deps):
        """main registers signal_handler for both SIGINT and SIGTERM."""
        with (
            patch("sendtoinflux.signal.signal") as mock_signal,
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            registered = [c[0][0] for c in mock_signal.call_args_list]
            assert signal.SIGINT in registered
            assert signal.SIGTERM in registered

    def test_main_without_source_runs_configured_sources(self):
        """main without --source starts multi-source mode using settings sources list."""
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
            patch("sendtoinflux.run_multi_source") as mock_run_multi_source,
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
        ):
            mock_load_settings.return_value = {
                "default_source": "hue",
                "sources": ["hue", "zappi", "speedtest"],
                "stagger_seconds": 3,
            }
            sendtoinflux.main()
            mock_run_multi_source.assert_called_once()
            call_args = mock_run_multi_source.call_args[0]
            assert call_args[0] == ["hue", "zappi", "speedtest"]
            assert call_args[2] == 3

    def test_main_logs_sources_on_multi_source_startup(self, caplog):
        """main logs the configured sources list when starting in multi-source mode."""
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
            patch("sendtoinflux.run_multi_source"),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
            caplog.at_level("INFO"),
        ):
            mock_load_settings.return_value = {
                "default_source": "hue",
                "sources": ["hue", "zappi", "speedtest"],
                "stagger_seconds": 3,
            }
            sendtoinflux.main()
            assert any("hue, zappi, speedtest" in record.message for record in caplog.records)

    def test_main_logs_source_on_single_source_startup(self, mock_main_deps, caplog):
        """main logs the source name when started with -s/--source."""
        with (
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-s", "zappi"]),
            caplog.at_level("INFO"),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            assert any("source=zappi" in record.message for record in caplog.records)

    def test_main_logs_default_source_on_startup(self, mock_main_deps, caplog):
        """main logs the default_source when no --source or settings sources list is given."""
        with (
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
            caplog.at_level("INFO"),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            assert any("source=hue, from default_source" in record.message for record in caplog.records)

    def test_main_version_flag_prints_version_and_exits_zero(self, capsys):
        """main with --version prints the version string and exits 0, without needing settings."""
        with patch("sendtoinflux.sys.argv", ["sendtoinflux", "--version"]):
            with pytest.raises(SystemExit) as exc_info:
                sendtoinflux.main()
            assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert sendtoinflux.__version__ in captured.out

    def test_main_check_config_prints_ok_and_exits_zero(self):
        """main with --check-config validates settings, prints a success message, and exits 0."""
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
            patch("sendtoinflux.toinflux.validate_settings") as mock_validate_settings,
            patch("sendtoinflux.print") as mock_print,
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "--check-config"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(0)) as mock_exit,
        ):
            mock_load_settings.return_value = {"default_source": "hue"}
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_exit.assert_called_once_with(0)
            mock_validate_settings.assert_called_once_with(
                {"default_source": "hue"}, source=None, settings_path="settings.yaml"
            )
            mock_print.assert_called_once_with("Configuration OK")

    def test_main_check_config_validates_explicit_source_argument(self, tmp_path):
        """--check-config also validates the source named by --source, even if it isn't in sources/default_source.

        Uses a real settings file and the real validate_settings() (not mocked), since
        that's exactly the code path a fully-mocked test can't catch a gap in.
        """
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text("""
default_source: hue
influx:
  url: "http://influx.example.com:8086"
  user: "u"
  password: "p"
hue:
  db: hue_db
  interval: 300
octopus:
  db: octopus_db
""")
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.print") as mock_print,
            patch(
                "sendtoinflux.sys.argv",
                ["sendtoinflux", "--check-config", "--source", "octopus", "--settings", str(settings_path)],
            ),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_exit.assert_called_once_with(1)
            call_arg = mock_print.call_args[0][0]
            assert "octopus.interval is required" in call_arg

    def test_main_check_config_prints_error_and_exits_one_when_invalid(self):
        """main with --check-config prints the error and exits 1 when settings are invalid."""
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings", side_effect=ConfigError("influx.url is required")),
            patch("sendtoinflux.print") as mock_print,
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "--check-config"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_exit.assert_called_once_with(1)
            mock_print.assert_called_once_with("Configuration error: influx.url is required")

    def test_main_verbose_flag_forces_debug_loglevel(self, mock_main_deps):
        """main with -v/--verbose overrides the configured loglevel with DEBUG."""
        with (
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-v"]),
            patch("sendtoinflux.toinflux.configure_logging") as mock_configure_logging,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            assert mock_configure_logging.call_args.kwargs["loglevel"] == "DEBUG"

    def test_main_uses_settings_loglevel_when_not_verbose(self, mock_main_deps):
        """main uses the 'loglevel' settings.yaml key when -v is not passed."""
        with (
            patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
            patch("sendtoinflux.toinflux.configure_logging") as mock_configure_logging,
        ):
            mock_load_settings.return_value = {"default_source": "hue", "loglevel": "WARNING"}
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            assert mock_configure_logging.call_args.kwargs["loglevel"] == "WARNING"

    def test_main_passes_log_rotation_settings_through(self, mock_main_deps):
        """main forwards log_max_bytes/log_backup_count settings keys to configure_logging."""
        with (
            patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
            patch("sendtoinflux.toinflux.configure_logging") as mock_configure_logging,
        ):
            mock_load_settings.return_value = {
                "default_source": "hue",
                "logfile": "/tmp/send-to-influx-test.log",
                "log_max_bytes": 123,
                "log_backup_count": 7,
            }
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            kwargs = mock_configure_logging.call_args.kwargs
            assert kwargs["log_max_bytes"] == 123
            assert kwargs["log_backup_count"] == 7

    def test_main_logs_and_exits_one_when_configure_logging_raises_config_error(self, mock_main_deps):
        """main catches ConfigError from configure_logging (e.g. an unwritable logfile) and exits 1 cleanly."""
        with (
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
            patch("sendtoinflux.toinflux.configure_logging", side_effect=ConfigError("Cannot open logfile 'x'")),
            patch("sendtoinflux.logging.critical") as mock_critical,
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_exit.assert_called_once_with(1)
            mock_critical.assert_called_once_with("%s", ANY)
            assert "Cannot open logfile" in str(mock_critical.call_args[0][1])

    def test_main_multi_source_dump_requires_source(self):
        """main in multi-source mode exits when --dump is used without --source."""
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "--dump"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        ):
            mock_load_settings.return_value = {
                "default_source": "hue",
                "sources": ["hue", "zappi"],
            }
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_exit.assert_called_once_with(1)


class TestHelpers:
    """Tests for helper functions used by multi-source mode."""

    def test_get_backoff_delay_caps_at_max(self):
        """get_backoff_delay caps large failure counts at configured maximum."""
        delay = sendtoinflux.get_backoff_delay(10_000, backoff_base_seconds=5, backoff_max_seconds=300)
        assert delay == 300

    def test_collect_source_data_uses_existing_handler(self):
        """collect_source_data uses the supplied handler instead of reloading one."""
        handler = MagicMock()
        handler.get_data.return_value = {"x": 1}
        handler.source_settings = {"interval": 123}
        args = SimpleNamespace(print=False, dump=False, settings=None)

        interval = sendtoinflux.collect_source_data("hue", args, handler)

        assert interval == 123
        handler.get_data.assert_called_once()
        handler.send_data.assert_called_once()

    def test_run_multi_source_coerces_invalid_stagger_to_zero(self):
        """run_multi_source falls back to zero stagger when value is invalid."""
        args = SimpleNamespace(print=False, dump=False, settings=None)
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True

        with (
            patch("sendtoinflux.create_source_worker") as mock_create_source_worker,
            patch("sendtoinflux.spawn_source_thread", return_value=fake_thread),
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_multi_source(["hue", "zappi"], args, "not-an-int")

        mock_create_source_worker.assert_any_call("hue", 0, args, set())
        mock_create_source_worker.assert_any_call("zappi", 0, args, set())

    def test_create_source_worker_stops_permanently_on_config_error(self):
        """create_source_worker adds the source to stopped_sources and returns (no retry) on ConfigError."""
        handler = MagicMock()
        handler.get_data.side_effect = ConfigError("bad config")
        args = SimpleNamespace(print=False, dump=False, settings=None)
        stopped_sources = set()

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep"),
        ):
            worker = sendtoinflux.create_source_worker("hue", 0, args, stopped_sources)
            worker()  # should return normally, not raise or loop forever

        assert stopped_sources == {"hue"}
        handler.get_data.assert_called_once()

    def test_run_multi_source_does_not_restart_stopped_source(self):
        """run_multi_source does not restart a thread whose source gave up with a ConfigError."""
        args = SimpleNamespace(print=False, dump=False, settings=None)

        def make_dead_thread():
            thread = MagicMock()
            thread.is_alive.return_value = False
            return thread

        with (
            patch("sendtoinflux.create_source_worker") as mock_create_source_worker,
            patch("sendtoinflux.spawn_source_thread", side_effect=lambda worker: make_dead_thread()) as mock_spawn,
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
        ):
            # simulate "zappi" having already stopped permanently by the time the
            # supervisor loop runs its first check
            def fake_create_source_worker(source, delay, worker_args, stopped_sources):
                if source == "zappi":
                    stopped_sources.add("zappi")
                return MagicMock()

            mock_create_source_worker.side_effect = fake_create_source_worker

            with pytest.raises(SystemExit):
                sendtoinflux.run_multi_source(["hue", "zappi"], args, 0)

        # both threads report dead (2 initial spawns), but only "hue" (not in
        # stopped_sources) should have triggered a respawn attempt (3rd spawn)
        assert mock_spawn.call_count == 3


class TestSendHeartbeat:
    """Tests for send_heartbeat."""

    def test_no_op_when_handler_is_none(self):
        """send_heartbeat does nothing when no handler has been constructed yet."""
        sendtoinflux.send_heartbeat(None, "hue", ok=True, consecutive_failures=0)

    def test_sends_ok_status_and_restores_header(self):
        """send_heartbeat writes ok=1 and restores the handler's original influx_header."""
        handler = MagicMock()
        handler.influx_header = "hue,host=test "
        with patch("sendtoinflux.time.time", return_value=1700000000.0):
            sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)
        handler.send_data.assert_called_once_with(data={"ok": 1, "consecutive_failures": 0}, timestamp=1700000000)
        assert handler.influx_header == "hue,host=test "

    def test_sends_failure_status_with_count(self):
        """send_heartbeat writes ok=0 with the current consecutive failure count."""
        handler = MagicMock()
        handler.influx_header = "hue "
        with patch("sendtoinflux.time.time", return_value=1700000000.0):
            sendtoinflux.send_heartbeat(handler, "hue", ok=False, consecutive_failures=3)
        handler.send_data.assert_called_once_with(data={"ok": 0, "consecutive_failures": 3}, timestamp=1700000000)

    def test_uses_collector_status_measurement_while_sending(self):
        """send_heartbeat temporarily swaps in the collector_status header for the write."""
        handler = MagicMock()
        handler.influx_header = "hue "
        captured = {}
        handler.send_data.side_effect = lambda data=None, timestamp=None: captured.update(header=handler.influx_header)

        sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)

        assert captured["header"] == "collector_status,source=hue "

    def test_uses_current_time_not_a_stale_self_timestamp(self, sample_settings):
        """send_heartbeat writes with the current time, not a stale self.timestamp set by an earlier get_data() cycle.

        Uses a real DataHandler (not a bare mock) so the actual send_data() timestamp
        fallback logic in influx.py runs, since that's exactly the interaction a fully
        mocked handler can't catch.
        """
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = DataHandler(source="hue")
            handler.influx_header = "hue "
            # Simulate a handler whose last get_data() cycle set a stale timestamp
            # (e.g. Octopus using a delayed reading's interval_start).
            handler.timestamp = 1000000000
            with (
                patch.object(handler.session, "post") as mock_post,
                patch("sendtoinflux.time.time", return_value=2000000000.0),
            ):
                mock_post.return_value.raise_for_status = MagicMock()
                sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)
                body = mock_post.call_args[1]["data"]
                assert body.endswith(" 2000000000")

    def test_swallows_send_failures(self):
        """A heartbeat write failure is logged and swallowed, not raised."""
        handler = MagicMock()
        handler.influx_header = "hue "
        handler.send_data.side_effect = Exception("network error")

        sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)  # should not raise

        assert handler.influx_header == "hue "


class TestMaybeSendHeartbeat:
    """Tests for maybe_send_heartbeat."""

    def test_sends_when_not_in_print_mode(self):
        """maybe_send_heartbeat delegates to send_heartbeat when not in --print mode."""
        handler = MagicMock()
        args = SimpleNamespace(print=False, dump=False)
        with patch("sendtoinflux.send_heartbeat") as mock_heartbeat:
            sendtoinflux.maybe_send_heartbeat(args, handler, "hue", ok=True, consecutive_failures=0)
        mock_heartbeat.assert_called_once_with(handler, "hue", ok=True, consecutive_failures=0)

    def test_skips_in_print_mode(self):
        """maybe_send_heartbeat does not touch InfluxDB in --print mode."""
        handler = MagicMock()
        args = SimpleNamespace(print=True, dump=False)
        with patch("sendtoinflux.send_heartbeat") as mock_heartbeat:
            sendtoinflux.maybe_send_heartbeat(args, handler, "hue", ok=True, consecutive_failures=0)
        mock_heartbeat.assert_not_called()


class TestRunSingleSourceRetry:
    """Tests for retry/backoff behaviour in run_single_source."""

    def _make_handler(self):
        handler = MagicMock()
        handler.source_settings = {"interval": 60}
        return handler

    def test_exception_is_caught_and_loop_continues(self):
        """run_single_source catches Exception, resets handler, and retries."""
        handler = self._make_handler()
        handler.get_data.side_effect = [Exception("network error"), Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False, settings=None))

        assert handler.get_data.call_count >= 1

    def test_config_error_exits_immediately_without_retry(self):
        """run_single_source exits with code 1 on ConfigError instead of retrying."""
        handler = self._make_handler()
        handler.get_data.side_effect = ConfigError("bad config")

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
        ):
            with pytest.raises(SystemExit) as exc_info:
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False, settings=None))

        assert exc_info.value.code == 1
        handler.get_data.assert_called_once()

    def test_handler_is_recreated_after_failure(self):
        """run_single_source calls get_class again after a failure resets the handler."""
        handler = self._make_handler()
        handler.get_data.side_effect = [Exception("fail"), Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler) as mock_get_class,
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False, settings=None))

        # Called once before the loop, then again after the failure reset
        assert mock_get_class.call_count == 2

    def test_failure_count_increments_backoff(self):
        """run_single_source passes increasing failure_count to get_backoff_delay."""
        handler = self._make_handler()
        handler.get_data.side_effect = Exception("always fails")
        delays = []

        original_backoff = sendtoinflux.get_backoff_delay

        def capturing_backoff(failure_count, **kwargs):
            delays.append(failure_count)
            return original_backoff(failure_count, **kwargs)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.get_backoff_delay", side_effect=capturing_backoff),
            patch("sendtoinflux.time.sleep", side_effect=[None, None, SystemExit(0)]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False, settings=None))

        assert len(delays) >= 2
        assert delays == list(range(1, len(delays) + 1))

    def test_sends_heartbeat_on_success(self):
        """run_single_source sends an ok=1 heartbeat after a successful cycle."""
        handler = self._make_handler()
        handler.get_data.side_effect = [{"x": 1}, Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False, settings=None))

        mock_heartbeat.assert_any_call(handler, "hue", ok=True, consecutive_failures=0)

    def test_sends_heartbeat_on_failure_with_failure_count(self):
        """run_single_source sends an ok=0 heartbeat with the failure count after a failed cycle."""
        handler = self._make_handler()
        handler.get_data.side_effect = [Exception("network error"), Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False, settings=None))

        mock_heartbeat.assert_any_call(handler, "hue", ok=False, consecutive_failures=1)

    def test_skips_heartbeat_in_print_mode(self):
        """run_single_source does not write heartbeats in --print mode."""
        handler = self._make_handler()
        handler.get_data.side_effect = [{"x": 1}, Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=True, dump=False, settings=None))

        mock_heartbeat.assert_not_called()


class TestCreateSourceWorkerHeartbeat:
    """Tests for heartbeat wiring in the multi-source worker."""

    def test_worker_sends_heartbeat_on_success(self):
        """The multi-source worker sends an ok=1 heartbeat after a successful cycle."""
        handler = MagicMock()
        handler.source_settings = {"interval": 60}
        handler.get_data.return_value = {"x": 1}
        args = SimpleNamespace(print=False, dump=False, settings=None)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, KeyboardInterrupt()]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            worker = sendtoinflux.create_source_worker("hue", 0, args, set())
            with pytest.raises(KeyboardInterrupt):
                worker()

        mock_heartbeat.assert_called_once_with(handler, "hue", ok=True, consecutive_failures=0)

    def test_worker_sends_heartbeat_on_failure(self):
        """The multi-source worker sends an ok=0 heartbeat with the failure count on error."""
        handler = MagicMock()
        handler.source_settings = {"interval": 60}
        handler.get_data.side_effect = Exception("network error")
        args = SimpleNamespace(print=False, dump=False, settings=None)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, KeyboardInterrupt()]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            worker = sendtoinflux.create_source_worker("hue", 0, args, set())
            with pytest.raises(KeyboardInterrupt):
                worker()

        mock_heartbeat.assert_any_call(handler, "hue", ok=False, consecutive_failures=1)

    def test_worker_skips_heartbeat_in_print_mode(self):
        """The multi-source worker does not write heartbeats in --print mode."""
        handler = MagicMock()
        handler.source_settings = {"interval": 60}
        handler.get_data.return_value = {"x": 1}
        args = SimpleNamespace(print=True, dump=False, settings=None)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, KeyboardInterrupt()]),
            patch("sendtoinflux.print_source_data"),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            worker = sendtoinflux.create_source_worker("hue", 0, args, set())
            with pytest.raises(KeyboardInterrupt):
                worker()

        mock_heartbeat.assert_not_called()


class TestConfigureLogging:
    """Tests for configure_logging."""

    def _remove_handlers(self, root, added):
        for h in added:
            root.removeHandler(h)
            h.close()

    def test_adds_stdout_stream_handler(self):
        """configure_logging adds a StreamHandler writing to stdout."""
        import logging
        import sys
        from toinflux.general import configure_logging

        root = logging.getLogger()
        before = set(root.handlers)
        try:
            configure_logging()
            added = [h for h in root.handlers if h not in before]
            stream_handlers = [
                h for h in added if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            ]
            assert len(stream_handlers) == 1
            assert stream_handlers[0].stream is sys.stdout
        finally:
            self._remove_handlers(root, [h for h in root.handlers if h not in before])

    def test_adds_file_handler_when_logfile_provided(self):
        """configure_logging adds a FileHandler when logfile is specified."""
        import logging
        import tempfile
        import os
        from toinflux.general import configure_logging

        root = logging.getLogger()
        before = set(root.handlers)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as f:
            logfile = f.name
        try:
            configure_logging(logfile=logfile)
            added = [h for h in root.handlers if h not in before]
            file_handlers = [h for h in added if isinstance(h, logging.FileHandler)]
            assert len(file_handlers) == 1
        finally:
            self._remove_handlers(root, [h for h in root.handlers if h not in before])
            os.unlink(logfile)

    def test_no_file_handler_without_logfile(self):
        """configure_logging does not add a FileHandler when logfile is None."""
        import logging
        from toinflux.general import configure_logging

        root = logging.getLogger()
        before = set(root.handlers)
        try:
            configure_logging()
            added = [h for h in root.handlers if h not in before]
            file_handlers = [h for h in added if isinstance(h, logging.FileHandler)]
            assert len(file_handlers) == 0
        finally:
            self._remove_handlers(root, [h for h in root.handlers if h not in before])

    def test_repeated_calls_do_not_duplicate_handlers(self):
        """configure_logging replaces its own handlers rather than accumulating them."""
        import logging
        from toinflux.general import configure_logging

        root = logging.getLogger()
        before = set(root.handlers)
        try:
            configure_logging()
            configure_logging()
            configure_logging()
            added = [h for h in root.handlers if h not in before]
            stream_handlers = [
                h for h in added if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            ]
            assert len(stream_handlers) == 1
        finally:
            self._remove_handlers(root, [h for h in root.handlers if h not in before])

    def test_sets_specified_loglevel(self):
        """configure_logging sets the root logger to the requested level."""
        import logging
        from toinflux.general import configure_logging

        root = logging.getLogger()
        before = set(root.handlers)
        previous_level = root.level
        try:
            configure_logging(loglevel="DEBUG")
            assert root.level == logging.DEBUG
        finally:
            self._remove_handlers(root, [h for h in root.handlers if h not in before])
            root.setLevel(previous_level)

    def test_invalid_loglevel_defaults_to_info(self):
        """configure_logging falls back to INFO when given an unrecognised level name."""
        import logging
        from toinflux.general import configure_logging

        root = logging.getLogger()
        before = set(root.handlers)
        previous_level = root.level
        try:
            configure_logging(loglevel="NOT_A_LEVEL")
            assert root.level == logging.INFO
        finally:
            self._remove_handlers(root, [h for h in root.handlers if h not in before])
            root.setLevel(previous_level)

    def test_file_handler_is_rotating_with_custom_params(self):
        """configure_logging uses a RotatingFileHandler honouring maxBytes/backupCount."""
        import logging
        import tempfile
        import os
        from logging.handlers import RotatingFileHandler
        from toinflux.general import configure_logging

        root = logging.getLogger()
        before = set(root.handlers)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as f:
            logfile = f.name
        try:
            configure_logging(logfile=logfile, log_max_bytes=1234, log_backup_count=7)
            added = [h for h in root.handlers if h not in before]
            file_handlers = [h for h in added if isinstance(h, RotatingFileHandler)]
            assert len(file_handlers) == 1
            assert file_handlers[0].maxBytes == 1234
            assert file_handlers[0].backupCount == 7
        finally:
            self._remove_handlers(root, [h for h in root.handlers if h not in before])
            os.unlink(logfile)

    def test_unwritable_logfile_raises_config_error(self):
        """configure_logging raises ConfigError (not a raw OSError) when the logfile can't be opened."""
        import logging
        from toinflux.general import configure_logging
        from toinflux.exceptions import ConfigError

        root = logging.getLogger()
        before = set(root.handlers)
        try:
            with pytest.raises(ConfigError, match="Cannot open logfile"):
                configure_logging(logfile="/nonexistent-directory/send-to-influx.log")
        finally:
            self._remove_handlers(root, [h for h in root.handlers if h not in before])
