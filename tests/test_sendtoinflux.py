"""Unit tests for sendtoinflux (signal_handler, main, helper functions)."""

import itertools
import json
import logging
import signal
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch
import pytest
import sendtoinflux
from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.influx import DataHandler, InfluxWriteError


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


class TestRegisterThreadDumpHandler:
    """Tests for register_thread_dump_handler."""

    def test_registers_on_a_platform_with_sigusr1(self):
        with patch("sendtoinflux.faulthandler.register") as mock_register:
            sendtoinflux.register_thread_dump_handler()

        mock_register.assert_called_once_with(signal.SIGUSR1, all_threads=True)

    def test_skips_registration_when_sigusr1_is_unavailable(self):
        """Windows (and any other platform without SIGUSR1) must not raise
        AttributeError here - that would take down startup entirely, including
        plain --version/--help runs, since this is called unconditionally near
        the top of main()."""

        class _SignalModuleWithoutSigusr1:
            """A stand-in for the signal module with SIGUSR1 deleted."""

        with (
            patch("sendtoinflux.signal", _SignalModuleWithoutSigusr1()),
            patch("sendtoinflux.faulthandler.register") as mock_register,
        ):
            sendtoinflux.register_thread_dump_handler()  # must not raise

        mock_register.assert_not_called()

    def test_degrades_to_a_warning_when_register_itself_fails(self):
        """e.g. stderr has no real file descriptor (observed under pytest's
        captured output) - an optional diagnostic must not crash the process."""
        with patch("sendtoinflux.faulthandler.register", side_effect=OSError("no fileno")):
            sendtoinflux.register_thread_dump_handler()  # must not raise


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
            mock_get_class.assert_called_once_with("zappi", None, instance=None)

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
            mock_get_class.assert_called_once_with("zappi", "/etc/send-to-influx/settings.yaml", instance=None)

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
            patch("sendtoinflux.run_workers") as mock_run_workers,
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
        ):
            mock_load_settings.return_value = {
                "default_source": "hue",
                "sources": ["hue", "zappi", "speedtest"],
                "stagger_seconds": 3,
                # A real bridge: hue expands to one worker per *configured* bridge, so a
                # hue with no block at all would correctly expand to no worker and drop
                # out of the list these tests are checking.
                "hue": {"db": "hue_db", "interval": 60, "host": "hue.example.com", "user": "tok"},
            }
            sendtoinflux.main()
            mock_run_workers.assert_called_once()
            call_args = mock_run_workers.call_args[0]
            assert call_args[0] == [("hue", "hue.example.com"), ("zappi", None), ("speedtest", None)]
            assert call_args[2] == 3

    def test_main_logs_sources_on_multi_source_startup(self, caplog):
        """main logs the configured sources list when starting in multi-source mode."""
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
            patch("sendtoinflux.run_workers"),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
            caplog.at_level("INFO"),
        ):
            mock_load_settings.return_value = {
                "default_source": "hue",
                "sources": ["hue", "zappi", "speedtest"],
                "stagger_seconds": 3,
                # A real bridge: hue expands to one worker per *configured* bridge, so a
                # hue with no block at all would correctly expand to no worker and drop
                # out of the list these tests are checking.
                "hue": {"db": "hue_db", "interval": 60, "host": "hue.example.com", "user": "tok"},
            }
            sendtoinflux.main()
            assert any("workers=hue@hue.example.com, zappi, speedtest" in record.message for record in caplog.records)

    def test_main_logs_source_on_single_source_startup(self, mock_main_deps, caplog):
        """main logs the source name when started with -s/--source."""
        with (
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-s", "zappi"]),
            caplog.at_level("INFO"),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            assert any("workers=zappi" in record.message for record in caplog.records)

    def test_main_logs_default_source_on_startup(self, mock_main_deps, caplog):
        """main logs the default_source when no --source or settings sources list is given."""
        with (
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
            caplog.at_level("INFO"),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
            assert any(
                "workers=hue@hue.example.com, from default_source" in record.message for record in caplog.records
            )

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
            # warn=True: --check-config is the one mode whose job is reporting on the
            # configuration, so non-fatal findings belong in its output. Everywhere else
            # validate_settings() runs via load_settings() on every handler construction.
            mock_validate_settings.assert_called_once_with(
                {"default_source": "hue"}, source=None, settings_path="settings.yaml", warn=True
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
            # On stderr, so --check-config's stdout carries only its verdict.
            mock_print.assert_called_once_with("Configuration error: influx.url is required", file=sys.stderr)

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
        handler = MagicMock(STREAMING=False, instance=None)
        handler.get_data.return_value = {"x": 1}
        handler.source_settings = {"interval": 123}
        args = SimpleNamespace(print=False, dump=False, settings=None)

        interval = sendtoinflux.collect_source_data("hue", args, handler)

        assert interval == 123
        handler.get_data.assert_called_once()
        handler.send_data.assert_called_once()

    def test_run_workers_coerces_invalid_stagger_to_zero(self):
        """run_workers falls back to zero stagger when value is invalid."""
        args = SimpleNamespace(print=False, dump=False, settings=None)
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True

        with (
            patch("sendtoinflux.create_source_worker") as mock_create_source_worker,
            patch("sendtoinflux.spawn_source_thread", return_value=fake_thread),
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_workers([("hue", None), ("zappi", None)], args, "not-an-int")

        mock_create_source_worker.assert_any_call(("hue", None), 0, args, set(), {})
        mock_create_source_worker.assert_any_call(("zappi", None), 0, args, set(), {})

    def test_create_source_worker_stops_permanently_on_config_error(self):
        """create_source_worker adds the source to stopped_sources and returns (no retry) on ConfigError."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.get_data.side_effect = ConfigError("bad config")
        args = SimpleNamespace(print=False, dump=False, settings=None)
        stopped_sources = set()

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep"),
        ):
            worker = sendtoinflux.create_source_worker(("hue", None), 0, args, stopped_sources)
            worker()  # should return normally, not raise or loop forever

        assert stopped_sources == {("hue", None)}
        handler.get_data.assert_called_once()

    def test_run_workers_does_not_restart_stopped_source(self):
        """run_workers does not restart a thread whose source gave up with a ConfigError."""
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
            def fake_create_source_worker(unit, delay, worker_args, stopped_sources, last_activity):
                source, _ = unit
                if source == "zappi":
                    stopped_sources.add(("zappi", None))
                return MagicMock()

            mock_create_source_worker.side_effect = fake_create_source_worker

            with pytest.raises(SystemExit):
                sendtoinflux.run_workers([("hue", None), ("zappi", None)], args, 0)

        # both threads report dead (2 initial spawns), but only "hue" (not in
        # stopped_sources) should have triggered a respawn attempt (3rd spawn)
        assert mock_spawn.call_count == 3


class TestStallDetection:
    """Tests for create_source_worker's last_activity stamping and
    check_for_stalled_sources - the watchdog for a thread that's alive but has
    stopped making any progress (the failure mode a plain thread.is_alive() check
    can't see, since a hung thread never dies)."""

    @staticmethod
    def _stop_after_one_full_iteration():
        """time.sleep side_effect: let the first call through (the scheduling
        sleep at the top of the loop) and raise on the second (the top of the
        *next* iteration), so the worker completes exactly one full cycle."""
        calls = {"n": 0}

        def fake_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise SystemExit(0)

        return fake_sleep

    def test_initial_stamp_uses_the_scheduled_start_not_thread_creation_time(self):
        """A large source_start_delay (a big stagger_seconds, or many sources) can
        itself exceed the stall threshold - the initial stamp must reflect the
        scheduled first-run time (next_update), not the moment the thread was
        created, or the watchdog would flag a source as stalled while it's still
        in its intentional initial delay, before it's ever had a chance to run."""
        handler = MagicMock(STREAMING=False, instance=None)
        last_activity = {}
        large_start_delay = sendtoinflux.STALL_WARNING_SECONDS * 2
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
        ):
            args = SimpleNamespace(print=False, dump=False, settings=None)
            worker = sendtoinflux.create_source_worker(
                ("speedtest", None), large_start_delay, args, set(), last_activity
            )
            with pytest.raises(SystemExit):
                worker()

        assert last_activity[("speedtest", None)] == 1000.0 + large_start_delay

        # Confirm the watchdog agrees: right after thread creation, a source with
        # a delay this large must not be flagged, even though the delay itself
        # exceeds STALL_WARNING_SECONDS.
        stalled_sources = set()
        with patch("sendtoinflux.time.time", return_value=1000.0 + sendtoinflux.STALL_WARNING_SECONDS + 1):
            sendtoinflux.check_for_stalled_sources([("speedtest", None)], set(), last_activity, stalled_sources)
        assert stalled_sources == set()

    def test_successful_cycle_stamps_last_activity_again(self):
        """The thread-start stamp alone isn't the interesting case - a completed
        cycle must advance it further, so a thread that's actually looping (not
        just recently started) keeps proving it's alive. A monotonically
        increasing fake clock avoids hardcoding exactly how many time.time()
        calls happen per iteration - only their relative order matters: the
        pre-loop stamp is the second call ever made (1001.0), so any later
        stamp proves the success branch, not just startup, wrote it."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.source_settings = {"interval": 60}
        last_activity = {}
        clock = itertools.count(1000.0, 1.0)
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", side_effect=lambda: next(clock)),
            patch("sendtoinflux.time.sleep", side_effect=self._stop_after_one_full_iteration()),
        ):
            args = SimpleNamespace(print=False, dump=False, settings=None)
            worker = sendtoinflux.create_source_worker(("hue", None), 0, args, set(), last_activity)
            with pytest.raises(SystemExit):
                worker()

        assert last_activity[("hue", None)] > 1001.0

    def test_failed_cycle_also_stamps_last_activity(self):
        """A retried failure is already visible via its own WARNING - stamping it too
        means the watchdog only fires for a source that's stopped producing *either*
        signal, not one that's actively (and visibly) retrying. Uses a plain retryable
        exception (not ConfigError, which stops the worker permanently and is excluded
        from stall-checking entirely via stopped_sources)."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.get_data.side_effect = SourceConnectionError("connection reset")
        last_activity = {}
        clock = itertools.count(1000.0, 1.0)
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", side_effect=lambda: next(clock)),
            patch("sendtoinflux.time.sleep", side_effect=self._stop_after_one_full_iteration()),
        ):
            args = SimpleNamespace(print=False, dump=False, settings=None)
            worker = sendtoinflux.create_source_worker(("hue", None), 0, args, set(), last_activity)
            with pytest.raises(SystemExit):
                worker()

        assert last_activity[("hue", None)] > 1001.0

    def test_last_activity_none_disables_stamping(self):
        """The default (no last_activity dict) is a no-op - existing callers that
        don't care about stall detection (e.g. other tests exercising retry logic
        in isolation) are unaffected."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.get_data.side_effect = ConfigError("bad config")
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=3000.0),
            patch("sendtoinflux.time.sleep"),
        ):
            args = SimpleNamespace(print=False, dump=False, settings=None)
            worker = sendtoinflux.create_source_worker(("hue", None), 0, args, set())
            worker()  # should not raise despite no last_activity dict provided

    def test_stalled_source_logs_critical_once(self, caplog):
        now = 1000.0 + sendtoinflux.STALL_WARNING_SECONDS + 1
        # Both keys must be work-unit tuples: keyed by a bare string, zappi would look
        # like it had never reported activity at all, so this test would pass while
        # silently not exercising the healthy-worker branch.
        last_activity = {("hue", None): 0.0, ("zappi", None): now - 1}
        stalled_sources = set()
        with (
            caplog.at_level("CRITICAL"),
            patch("sendtoinflux.time.time", return_value=now),
        ):
            sendtoinflux.check_for_stalled_sources(
                [("hue", None), ("zappi", None)], set(), last_activity, stalled_sources
            )
            sendtoinflux.check_for_stalled_sources(
                [("hue", None), ("zappi", None)], set(), last_activity, stalled_sources
            )

        assert stalled_sources == {("hue", None)}
        critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_records) == 1
        assert "hue" in critical_records[0].message
        assert "SIGUSR1" in critical_records[0].message

    def test_recovered_source_clears_the_stalled_flag(self):
        last_activity = {("hue", None): 0.0}
        stalled_sources = {("hue", None)}
        with patch("sendtoinflux.time.time", return_value=1.0):
            sendtoinflux.check_for_stalled_sources([("hue", None)], set(), last_activity, stalled_sources)

        assert stalled_sources == set()

    def test_stopped_sources_are_never_flagged(self):
        last_activity = {("hue", None): 0.0}
        stalled_sources = set()
        with patch("sendtoinflux.time.time", return_value=1_000_000.0):
            sendtoinflux.check_for_stalled_sources([("hue", None)], {("hue", None)}, last_activity, stalled_sources)

        assert stalled_sources == set()

    def test_source_with_no_recorded_activity_is_skipped(self):
        """A source that hasn't completed even its first stagger delay yet has no
        last_activity entry - shouldn't be flagged before it's had a chance to run."""
        stalled_sources = set()
        with patch("sendtoinflux.time.time", return_value=1_000_000.0):
            sendtoinflux.check_for_stalled_sources([("hue", None)], set(), {}, stalled_sources)

        assert stalled_sources == set()

    def test_long_interval_source_is_not_flagged_after_the_flat_threshold(self):
        """A source that legitimately sleeps for its own configured interval between
        cycles (e.g. speedtest's 6-hour default) must not be flagged as stalled just
        because that interval exceeds STALL_WARNING_SECONDS - it would otherwise fire
        on every single cycle of every long-interval source."""
        settings = {"speedtest": {"interval": 21600}}
        last_activity = {("speedtest", None): 0.0}
        stalled_sources = set()

        # Well past STALL_WARNING_SECONDS (900s), but well within one configured
        # interval - a perfectly healthy source sitting in its normal sleep.
        with patch("sendtoinflux.time.time", return_value=3600.0):
            sendtoinflux.check_for_stalled_sources(
                [("speedtest", None)], set(), last_activity, stalled_sources, settings
            )

        assert stalled_sources == set()

    def test_long_interval_source_is_flagged_after_several_missed_intervals(self):
        """The threshold still fires eventually - after STALL_INTERVAL_MULTIPLIER
        missed cycles - so a genuinely stuck long-interval source is still caught,
        just not on every ordinary sleep."""
        settings = {"speedtest": {"interval": 21600}}
        last_activity = {("speedtest", None): 0.0}
        stalled_sources = set()
        past_threshold = 21600 * sendtoinflux.STALL_INTERVAL_MULTIPLIER + 1

        with patch("sendtoinflux.time.time", return_value=past_threshold):
            sendtoinflux.check_for_stalled_sources(
                [("speedtest", None)], set(), last_activity, stalled_sources, settings
            )

        assert stalled_sources == {("speedtest", None)}

    def test_short_interval_source_keeps_the_flat_floor(self):
        """A short-interval source (e.g. 300s) should still use the flat
        STALL_WARNING_SECONDS floor, not a tiny multiple of its own interval -
        STALL_INTERVAL_MULTIPLIER * 300 (900s) happens to equal the floor exactly,
        but this pins the behaviour rather than relying on that coincidence."""
        settings = {"hue": {"interval": 60}}
        last_activity = {("hue", None): 0.0}
        stalled_sources = set()

        with patch("sendtoinflux.time.time", return_value=sendtoinflux.STALL_WARNING_SECONDS - 1):
            sendtoinflux.check_for_stalled_sources([("hue", None)], set(), last_activity, stalled_sources, settings)

        assert stalled_sources == set()

    def test_missing_or_invalid_interval_falls_back_to_the_flat_threshold(self):
        """No settings, no per-source block, or a non-numeric/non-finite interval
        must all degrade to the flat threshold rather than crashing the watchdog
        or (for .inf, which passes a plain '> 0' check) silently raising when the
        CRITICAL message formats an infinite threshold with %d."""
        now = sendtoinflux.STALL_WARNING_SECONDS + 1
        bad_intervals = (
            None,
            {},
            {"hue": {}},
            {"hue": {"interval": "not-a-number"}},
            {"hue": {"interval": True}},
            {"hue": {"interval": float("inf")}},
            {"hue": {"interval": float("nan")}},
            {"hue": {"interval": float("-inf")}},
        )
        for settings in bad_intervals:
            last_activity = {("hue", None): 0.0}
            stalled_sources = set()
            with patch("sendtoinflux.time.time", return_value=now):
                sendtoinflux.check_for_stalled_sources([("hue", None)], set(), last_activity, stalled_sources, settings)
            assert stalled_sources == {("hue", None)}, f"settings={settings!r}"

    def test_infinite_interval_does_not_break_the_critical_log_message(self, caplog):
        """A regression test for the specific failure mode: before the finiteness
        check, an interval of .inf produced an infinite threshold, and formatting
        that with %d in the CRITICAL log call raised OverflowError."""
        settings = {"hue": {"interval": float("inf")}}
        last_activity = {("hue", None): 0.0}
        stalled_sources = set()
        with (
            caplog.at_level("CRITICAL"),
            patch("sendtoinflux.time.time", return_value=sendtoinflux.STALL_WARNING_SECONDS + 1),
        ):
            sendtoinflux.check_for_stalled_sources([("hue", None)], set(), last_activity, stalled_sources, settings)

        assert stalled_sources == {("hue", None)}
        assert any(r.levelname == "CRITICAL" for r in caplog.records)


class TestSendHeartbeat:
    """Tests for send_heartbeat."""

    def test_no_op_when_handler_is_none(self):
        """send_heartbeat does nothing when no handler has been constructed yet."""
        sendtoinflux.send_heartbeat(None, "hue", ok=True, consecutive_failures=0)

    def test_sends_ok_status_and_restores_header(self):
        """send_heartbeat writes ok=1 and restores the handler's original influx_header."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.influx_header = "hue,host=test "
        with patch("sendtoinflux.time.time", return_value=1700000000.0):
            sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)
        handler.send_data.assert_called_once_with(
            data={"ok": 1, "consecutive_failures": 0}, timestamp=1700000000, use_buffer=False
        )
        assert handler.influx_header == "hue,host=test "

    def test_sends_failure_status_with_count(self):
        """send_heartbeat writes ok=0 with the current consecutive failure count."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.influx_header = "hue "
        with patch("sendtoinflux.time.time", return_value=1700000000.0):
            sendtoinflux.send_heartbeat(handler, "hue", ok=False, consecutive_failures=3)
        handler.send_data.assert_called_once_with(
            data={"ok": 0, "consecutive_failures": 3}, timestamp=1700000000, use_buffer=False
        )

    def test_uses_collector_status_measurement_while_sending(self):
        """send_heartbeat temporarily swaps in the collector_status header for the write."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.influx_header = "hue "
        captured = {}
        handler.send_data.side_effect = lambda data=None, timestamp=None, use_buffer=True: captured.update(
            header=handler.influx_header
        )

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
        handler = MagicMock(STREAMING=False, instance=None)
        handler.influx_header = "hue "
        handler.send_data.side_effect = Exception("network error")

        sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)  # should not raise

        assert handler.influx_header == "hue "


class TestMaybeSendHeartbeat:
    """Tests for maybe_send_heartbeat."""

    def test_sends_when_not_in_print_mode(self):
        """maybe_send_heartbeat delegates to send_heartbeat when not in --print mode."""
        handler = MagicMock(STREAMING=False, instance=None)
        args = SimpleNamespace(print=False, dump=False)
        with patch("sendtoinflux.send_heartbeat") as mock_heartbeat:
            sendtoinflux.maybe_send_heartbeat(args, handler, "hue", ok=True, consecutive_failures=0)
        mock_heartbeat.assert_called_once_with(handler, "hue", ok=True, consecutive_failures=0)

    def test_skips_in_print_mode(self):
        """maybe_send_heartbeat does not touch InfluxDB in --print mode."""
        handler = MagicMock(STREAMING=False, instance=None)
        args = SimpleNamespace(print=True, dump=False)
        with patch("sendtoinflux.send_heartbeat") as mock_heartbeat:
            sendtoinflux.maybe_send_heartbeat(args, handler, "hue", ok=True, consecutive_failures=0)
        mock_heartbeat.assert_not_called()


class TestRunSingleSourceRetry:
    """Tests for retry/backoff behaviour in run_one_worker."""

    def _make_handler(self):
        handler = MagicMock(STREAMING=False, instance=None)
        handler.source_settings = {"interval": 60}
        return handler

    def test_exception_is_caught_and_loop_continues(self):
        """run_one_worker catches Exception, resets handler, and retries."""
        handler = self._make_handler()
        handler.get_data.side_effect = [Exception("network error"), Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_one_worker(("hue", None), SimpleNamespace(print=False, dump=False, settings=None))

        assert handler.get_data.call_count >= 1

    def test_config_error_exits_immediately_without_retry(self):
        """run_one_worker exits with code 1 on ConfigError instead of retrying."""
        handler = self._make_handler()
        handler.get_data.side_effect = ConfigError("bad config")

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
        ):
            with pytest.raises(SystemExit) as exc_info:
                sendtoinflux.run_one_worker(("hue", None), SimpleNamespace(print=False, dump=False, settings=None))

        assert exc_info.value.code == 1
        handler.get_data.assert_called_once()

    def test_handler_is_recreated_after_failure(self):
        """run_one_worker calls get_class again after a failure resets the handler."""
        handler = self._make_handler()
        handler.get_data.side_effect = [Exception("fail"), Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler) as mock_get_class,
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_one_worker(("hue", None), SimpleNamespace(print=False, dump=False, settings=None))

        # Called once before the loop, then again after the failure reset
        assert mock_get_class.call_count == 2

    def test_failure_count_increments_backoff(self):
        """run_one_worker passes increasing failure_count to get_backoff_delay."""
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
                sendtoinflux.run_one_worker(("hue", None), SimpleNamespace(print=False, dump=False, settings=None))

        assert len(delays) >= 2
        assert delays == list(range(1, len(delays) + 1))

    def test_sends_heartbeat_on_success(self):
        """run_one_worker sends an ok=1 heartbeat after a successful cycle."""
        handler = self._make_handler()
        handler.get_data.side_effect = [{"x": 1}, Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_one_worker(("hue", None), SimpleNamespace(print=False, dump=False, settings=None))

        mock_heartbeat.assert_any_call(handler, "hue", ok=True, consecutive_failures=0)

    def test_sends_heartbeat_on_failure_with_failure_count(self):
        """run_one_worker sends an ok=0 heartbeat with the failure count after a failed cycle."""
        handler = self._make_handler()
        handler.get_data.side_effect = [Exception("network error"), Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_one_worker(("hue", None), SimpleNamespace(print=False, dump=False, settings=None))

        mock_heartbeat.assert_any_call(handler, "hue", ok=False, consecutive_failures=1)

    def test_skips_heartbeat_in_print_mode(self):
        """run_one_worker does not write heartbeats in --print mode."""
        handler = self._make_handler()
        handler.get_data.side_effect = [{"x": 1}, Exception("break")]

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_one_worker(("hue", None), SimpleNamespace(print=True, dump=False, settings=None))

        mock_heartbeat.assert_not_called()


class TestCreateSourceWorkerHeartbeat:
    """Tests for heartbeat wiring in the multi-source worker."""

    def test_worker_sends_heartbeat_on_success(self):
        """The multi-source worker sends an ok=1 heartbeat after a successful cycle."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.source_settings = {"interval": 60}
        handler.get_data.return_value = {"x": 1}
        args = SimpleNamespace(print=False, dump=False, settings=None)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, KeyboardInterrupt()]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            worker = sendtoinflux.create_source_worker(("hue", None), 0, args, set())
            with pytest.raises(KeyboardInterrupt):
                worker()

        mock_heartbeat.assert_called_once_with(handler, "hue", ok=True, consecutive_failures=0)

    def test_worker_sends_heartbeat_on_failure(self):
        """The multi-source worker sends an ok=0 heartbeat with the failure count on error."""
        handler = MagicMock(STREAMING=False, instance=None)
        handler.source_settings = {"interval": 60}
        handler.get_data.side_effect = Exception("network error")
        args = SimpleNamespace(print=False, dump=False, settings=None)

        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, KeyboardInterrupt()]),
            patch("sendtoinflux.send_heartbeat") as mock_heartbeat,
        ):
            worker = sendtoinflux.create_source_worker(("hue", None), 0, args, set())
            with pytest.raises(KeyboardInterrupt):
                worker()

        mock_heartbeat.assert_any_call(handler, "hue", ok=False, consecutive_failures=1)

    def test_worker_skips_heartbeat_in_print_mode(self):
        """The multi-source worker does not write heartbeats in --print mode."""
        handler = MagicMock(STREAMING=False, instance=None)
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
            worker = sendtoinflux.create_source_worker(("hue", None), 0, args, set())
            with pytest.raises(KeyboardInterrupt):
                worker()

        mock_heartbeat.assert_not_called()


class TestConfigureLogging:
    """Tests for configure_logging."""

    def _remove_handlers(self, root, added):
        for h in added:
            root.removeHandler(h)
            h.close()

    def test_adds_stderr_stream_handler(self):
        """configure_logging adds a StreamHandler writing to *stderr*.

        stdout carries the program's data - --dump/--print JSON, --check-config's verdict -
        so a caller can parse it. Logging there made a partial-failure dump unparseable,
        because the failure it reports lands in the middle of the payload it still produces.
        """
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
            assert stream_handlers[0].stream is sys.stderr
            assert stream_handlers[0].stream is not sys.stdout
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


class TestMaybeStartMcpServer:
    """Tests for the MCP server startup gate in the entry point."""

    ENABLED_SETTINGS = {
        "mcp": {
            "public_url": "https://mcp.example.org",
            "user": "gavin",
            "password": "hunter22",
        },
    }

    def test_disabled_settings_do_not_start_a_server(self):
        args = SimpleNamespace(print=False, dump=False, settings=None)
        assert sendtoinflux.maybe_start_mcp_server({}, args) is None

    def test_print_mode_never_starts_a_server(self):
        args = SimpleNamespace(print=True, dump=False, settings=None)
        with patch("toinflux.mcpserver.start_mcp_server_thread") as start:
            assert sendtoinflux.maybe_start_mcp_server(self.ENABLED_SETTINGS, args) is None
        start.assert_not_called()

    def test_dump_mode_never_starts_a_server(self):
        args = SimpleNamespace(print=False, dump=True, settings=None)
        with patch("toinflux.mcpserver.start_mcp_server_thread") as start:
            assert sendtoinflux.maybe_start_mcp_server(self.ENABLED_SETTINGS, args) is None
        start.assert_not_called()

    def test_enabled_settings_start_the_server_thread(self):
        args = SimpleNamespace(print=False, dump=False, settings="/etc/send-to-influx/settings.yaml")
        with patch("toinflux.mcpserver.start_mcp_server_thread") as start:
            result = sendtoinflux.maybe_start_mcp_server(self.ENABLED_SETTINGS, args)
        start.assert_called_once_with(self.ENABLED_SETTINGS, "/etc/send-to-influx/settings.yaml")
        assert result is start.return_value


class TestStreamSink:
    """Tests for _StreamSink - the bridge from the streaming transport's callbacks to the
    collector's write, heartbeat and stall-activity behaviour (slice 2)."""

    def _sink(self, print_mode=False, on_activity=None):
        handler = MagicMock(STREAMING=True)
        handler.source_settings = {"interval": 300}
        handler.STREAM_TOPIC_FILTER = "nuki/+/+"
        args = SimpleNamespace(print=print_mode, dump=False, settings=None)
        return sendtoinflux._StreamSink("nuki", args, handler, on_activity), handler, args

    # --- on_message (the immediate interrupt path) ---

    def test_on_message_writes_decoded_point_and_stamps_activity(self):
        """A decoded message is written straight away and stamps stall-activity."""
        activity = []
        sink, handler, _ = self._sink(on_activity=lambda: activity.append(1))
        handler.decode_stream_message.return_value = {"x": 1}
        sink.on_message("nuki/A/state", "3")
        handler.send_data.assert_called_once_with(data={"x": 1})
        assert activity == [1]

    def test_on_message_ignores_a_message_that_decodes_to_nothing(self):
        """A control/metadata topic (decode returns None) writes nothing and doesn't stamp."""
        activity = []
        sink, handler, _ = self._sink(on_activity=lambda: activity.append(1))
        handler.decode_stream_message.return_value = None
        sink.on_message("nuki/A/name", "Front Door")
        handler.send_data.assert_not_called()
        assert activity == []

    def test_on_message_prints_instead_of_sending_in_print_mode(self):
        """--print routes the immediate point to stdout, never to InfluxDB."""
        sink, handler, _ = self._sink(print_mode=True)
        handler.decode_stream_message.return_value = {"x": 1}
        with patch("sendtoinflux.print_source_data") as mock_print:
            sink.on_message("nuki/A/state", "3")
        mock_print.assert_called_once_with("nuki", {"x": 1})
        handler.send_data.assert_not_called()

    def test_on_message_swallows_influx_write_error(self):
        """A failed InfluxDB write is buffered by send_data, not a stream failure - it must
        not propagate out of the network callback (activity is already stamped)."""
        activity = []
        sink, handler, _ = self._sink(on_activity=lambda: activity.append(1))
        handler.decode_stream_message.return_value = {"x": 1}
        handler.send_data.side_effect = InfluxWriteError("influx down")
        sink.on_message("nuki/A/state", "3")  # must not raise
        assert activity == [1]

    # --- periodic (the safety-net probe + heartbeat) ---

    def test_periodic_probe_success_reports_healthy(self):
        """A successful probe writes the full-state point and reports ok=1, failures=0."""
        activity = []
        sink, handler, args = self._sink(on_activity=lambda: activity.append(1))
        handler.get_data.return_value = {"x": 1}
        with patch("sendtoinflux.maybe_send_heartbeat") as heartbeat:
            sink.periodic()
        handler.send_data.assert_called_once_with(data={"x": 1})
        heartbeat.assert_called_once_with(args, handler, "nuki", ok=True, consecutive_failures=0)
        assert activity == [1]

    def test_periodic_probe_failure_without_messages_reports_unhealthy(self, caplog):
        """A failed probe with no messages since the last tick is the correlated outage we
        must surface: ok=0, with consecutive_failures climbing, and a WARNING logged."""
        sink, handler, _ = self._sink()
        handler.get_data.side_effect = SourceConnectionError("broker down")
        with patch("sendtoinflux.maybe_send_heartbeat") as heartbeat, caplog.at_level(logging.WARNING):
            sink.periodic()
            sink.periodic()
        assert [c.kwargs["ok"] for c in heartbeat.call_args_list] == [False, False]
        assert [c.kwargs["consecutive_failures"] for c in heartbeat.call_args_list] == [1, 2]
        assert "Health probe for streaming source 'nuki' failed" in caplog.text

    def test_periodic_message_since_tick_overrides_a_failed_probe(self):
        """A demonstrably-working stream (a message arrived) is healthy even if the one-off
        probe fails - the message is a sign of life, so ok stays True."""
        sink, handler, args = self._sink()
        handler.decode_stream_message.return_value = {"x": 1}
        handler.get_data.side_effect = SourceConnectionError("broker down")
        sink.on_message("nuki/A/state", "3")  # a message this interval
        with patch("sendtoinflux.maybe_send_heartbeat") as heartbeat:
            sink.periodic()
        heartbeat.assert_called_once_with(args, handler, "nuki", ok=True, consecutive_failures=0)

    def test_periodic_clears_the_message_flag_each_tick(self):
        """The message-since-tick flag covers only the interval it arrived in: the next
        tick with a still-failing probe and no new message reports unhealthy."""
        sink, handler, _ = self._sink()
        handler.decode_stream_message.return_value = {"x": 1}
        handler.get_data.side_effect = SourceConnectionError("broker down")
        sink.on_message("nuki/A/state", "3")
        with patch("sendtoinflux.maybe_send_heartbeat") as heartbeat:
            sink.periodic()  # message covers this tick
            sink.periodic()  # no new message, probe still failing
        assert [c.kwargs["ok"] for c in heartbeat.call_args_list] == [True, False]
        assert [c.kwargs["consecutive_failures"] for c in heartbeat.call_args_list] == [0, 1]

    def test_periodic_recovery_resets_the_failure_streak(self):
        """Once the probe recovers, the consecutive-failure streak resets to zero."""
        sink, handler, _ = self._sink()
        handler.get_data.side_effect = [SourceConnectionError("x"), SourceConnectionError("y"), {"ok": 1}]
        with patch("sendtoinflux.maybe_send_heartbeat") as heartbeat:
            sink.periodic()
            sink.periodic()
            sink.periodic()
        assert [c.kwargs["ok"] for c in heartbeat.call_args_list] == [False, False, True]
        assert [c.kwargs["consecutive_failures"] for c in heartbeat.call_args_list] == [1, 2, 0]

    def test_periodic_influx_write_error_still_counts_the_probe_as_reachable(self):
        """A failed InfluxDB write isn't a probe failure - the source was reachable, so the
        heartbeat stays healthy and the point is left to the buffer."""
        sink, handler, args = self._sink()
        handler.get_data.return_value = {"x": 1}
        handler.send_data.side_effect = InfluxWriteError("influx down")
        with patch("sendtoinflux.maybe_send_heartbeat") as heartbeat:
            sink.periodic()  # must not raise
        heartbeat.assert_called_once_with(args, handler, "nuki", ok=True, consecutive_failures=0)


class TestShouldStream:
    """_should_stream gates the streaming path on both STREAMING and a topic filter."""

    def test_non_streaming_handler_polls(self):
        assert sendtoinflux._should_stream(MagicMock(STREAMING=False)) is False

    def test_streaming_transport_without_filter_polls(self):
        """A STREAMING transport not yet wired to a concrete source (STREAM_TOPIC_FILTER
        still None) polls, so it can't be stranded subscribing to a None filter."""
        handler = MagicMock(STREAMING=True, STREAM_TOPIC_FILTER=None)
        assert sendtoinflux._should_stream(handler) is False

    def test_streaming_transport_with_filter_streams(self):
        handler = MagicMock(STREAMING=True, STREAM_TOPIC_FILTER="nuki/+/+")
        assert sendtoinflux._should_stream(handler) is True

    def test_non_mqtt_handler_without_the_attribute_polls(self):
        """A plain HTTP handler has no STREAM_TOPIC_FILTER attribute at all - the getattr
        fallback keeps it on the poll path rather than raising."""
        handler = MagicMock(STREAMING=False, spec=["STREAMING"])
        assert sendtoinflux._should_stream(handler) is False


class TestStreamSourceData:
    """stream_source_data hands the transport the source's topic filter, both sink
    callbacks, the interval and the stop event (slice 2)."""

    def test_wires_the_transport_with_the_sink_callbacks(self):
        handler = MagicMock(STREAMING=True)
        handler.source_settings = {"interval": 300}
        handler.STREAM_TOPIC_FILTER = "nuki/+/+"
        args = SimpleNamespace(print=False, dump=False, settings=None)
        should_stop = threading.Event()
        sendtoinflux.stream_source_data("nuki", args, handler, should_stop)
        handler.stream_mqtt_messages.assert_called_once()
        topic, on_message, periodic, interval, stop = handler.stream_mqtt_messages.call_args.args
        assert topic == "nuki/+/+"
        assert callable(on_message) and callable(periodic)
        assert interval == 300
        assert stop is should_stop


class TestWorkerStreamingBranch:
    """Both worker paths run the blocking stream loop for a STREAMING handler instead of
    the poll-then-sleep cycle (slice 2)."""

    def _streaming_handler(self):
        handler = MagicMock(STREAMING=True)
        handler.source_settings = {"interval": 300}
        handler.STREAM_TOPIC_FILTER = "nuki/+/+"  # a wired-up streaming source
        return handler

    def test_streaming_transport_without_a_filter_keeps_polling(self):
        """A STREAMING transport whose source hasn't set STREAM_TOPIC_FILTER yet (e.g. Nuki
        before slice 3) must keep polling, not enter the stream path and subscribe to None -
        that would be a retry-forever regression vs the previous polling collector."""
        handler = MagicMock(STREAMING=True)
        handler.source_settings = {"interval": 300}
        handler.STREAM_TOPIC_FILTER = None
        args = SimpleNamespace(print=False, dump=False, settings=None)
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.stream_source_data") as stream,
            patch("sendtoinflux.collect_source_data", return_value=300) as collect,
            patch("sendtoinflux.maybe_send_heartbeat"),
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
        ):
            worker = sendtoinflux.create_source_worker(("nuki", None), 0, args, set())
            with pytest.raises(SystemExit):
                worker()
        stream.assert_not_called()
        collect.assert_called()

    def test_create_source_worker_streams_and_returns(self):
        """A streaming handler takes stream_source_data and returns, never polling."""
        handler = self._streaming_handler()
        args = SimpleNamespace(print=False, dump=False, settings=None)
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.stream_source_data") as stream,
            patch("sendtoinflux.collect_source_data") as collect,
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep"),
        ):
            worker = sendtoinflux.create_source_worker(("nuki", None), 0, args, set(), {})
            worker()  # returns cleanly, no infinite poll loop
        stream.assert_called_once()
        assert stream.call_args.args[:4] == ("nuki", args, handler, sendtoinflux.SHUTDOWN)
        collect.assert_not_called()

    def test_create_source_worker_streaming_on_activity_stamps_last_activity(self):
        """The on_activity callback handed to the stream stamps the stall watchdog's dict."""
        handler = self._streaming_handler()
        args = SimpleNamespace(print=False, dump=False, settings=None)
        last_activity = {}
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.stream_source_data") as stream,
            patch("sendtoinflux.time.time", return_value=1234.0),
            patch("sendtoinflux.time.sleep"),
        ):
            worker = sendtoinflux.create_source_worker(("nuki", None), 0, args, set(), last_activity)
            worker()
            on_activity = stream.call_args.kwargs["on_activity"]
            last_activity.clear()  # drop the initial scheduled-start stamp
            on_activity()
            assert last_activity == {("nuki", None): 1234.0}

    def test_streaming_startup_failure_is_retried_with_backoff(self):
        """A SourceConnectionError from the stream (broker down at startup) is caught by the
        worker's existing backoff branch and reported unhealthy, exactly like a failed poll."""
        handler = self._streaming_handler()
        args = SimpleNamespace(print=False, dump=False, settings=None)
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.stream_source_data", side_effect=SourceConnectionError("broker down")),
            patch("sendtoinflux.maybe_send_heartbeat") as heartbeat,
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
        ):
            worker = sendtoinflux.create_source_worker(("nuki", None), 0, args, set())
            with pytest.raises(SystemExit):
                worker()
        assert any(c.kwargs.get("ok") is False for c in heartbeat.call_args_list)

    def test_run_one_worker_streams_and_returns(self):
        """The single-source path also runs the stream loop for a streaming handler."""
        handler = self._streaming_handler()
        args = SimpleNamespace(print=False, dump=False, settings=None)
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.stream_source_data") as stream,
            patch("sendtoinflux.collect_source_data") as collect,
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep"),
        ):
            sendtoinflux.run_one_worker(("nuki", None), args)
        stream.assert_called_once_with("nuki", args, handler, sendtoinflux.SHUTDOWN)
        collect.assert_not_called()

    def test_run_one_worker_streaming_startup_failure_backs_off(self):
        """A SourceConnectionError from the stream in single-source mode is caught by the
        loop's backoff branch and reported unhealthy (ok=0), same as a failed poll, then
        retried - not left unhandled."""
        handler = self._streaming_handler()
        args = SimpleNamespace(print=False, dump=False, settings=None)
        with (
            patch("sendtoinflux.toinflux.get_class", return_value=handler),
            patch("sendtoinflux.stream_source_data", side_effect=SourceConnectionError("broker down")),
            patch("sendtoinflux.maybe_send_heartbeat") as heartbeat,
            patch("sendtoinflux.time.time", return_value=1000.0),
            patch("sendtoinflux.time.sleep", side_effect=[None, SystemExit(0)]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.run_one_worker(("nuki", None), args)
        assert any(c.kwargs.get("ok") is False for c in heartbeat.call_args_list)


class TestMultiBridgeWorkers:
    """A source with several instances runs one worker per instance."""

    HUE = {
        "db": "hue_db",
        "interval": 300,
        "host": "a.example.com",
        "user": "tok-a",
        "host3": "b.example.com",
        "user3": "tok-b",
    }

    def _settings(self, **extra):
        return {"influx": {"url": "http://x", "token": "t", "org": "o"}, "hue": dict(self.HUE), **extra}

    def test_each_bridge_gets_its_own_worker_staggered(self):
        """One thread per bridge, and the stagger runs across the expanded list - so two
        bridges are spread apart just as two separate sources are, rather than both
        hitting their bridges at the same instant."""
        settings = self._settings(
            sources=["hue", "speedtest"], speedtest={"db": "s", "interval": 60}, stagger_seconds=5
        )
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings", return_value=settings),
            patch("sendtoinflux.create_source_worker") as mock_worker,
            patch("sendtoinflux.spawn_source_thread"),
            patch("sendtoinflux.time.sleep", side_effect=SystemExit(0)),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
        units_and_delays = [(c[0][0], c[0][1]) for c in mock_worker.call_args_list]
        assert units_and_delays == [
            (("hue", "a.example.com"), 0),
            (("hue", "b.example.com"), 5),
            (("speedtest", None), 10),
        ]

    def test_a_single_bridge_still_runs_on_the_main_thread(self):
        """One worker keeps the main-thread path, which is what lets a streaming source
        shut down cleanly on a signal."""
        settings = self._settings(
            sources=["hue"], hue={"db": "d", "interval": 300, "host": "only.example.com", "user": "tok"}
        )
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings", return_value=settings),
            patch("sendtoinflux.run_one_worker") as mock_one,
            patch("sendtoinflux.run_workers") as mock_many,
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
        ):
            sendtoinflux.main()
        mock_one.assert_called_once()
        assert mock_one.call_args[0][0] == ("hue", "only.example.com")
        mock_many.assert_not_called()

    def test_source_flag_collects_every_bridge(self):
        """--source hue must collect all bridges, not just the first."""
        settings = self._settings(sources=["hue"])
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings", return_value=settings),
            patch("sendtoinflux.run_workers") as mock_many,
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-s", "hue"]),
        ):
            sendtoinflux.main()
        assert mock_many.call_args[0][0] == [("hue", "a.example.com"), ("hue", "b.example.com")]

    def test_nothing_to_collect_exits_rather_than_idling(self):
        """Every requested source expanding to nothing must say so and stop - not spin a
        supervisor over an empty list, and not look healthy while collecting nothing."""
        settings = self._settings(
            sources=["hue"], hue={"db": "d", "interval": 300, "host": "a.example.com", "user": "your_hue_user"}
        )
        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings", return_value=settings),
            patch("sendtoinflux.sys.argv", ["sendtoinflux"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
        mock_exit.assert_called_once_with(1)

    def test_heartbeat_identifies_the_bridge(self):
        """Per-bridge health, rather than several workers overwriting one another's
        ok/consecutive_failures on the same series at second precision."""
        captured = {}
        handler = MagicMock(STREAMING=False, instance="b.example.com")
        handler.influx_header = "hue,host=b.example.com "
        handler.send_data.side_effect = lambda **kw: captured.update(header=handler.influx_header)
        sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)
        assert captured["header"] == "collector_status,source=hue,host=b.example.com "

    def test_heartbeat_escapes_the_instance(self):
        """The heartbeat header is written verbatim, so an instance carrying a
        line-protocol special must be escaped or the point is silently corrupt."""
        captured = {}
        handler = MagicMock(STREAMING=False, instance="odd host,x")
        handler.influx_header = "hue "
        handler.send_data.side_effect = lambda **kw: captured.update(header=handler.influx_header)
        sendtoinflux.send_heartbeat(handler, "hue", ok=True, consecutive_failures=0)
        assert captured["header"] == "collector_status,source=hue,host=odd\\ host\\,x "

    def test_dump_emits_every_bridge_keyed_by_host(self):
        """--dump covers all bridges, keyed by host even for one bridge, so nothing
        reading the output depends on the operator's bridge count."""
        settings = self._settings(sources=["hue"])
        handlers = {}

        def fake_get_class(source, settings_file=None, instance=None):
            handler = MagicMock(STREAMING=False, instance=instance)
            handler.get_data.return_value = {"lamp": 1 if instance == "a.example.com" else 2}
            handlers[instance] = handler
            return handler

        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings", return_value=settings),
            patch("sendtoinflux.toinflux.get_class", side_effect=fake_get_class),
            patch("sendtoinflux.print") as mock_print,
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-s", "hue", "-d"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(0)),
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
        dumped = json.loads(mock_print.call_args[0][0])
        assert dumped == {"a.example.com": {"lamp": 1}, "b.example.com": {"lamp": 2}}

    def test_dump_reports_a_failing_bridge_but_still_emits_the_others(self):
        """A partial result WITH its failure status, rather than silence - exit 2."""
        settings = self._settings(sources=["hue"])

        def fake_get_class(source, settings_file=None, instance=None):
            handler = MagicMock(STREAMING=False, instance=instance)
            if instance == "b.example.com":
                handler.get_data.side_effect = SourceConnectionError("bridge down")
            else:
                handler.get_data.return_value = {"lamp": 1}
            return handler

        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings", return_value=settings),
            patch("sendtoinflux.toinflux.get_class", side_effect=fake_get_class),
            patch("sendtoinflux.print") as mock_print,
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-s", "hue", "-d"]),
            patch("sendtoinflux.sys.exit", side_effect=SystemExit(2)) as mock_exit,
        ):
            with pytest.raises(SystemExit):
                sendtoinflux.main()
        assert json.loads(mock_print.call_args[0][0]) == {"a.example.com": {"lamp": 1}}
        mock_exit.assert_called_once_with(2)

    def test_stall_watchdog_names_the_stalled_bridge(self):
        """Two workers on one source name must be distinguishable when one stalls."""
        stalled = set()
        last_activity = {("hue", "a.example.com"): 0.0, ("hue", "b.example.com"): time.time()}
        units = [("hue", "a.example.com"), ("hue", "b.example.com")]
        with patch("sendtoinflux.logging.critical") as mock_critical:
            sendtoinflux.check_for_stalled_sources(units, set(), last_activity, stalled, self._settings())
        assert stalled == {("hue", "a.example.com")}
        assert mock_critical.call_args[0][1] == "hue@a.example.com"


class TestOutputStreams:
    """Which stream carries what.

    stdout is the program's *data* - --dump/--print JSON and --check-config's verdict - and a
    caller has to be able to parse it. Diagnostics go to stderr. Before this split, a dump
    that partially succeeded was unparseable: the failure it reported landed in the middle of
    the payload it still produced, so `--dump | jq` failed exactly when the output mattered.
    """

    @staticmethod
    def _settings(sources):
        return {
            "sources": sources,
            "influx": {"url": "http://x", "user": "u", "password": "p"},
            "hue": {
                "db": "hue_db",
                "interval": 300,
                "host": "a.example.com",
                "user": "t1",
                "host2": "b.example.com",
                "user2": "t2",
            },
        }

    @pytest.fixture(autouse=True)
    def _restore_root_logger(self):
        """Put the root logger back exactly as it was.

        These tests deliberately let real logging happen - that is the point, since patching it
        would hide which stream it reaches - so they install handlers and change the level for
        real, and one of them does so indirectly through main(). Without this, the level and a
        stderr handler leak into every later test in the session, which makes log-capture
        assertions elsewhere depend on execution order. Measured before fixing: root went from
        WARNING/0 handlers to INFO/1. Autouse so a test added here later inherits it rather than
        having to remember.
        """
        root = logging.getLogger()
        level, handlers = root.level, list(root.handlers)
        try:
            yield
        finally:
            for handler in [h for h in root.handlers if h not in handlers]:
                root.removeHandler(handler)
                handler.close()
            root.setLevel(level)

    @pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
    def test_every_level_goes_to_stderr(self, level, capsys):
        """Every level, not just errors - splitting diagnostics across two streams by
        severity would interleave them unpredictably for anyone capturing either."""
        from toinflux.general import configure_logging

        configure_logging(loglevel="DEBUG")
        getattr(logging, level)("marker-%s", level)
        captured = capsys.readouterr()
        assert f"marker-{level}" in captured.err
        assert f"marker-{level}" not in captured.out

    def test_check_config_verdict_on_stdout_failure_on_stderr(self, capsys, tmp_path):
        """The verdict answers the question asked, so it belongs on stdout; the failure is
        diagnostics. Exit codes unchanged either way."""
        good = tmp_path / "good.yaml"
        good.write_text(
            "sources: [hue]\n"
            "influx: {url: 'http://x', db: home, user: u, password: p}\n"
            "hue: {host: h, user: t, interval: 5, db: home}\n"
        )
        good.chmod(0o600)
        with patch("sendtoinflux.sys.argv", ["sendtoinflux", "--check-config", "--settings", str(good)]):
            with pytest.raises(SystemExit) as excinfo:
                sendtoinflux.main()
        assert excinfo.value.code == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "Configuration OK"

        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "sources: [hue]\ninflux: {db: home, user: u, password: p}\nhue: {host: h, user: t, interval: 5, db: home}\n"
        )
        bad.chmod(0o600)
        with patch("sendtoinflux.sys.argv", ["sendtoinflux", "--check-config", "--settings", str(bad)]):
            with pytest.raises(SystemExit) as excinfo:
                sendtoinflux.main()
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Configuration error" in captured.err
        assert captured.out.strip() == ""

    def test_a_partially_failing_dump_leaves_stdout_parseable(self, capsys):
        """The regression this split exists for.

        One bridge answers, one does not: the payload is still emitted, the failure is still
        reported, the exit code is still 2 - and stdout on its own is valid JSON, so
        `--dump | jq` works. Deliberately does *not* patch print or logging: the whole point
        is which real stream each one reaches.
        """
        settings = self._settings(["hue"])

        def fake_get_class(source, settings_file=None, instance=None):
            handler = MagicMock(STREAMING=False, instance=instance)
            if instance == "b.example.com":
                handler.get_data.side_effect = SourceConnectionError("bridge down")
            else:
                handler.get_data.return_value = {"lamp": 1}
            return handler

        with (
            patch("sendtoinflux.signal.signal"),
            patch("sendtoinflux.toinflux.load_settings", return_value=settings),
            patch("sendtoinflux.toinflux.get_class", side_effect=fake_get_class),
            patch("sendtoinflux.sys.argv", ["sendtoinflux", "-s", "hue", "-d"]),
        ):
            with pytest.raises(SystemExit) as excinfo:
                sendtoinflux.main()
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        # stdout parses on its own - the assertion that fails if diagnostics return to it.
        assert json.loads(captured.out) == {"a.example.com": {"lamp": 1}}
        # ...and the failure was still reported, just elsewhere.
        assert "bridge down" in captured.err
