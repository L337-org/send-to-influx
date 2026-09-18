"""The fault injector: the four ways a control's world goes wrong, each with a duration.

Every fault is a context manager that clears itself, including when the body raises. A
fault left switched on by a failed assertion makes every later scenario fail for a reason
that has nothing to do with what it was testing, and the second failure is the one people
read.

The process faults are the two that matter to a watchdog and they are not the same
failure. A killed control stops; a stopped one keeps its file descriptors, keeps its pipe
open, and simply stops saying anything - which is the case a heartbeat exists to catch and
the case a naive "is the process alive" check gets wrong.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import signal
import time
from contextlib import contextmanager


@contextmanager
def unreachable(endpoint):
    """Make an endpoint accept connections and drop them.

    Args:
        endpoint (StubEndpoint): the endpoint to break

    Yields:
        StubEndpoint: the endpoint, while it is broken
    """
    endpoint.unreachable = True
    try:
        yield endpoint
    finally:
        endpoint.unreachable = False


@contextmanager
def hanging(endpoint, seconds):
    """Make an endpoint answer, slowly.

    Args:
        endpoint (StubEndpoint): the endpoint to slow down
        seconds (float): how long to wait before answering

    Yields:
        StubEndpoint: the endpoint, while it is slow
    """
    previous = endpoint.hang_seconds
    endpoint.hang_seconds = float(seconds)
    try:
        yield endpoint
    finally:
        endpoint.hang_seconds = previous


@contextmanager
def erroring(endpoint, status=503):
    """Make an endpoint answer with an HTTP error.

    Args:
        endpoint (StubEndpoint): the endpoint to break
        status (int): the status to answer with

    Yields:
        StubEndpoint: the endpoint, while it is failing
    """
    previous = endpoint.status
    endpoint.status = int(status)
    try:
        yield endpoint
    finally:
        endpoint.status = previous


@contextmanager
def frozen(influx):
    """Stop a source's data advancing, so its readings age.

    The fault a control has to notice by itself: the far end answers promptly and
    successfully with a value that stopped being true an hour ago.

    Args:
        influx (StubInflux): the InfluxDB to freeze

    Yields:
        StubInflux: the InfluxDB, while it is frozen
    """
    previous = influx.frozen
    influx.frozen = True
    try:
        yield influx
    finally:
        influx.frozen = previous


@contextmanager
def stopped(process):
    """Suspend a process with SIGSTOP, and resume it afterwards.

    A stopped control is alive by every cheap test - it has a pid, its pipe is open, its
    file descriptors are held - and says nothing. It is the case a heartbeat exists for,
    and the case "is the process still there" answers wrongly.

    Args:
        process (subprocess.Popen): the process to suspend

    Yields:
        subprocess.Popen: the process, while it is suspended
    """
    process.send_signal(signal.SIGSTOP)
    try:
        yield process
    finally:
        try:
            process.send_signal(signal.SIGCONT)
        except OSError:
            # Killed while suspended, by the test or by a supervisor doing its job.
            pass


def kill(process, signal_number=signal.SIGKILL, timeout=5):
    """Signal a process and wait for it to go.

    Args:
        process (subprocess.Popen): the process to kill
        signal_number (int): the signal to send
        timeout (float): how long to wait for it to exit

    Returns:
        int: the exit status, negative where a signal ended it

    Raises:
        AssertionError: it was still running when the timeout expired
    """
    process.send_signal(signal_number)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return process.returncode
        time.sleep(0.02)
    raise AssertionError(f"process {process.pid} survived {signal_number} for {timeout}s")
