"""Unit tests for sendtoinflux (signal_handler, main, helper functions)."""

import signal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
import sendtoinflux
from toinflux.exceptions import ConfigError


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
            assert "21" in call_arg

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
            patch("sendtoinflux.print") as mock_print,
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "--check-config"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(0)) as mock_exit,
        ):
            mock_load_settings.return_value = {"default_source": "hue"}
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            mock_exit.assert_called_once_with(0)
            mock_print.assert_called_once_with("Configuration OK")

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
