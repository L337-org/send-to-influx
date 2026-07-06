"""Unit tests for sendtoinflux (signal_handler, main, helper functions)."""

import signal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
import sendtoinflux


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
            mock_get_class.assert_called_once_with("zappi")

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
        args = SimpleNamespace(print=False, dump=False)

        interval = sendtoinflux.collect_source_data("hue", args, handler)

        assert interval == 123
        handler.get_data.assert_called_once()
        handler.send_data.assert_called_once()

    def test_run_multi_source_coerces_invalid_stagger_to_zero(self):
        """run_multi_source falls back to zero stagger when value is invalid."""
        args = SimpleNamespace(print=False, dump=False)
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True

        with (
            patch("sendtoinflux.create_source_worker") as mock_create_source_worker,
            patch("sendtoinflux.spawn_source_thread", return_value=fake_thread),
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_multi_source(["hue", "zappi"], args, "not-an-int")

        mock_create_source_worker.assert_any_call("hue", 0, args)
        mock_create_source_worker.assert_any_call("zappi", 0, args)


class TestSendHeartbeat:
    """Tests for send_heartbeat."""

    def test_no_op_when_handler_is_none(self):
        """send_heartbeat does nothing when no handler has been constructed yet."""
        sendtoinflux.send_heartbeat(None, "hue", ok=True, consecutive_failures=0)

    def test_sends_ok_status_and_restores_header(self):
        """send_heartbeat writes ok=1 and restores the handler's original influx_header."""
        handler = MagicMock()
        handler.influx_header = "hue,host=test "
        sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)
        handler.send_data.assert_called_once_with(data={"ok": 1, "consecutive_failures": 0})
        assert handler.influx_header == "hue,host=test "

    def test_sends_failure_status_with_count(self):
        """send_heartbeat writes ok=0 with the current consecutive failure count."""
        handler = MagicMock()
        handler.influx_header = "hue "
        sendtoinflux.send_heartbeat(handler, "hue", ok=False, consecutive_failures=3)
        handler.send_data.assert_called_once_with(data={"ok": 0, "consecutive_failures": 3})

    def test_uses_collector_status_measurement_while_sending(self):
        """send_heartbeat temporarily swaps in the collector_status header for the write."""
        handler = MagicMock()
        handler.influx_header = "hue "
        captured = {}
        handler.send_data.side_effect = lambda data=None: captured.update(header=handler.influx_header)

        sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)

        assert captured["header"] == "collector_status,source=hue "

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
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False))

        assert handler.get_data.call_count >= 1

    def test_systemexit_is_caught_and_loop_continues(self):
        """run_single_source catches SystemExit from get_data and retries."""
        handler = self._make_handler()
        handler.get_data.side_effect = [SystemExit(2), Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False))

        assert handler.get_data.call_count >= 1

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
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False))

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
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False))

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
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False))

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
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=False, dump=False))

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
                sendtoinflux.run_single_source("hue", SimpleNamespace(print=True, dump=False))

        mock_heartbeat.assert_not_called()


class TestCreateSourceWorkerHeartbeat:
    """Tests for heartbeat wiring in the multi-source worker."""

    def test_worker_sends_heartbeat_on_success(self):
        """The multi-source worker sends an ok=1 heartbeat after a successful cycle."""
        handler = MagicMock()
        handler.source_settings = {"interval": 60}
        handler.get_data.return_value = {"x": 1}
        args = SimpleNamespace(print=False, dump=False)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, KeyboardInterrupt()]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            worker = sendtoinflux.create_source_worker("hue", 0, args)
            with pytest.raises(KeyboardInterrupt):
                worker()

        mock_heartbeat.assert_called_once_with(handler, "hue", ok=True, consecutive_failures=0)

    def test_worker_sends_heartbeat_on_failure(self):
        """The multi-source worker sends an ok=0 heartbeat with the failure count on error."""
        handler = MagicMock()
        handler.source_settings = {"interval": 60}
        handler.get_data.side_effect = Exception("network error")
        args = SimpleNamespace(print=False, dump=False)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, KeyboardInterrupt()]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            worker = sendtoinflux.create_source_worker("hue", 0, args)
            with pytest.raises(KeyboardInterrupt):
                worker()

        mock_heartbeat.assert_any_call(handler, "hue", ok=False, consecutive_failures=1)

    def test_worker_skips_heartbeat_in_print_mode(self):
        """The multi-source worker does not write heartbeats in --print mode."""
        handler = MagicMock()
        handler.source_settings = {"interval": 60}
        handler.get_data.return_value = {"x": 1}
        args = SimpleNamespace(print=True, dump=False)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, KeyboardInterrupt()]),
            patch("sendtoinflux.print_source_data"),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            worker = sendtoinflux.create_source_worker("hue", 0, args)
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
