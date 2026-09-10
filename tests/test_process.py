"""Unit tests for toinflux.process (run_command and its guarantees).

These run real child processes rather than mocking ``subprocess``. The whole point of
this module is what happens at the boundary - an environment that is not inherited, a
pipe that fills, a process that will not exit - and a mocked Popen asserts only that we
called ourselves the way we expected to.

The child is always ``sys.executable -c ...``, so the tests need no fixture binary and
behave the same on every platform CI runs on.
"""

import os
import subprocess
import sys
import threading
import time
from unittest.mock import patch
import pytest
from toinflux import process as process_module
from toinflux.exceptions import ConfigError
from toinflux.process import (
    INHERITED_ENV_KEYS,
    CommandResult,
    ProcessError,
    run_command,
)


def python_c(script):
    """Build an argv that runs a snippet under this interpreter.

    Args:
        script (str): the Python source to run

    Returns:
        list: an argv suitable for run_command
    """
    return [sys.executable, "-c", script]


def _platform_injected_env():
    """Variables the operating system puts in a child regardless of what we pass.

    macOS adds ``__CF_USER_TEXT_ENCODING`` (and ``LC_CTYPE``) to every process it
    spawns, so an allow-list assertion that treats any unexpected name as a leak from
    ``run_command`` fails there for a reason that has nothing to do with this project.
    Measured rather than hardcoded: spawning with an explicitly empty environment shows
    exactly what the platform contributes, which keeps the guard strict on Linux, where
    the answer is nothing at all.

    Returns:
        set: variable names present in a child given no environment
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import os; print('\\n'.join(os.environ))"],
        env={},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return set(probe.stdout.split())


class TestSuccessfulCommands:
    def test_captures_stdout_and_reports_success(self):
        result = run_command(python_c("print('hello')"), timeout=30)
        assert result.ok
        assert result.returncode == 0
        assert result.stdout.strip() == b"hello"
        assert result.stderr == b""

    def test_argv_records_the_resolved_executable(self):
        result = run_command(python_c("pass"), timeout=30)
        assert os.path.isabs(result.argv[0])
        assert result.argv[1:] == ["-c", "pass"]

    def test_stdin_bytes_reach_the_child(self):
        # The credential path passes a secret this way precisely so it never appears in
        # argv, where it would be world-readable in /proc and in any error message.
        result = run_command(
            python_c("import sys; sys.stdout.write(sys.stdin.read().upper())"),
            timeout=30,
            stdin_bytes=b"a secret",
        )
        assert result.stdout == b"A SECRET"

    def test_stdin_is_empty_rather_than_inherited_when_none_is_given(self):
        result = run_command(python_c("import sys; print(len(sys.stdin.read()))"), timeout=30)
        assert result.stdout.strip() == b"0"


class TestFailingCommands:
    def test_a_non_zero_exit_is_returned_not_raised(self):
        # The distinction this module rests on: a command that ran and failed still
        # produced the output explaining why, so throwing it away to raise would
        # destroy what the caller needs.
        result = run_command(python_c("import sys; sys.exit(3)"), timeout=30)
        assert isinstance(result, CommandResult)
        assert not result.ok
        assert result.returncode == 3

    def test_stderr_is_captured_alongside_a_failure(self):
        result = run_command(
            python_c("import sys; sys.stderr.write('it went wrong'); sys.exit(1)"),
            timeout=30,
        )
        assert not result.ok
        assert result.stderr_text == "it went wrong"


class TestExecutableResolution:
    def test_an_unknown_command_raises_config_error_naming_it(self):
        with pytest.raises(ConfigError) as exc:
            run_command(["definitely-not-a-real-binary-zzz"], timeout=30)
        message = str(exc.value)
        # An operator reading this in the journal has to learn both what was missing and
        # where it was looked for, or the next step is guesswork.
        assert "definitely-not-a-real-binary-zzz" in message
        assert "PATH" in message

    def test_a_path_that_is_not_executable_raises_config_error(self, tmp_path):
        not_a_binary = tmp_path / "data.txt"
        not_a_binary.write_text("not a program")
        with pytest.raises(ConfigError) as exc:
            run_command([str(not_a_binary)], timeout=30)
        message = str(exc.value)
        assert str(not_a_binary) in message
        # The specific wording matters, not just the type. Dropping the pre-spawn check
        # entirely still produces a ConfigError naming the path, because exec then fails
        # with EACCES and that is caught too - so asserting only the type and the path
        # passes against code that does no checking at all. This phrase can only come
        # from the check that runs before the spawn.
        assert "not an executable file" in message

    def test_an_absent_path_is_reported_as_the_default_searched_not_as_nothing(self, monkeypatch):
        # With PATH unset, shutil.which(path=None) would fall back to the parent's PATH,
        # which is the coupling this module exists to avoid; the default is substituted
        # explicitly instead. The two happen to resolve identically today, so the message
        # is what distinguishes them - and "not found on PATH (None)" tells an operator
        # nothing about where to put the binary.
        monkeypatch.delenv("PATH", raising=False)
        with pytest.raises(ConfigError) as exc:
            run_command(["definitely-not-a-real-binary-zzz"], timeout=30)
        message = str(exc.value)
        assert repr(os.defpath) in message
        assert "None" not in message

    def test_a_relative_path_entry_still_resolves_to_an_absolute_path(self, tmp_path, monkeypatch):
        # shutil.which returns the PATH entry as it was written, so a relative entry gives
        # back a relative path and argv[0] would then depend on the caller's working
        # directory. A service's cwd is not the operator's, so "the binary it found" and
        # "the binary it runs" could differ.
        binaries = tmp_path / "bin"
        binaries.mkdir()
        tool = binaries / "faketool"
        tool.write_text("#!/bin/sh\nprintf ran\n")
        tool.chmod(0o755)
        monkeypatch.chdir(tmp_path)

        result = run_command(["faketool"], timeout=30, env_extra={"PATH": "bin"})
        assert os.path.isabs(result.argv[0]), f"argv[0] is relative: {result.argv[0]!r}"
        assert result.stdout == b"ran"

    def test_an_explicit_path_is_run_as_given(self):
        # A control process is started from the packaged venv, whose bin directory is on
        # nobody's PATH, so a path has to be usable as argv[0].
        result = run_command([os.path.abspath(sys.executable), "-c", "print('ok')"], timeout=30)
        assert result.stdout.strip() == b"ok"

    def test_resolution_follows_the_childs_path_not_the_parents(self, tmp_path):
        # The direct form of the guarantee. env_extra is the only way the two PATHs can
        # actually differ, so it is the only way to prove which one is consulted: the
        # interpreter is on the parent's PATH and not in this empty directory, so a lookup
        # against the parent's would succeed and the child's must not.
        with pytest.raises(ConfigError) as exc:
            run_command(["python3"], timeout=30, env_extra={"PATH": str(tmp_path)})
        assert str(tmp_path) in str(exc.value)

    def test_an_empty_path_is_reported_as_itself_not_as_the_default(self, tmp_path):
        # An empty PATH searches nothing, which is a different fault from an absent one.
        # Reporting the system default for both sends an operator looking in a directory
        # nothing consulted.
        with pytest.raises(ConfigError) as exc:
            run_command(["python3"], timeout=30, env_extra={"PATH": ""})
        assert os.defpath not in str(exc.value)

    def test_lookup_uses_the_childs_own_path(self, tmp_path):
        # Resolution and execution must agree. If which() searched the parent's PATH
        # while the child ran with the allow-listed one, a binary could resolve here and
        # be unfindable there.
        with patch.dict(os.environ, {"PATH": str(tmp_path)}, clear=False):
            with pytest.raises(ConfigError) as exc:
                run_command(["python3"], timeout=30)
        assert str(tmp_path) in str(exc.value)


class TestEnvironmentAllowList:
    def test_a_variable_outside_the_allow_list_does_not_reach_the_child(self):
        with patch.dict(os.environ, {"SEND_TO_INFLUX_TEST_LEAK": "leaked"}, clear=False):
            result = run_command(
                python_c("import os; print(os.environ.get('SEND_TO_INFLUX_TEST_LEAK', 'absent'))"),
                timeout=30,
            )
        assert result.stdout.strip() == b"absent"

    def test_credentials_directory_is_passed_through(self):
        # Named explicitly rather than looped over the whole allow-list: this is the one
        # whose absence looks like a permissions bug rather than a missing variable, and
        # a control process reads its own secrets from it.
        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": "/run/credentials/test"}, clear=False):
            result = run_command(
                python_c("import os; print(os.environ.get('CREDENTIALS_DIRECTORY', 'absent'))"),
                timeout=30,
            )
        assert result.stdout.strip() == b"/run/credentials/test"

    def test_state_directory_is_passed_through(self):
        with patch.dict(os.environ, {"STATE_DIRECTORY": "/var/lib/send-to-influx"}, clear=False):
            result = run_command(
                python_c("import os; print(os.environ.get('STATE_DIRECTORY', 'absent'))"),
                timeout=30,
            )
        assert result.stdout.strip() == b"/var/lib/send-to-influx"

    def test_env_extra_overrides_an_inherited_value(self):
        with patch.dict(os.environ, {"TZ": "Europe/London"}, clear=False):
            result = run_command(
                python_c("import os; print(os.environ['TZ'])"),
                timeout=30,
                env_extra={"TZ": "UTC"},
            )
        assert result.stdout.strip() == b"UTC"

    def test_the_child_environment_holds_nothing_but_the_allow_list(self):
        # The broad guard: a future edit reaching for os.environ.copy() passes every
        # targeted test above and fails this one.
        result = run_command(
            python_c("import os; print('\\n'.join(sorted(os.environ)))"),
            timeout=30,
            env_extra={"EXTRA_FOR_THIS_TEST": "1"},
        )
        seen = set(result.stdout.decode().split())
        assert seen <= set(INHERITED_ENV_KEYS) | {"EXTRA_FOR_THIS_TEST"} | _platform_injected_env()


class TestNoShell:
    def test_shell_metacharacters_are_passed_through_literally(self):
        # If this ever ran through a shell, the semicolon would start a second command
        # and the argument would not arrive intact.
        payload = "; echo pwned"
        result = run_command(python_c("import sys; sys.stdout.write(sys.argv[1])") + [payload], timeout=30)
        assert result.stdout.decode() == payload


class TestTimeout:
    def test_a_command_that_overruns_raises_process_error(self):
        with pytest.raises(ProcessError) as exc:
            run_command(python_c("import time; time.sleep(30)"), timeout=0.5)
        message = str(exc.value)
        assert "0.5" in message
        assert "killed" in message

    def test_the_overrunning_process_is_actually_killed(self):
        # Raising while leaving the child running would leak a process per failure, which
        # on a retry loop is unbounded.
        started = time.monotonic()
        with pytest.raises(ProcessError):
            run_command(python_c("import time; time.sleep(30)"), timeout=0.5)
        # A surviving child would hold the pipe open and stall the drain until its grace
        # period expired, so finishing promptly is the observable proof it died.
        assert time.monotonic() - started < 10

    def test_output_written_before_the_timeout_is_reported(self):
        with pytest.raises(ProcessError) as exc:
            run_command(
                python_c("import sys, time; sys.stderr.write('stuck on step 2'); sys.stderr.flush(); time.sleep(30)"),
                timeout=0.5,
            )
        assert "stuck on step 2" in str(exc.value)

    def test_standard_output_never_reaches_the_timeout_message(self):
        # stdout is the data channel: `systemd-creds decrypt` writes the plaintext
        # credential there. A decrypt that hung after emitting part of it would put the
        # secret into an exception message, and from there into the journal and the CLI's
        # own output. An earlier version fell back to stdout when stderr was empty, which
        # is precisely the case where the secret is all there is to fall back to.
        with pytest.raises(ProcessError) as exc:
            run_command(
                python_c(
                    "import sys, time; sys.stdout.write('super-secret-value'); sys.stdout.flush(); time.sleep(30)"
                ),
                timeout=0.5,
            )
        assert "super-secret-value" not in str(exc.value)


class TestOutputCap:
    def test_output_beyond_the_limit_is_dropped_and_flagged(self):
        result = run_command(
            python_c("import sys; sys.stdout.write('x' * 5000)"),
            timeout=30,
            output_limit=1000,
        )
        assert len(result.stdout) == 1000
        assert result.stdout_truncated

    def test_a_truncated_result_says_so_rather_than_looking_complete(self):
        result = run_command(
            python_c("import sys; sys.stdout.write('x' * 5000)"),
            timeout=30,
            output_limit=1000,
        )
        assert "truncated" in result.stdout_text

    def test_output_within_the_limit_is_not_flagged(self):
        result = run_command(python_c("print('small')"), timeout=30, output_limit=1000)
        assert not result.stdout_truncated
        assert "truncated" not in result.stdout_text

    def test_a_child_exceeding_the_cap_still_runs_to_completion(self):
        # The cap must not become a deadlock. A reader that stopped at the limit would
        # leave the child blocked writing into a full pipe, and this test would hit its
        # timeout instead of returning - which is exactly the bug the drain-past-the-cap
        # loop exists to prevent. The payload is far larger than a pipe buffer.
        result = run_command(
            python_c("import sys; sys.stdout.write('y' * 2_000_000); sys.exit(0)"),
            timeout=60,
            output_limit=1000,
        )
        assert result.ok
        assert len(result.stdout) == 1000
        assert result.stdout_truncated


class TestDecoding:
    def test_raw_bytes_survive_verbatim(self):
        # A caller holding a credential decodes strictly itself, so the bytes it decodes
        # have to be exactly what the command emitted.
        result = run_command(
            python_c("import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"),
            timeout=30,
        )
        assert result.stdout == b"\xff\xfe"

    def test_text_access_replaces_undecodable_bytes_rather_than_raising(self):
        result = run_command(
            python_c("import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"),
            timeout=30,
        )
        assert result.stdout_text == "��"


class TestAnInheritedPipeDoesNotStallTheCall:
    """A grandchild that inherits a pipe holds its write end open, so the read never
    reaches EOF however long the wait.

    This was a real defect in the first version of this module, which gave each pipe a
    reader thread. Such a thread cannot be cleaned up: closing the stream from another
    thread waits on the same lock the blocked read holds rather than interrupting it
    (measured - a close took as long as the reader stayed blocked). The choice was
    therefore between leaking a thread per call and blocking the caller past its own
    timeout, and a supervisor launching control processes on a loop would do both. The
    pump is single-threaded for this reason.
    """

    def test_the_call_returns_promptly_instead_of_waiting_for_the_grandchild(self, monkeypatch):
        monkeypatch.setattr(process_module, "_DRAIN_GRACE_SECONDS", 0.5)
        started = time.monotonic()
        result = run_command(
            python_c(
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "sys.exit(0)"
            ),
            timeout=30,
        )
        elapsed = time.monotonic() - started
        assert result.ok
        # The grandchild lives for 30s. Anything close to that means the call waited for
        # a pipe nothing was going to close.
        assert elapsed < 10, f"the call took {elapsed:.1f}s, so it waited on the inherited pipe"

    def test_no_threads_are_left_behind(self, monkeypatch):
        # Trivially true while the pump stays single-threaded, and that is the point: it
        # fails the moment someone reintroduces a reader thread that cannot be joined.
        monkeypatch.setattr(process_module, "_DRAIN_GRACE_SECONDS", 0.5)
        before = {thread.ident for thread in threading.enumerate()}
        run_command(
            python_c(
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "sys.exit(0)"
            ),
            timeout=30,
        )
        leaked = {t.ident for t in threading.enumerate() if t.is_alive()} - before
        assert not leaked, f"{len(leaked)} thread(s) outlived the call"

    def test_output_written_before_the_pipe_was_inherited_is_still_returned(self, monkeypatch):
        # Abandoning the pipe must not cost the output the command actually produced.
        monkeypatch.setattr(process_module, "_DRAIN_GRACE_SECONDS", 0.5)
        result = run_command(
            python_c(
                "import subprocess, sys; "
                "sys.stdout.write('said this first'); sys.stdout.flush(); "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "sys.exit(0)"
            ),
            timeout=30,
        )
        assert result.stdout == b"said this first"


class TestTheTimeoutAlwaysApplies:
    """The mandatory timeout is this module's headline guarantee, and the pump is where
    it is actually enforced - `wait()` afterwards is unbounded by design, because the
    pump only returns normally once the child has gone."""

    def test_a_child_that_closes_its_pipes_and_keeps_running_still_times_out(self):
        # The hole this covers: with both pipes at EOF the selector map empties, so a loop
        # written as `while selector.get_map()` falls out while the child is still alive,
        # and the caller goes straight into an unbounded wait. Measured at 20s against a
        # 1s timeout before the fix.
        started = time.monotonic()
        with pytest.raises(ProcessError):
            run_command(python_c("import os, time; os.close(1); os.close(2); time.sleep(20)"), timeout=1.0)
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"the call took {elapsed:.1f}s, so the timeout was not enforced"

    def test_a_child_that_closes_only_stdout_still_times_out(self):
        # Half the case above: one pipe gone, one still open. The loop must not treat a
        # partially-empty selector map as a reason to stop counting.
        started = time.monotonic()
        with pytest.raises(ProcessError):
            run_command(python_c("import os, time; os.close(1); time.sleep(20)"), timeout=1.0)
        assert time.monotonic() - started < 10


class TestSpuriousReadiness:
    def test_a_pipe_that_reports_ready_and_then_refuses_is_retried(self):
        # A selector reporting readiness is a hint, not a promise. Impossible to provoke
        # with a real pipe, so the guard is exercised directly: without it the
        # BlockingIOError escapes run_command and takes down the caller for a condition
        # that means only "nothing to do yet".
        real_read = process_module.os.read
        state = {"raised": False}

        def flaky_read(fd, size):
            # Only the pump reads a whole chunk at a time. Popen does its own os.read on
            # the exec-status pipe while spawning, and failing that one turns this into a
            # spawn error instead of exercising the guard under test.
            if size == process_module._READ_CHUNK and not state["raised"]:
                state["raised"] = True
                raise BlockingIOError("resource temporarily unavailable")
            return real_read(fd, size)

        with patch.object(process_module.os, "read", flaky_read):
            result = run_command(python_c("print('survived')"), timeout=30)
        assert state["raised"], "the guard was never reached, so this asserts nothing"
        assert result.stdout.strip() == b"survived"


class TestTheTruncationMarkerNamesTheRealLimit:
    def test_a_smaller_output_limit_is_reported_rather_than_the_default(self):
        # The marker used to name the module default whatever cap was in force, so a
        # caller passing a smaller limit was told a figure that was never applied. The
        # existing cap tests asserted only that the word "truncated" appeared, which is
        # why this went unnoticed.
        result = run_command(python_c("import sys; sys.stdout.write('x' * 5000)"), timeout=30, output_limit=1000)
        assert "1000" in result.stdout_text
        assert str(process_module.MAX_CAPTURED_BYTES) not in result.stdout_text

    def test_the_default_limit_is_still_reported_when_it_is_the_one_in_force(self):
        result = CommandResult(argv=["x"], returncode=0, stdout=b"cut", stderr=b"", stdout_truncated=True)
        assert str(process_module.MAX_CAPTURED_BYTES) in result.stdout_text
