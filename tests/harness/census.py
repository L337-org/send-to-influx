"""Counting what a run leaves behind: processes, threads and file descriptors.

Read from the operating system rather than from the thing being measured. A supervisor
that leaked a descriptor per restart would report itself healthy throughout, and the only
place the truth exists is the kernel's own accounting.

Not every count is available everywhere. Where one is not, this says which and why rather
than returning a number it cannot stand behind: a census that silently counted zero threads
would read exactly like a run that leaked none.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import os
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class Census:
    """What was running at one moment.

    Attributes:
        processes (int): every descendant of the process, not counting the process itself -
            what a supervisor leaks is children, and including the subject would make every
            count one larger for no information.
        threads (int or None): its threads, None where this platform was not readable.
        descriptors (int or None): its open file descriptors, None likewise.
        skipped (tuple): what could not be counted, and why.
    """

    processes: int
    threads: "int | None" = None
    descriptors: "int | None" = None
    skipped: tuple = field(default_factory=tuple)


def _process_table():
    """Return ``{pid: ppid}`` for every process on the machine.

    Returns:
        dict: pid to parent pid

    Raises:
        AssertionError: ps could not be read, which no platform this runs on should do
    """
    # Popen rather than run(), so the pid of the `ps` itself is known and can be left out.
    # It is a child of whoever takes the census, and it is running while it lists the
    # table, so counting it makes every self-census one process too many - a measurement
    # that is wrong by exactly one is worse than one that is obviously wrong.
    child = None
    try:
        child = subprocess.Popen(["ps", "-Ao", "pid=,ppid="], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        output, errors = child.communicate(timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        # A timed-out communicate() leaves the child running, and an unreaped `ps` would
        # then appear in the very count it was spawned to take.
        if child is not None:
            child.kill()
            child.communicate()
        raise AssertionError(f"the harness could not read the process table: {exc}") from exc
    # Raised rather than skipped, unlike the counts below. A `ps` that failed yields an
    # empty table, an empty table yields no descendants, and no descendants reads exactly
    # like a run that leaked nothing - the census would report the answer it was written to
    # detect the absence of.
    if child.returncode != 0:
        raise AssertionError(f"ps exited {child.returncode} while listing the process table: {errors.strip()!r}")
    table = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[0]) != child.pid:
            table[int(parts[0])] = int(parts[1])
    return table


def descendants(pid, table=None):
    """Return every descendant of a process, however deep.

    Args:
        pid (int): the process to start from
        table (dict or None): a process table from :func:`_process_table`, read fresh when None

    Returns:
        set: the descendant pids, not including ``pid`` itself
    """
    table = _process_table() if table is None else table
    children = {}
    for child, parent in table.items():
        children.setdefault(parent, []).append(child)
    found, pending = set(), list(children.get(pid, []))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(children.get(current, []))
    return found


def _threads(pid):
    """Return a process's thread count, or None with a reason.

    Args:
        pid (int): the process to count

    Returns:
        tuple: ``(count_or_None, reason_or_None)``
    """
    status = f"/proc/{pid}/status"
    if os.path.exists(status):
        with open(status, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1]), None
        return None, f"threads: {status} carried no Threads: line"
    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["ps", "-M", "-p", str(pid)], capture_output=True, text=True, timeout=20, check=True
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"threads: ps -M failed: {exc}"
        # A header line, then one line per thread.
        lines = [line for line in output.splitlines() if line.strip()]
        return (len(lines) - 1, None) if len(lines) > 1 else (None, "threads: ps -M listed none")
    return None, f"threads: no count available on {sys.platform}"


def _descriptors(pid):
    """Return a process's open descriptor count, or None with a reason.

    Args:
        pid (int): the process to count

    Returns:
        tuple: ``(count_or_None, reason_or_None)``
    """
    directory = f"/proc/{pid}/fd"
    if os.path.isdir(directory):
        try:
            return len(os.listdir(directory)), None
        except OSError as exc:
            return None, f"descriptors: {directory} could not be listed: {exc}"
    return None, f"descriptors: no /proc/<pid>/fd on {sys.platform}"


def take(pid):
    """Return a census of one process and everything under it.

    Args:
        pid (int): the process at the top

    Returns:
        Census: what was running
    """
    table = _process_table()
    threads, thread_reason = _threads(pid)
    fds, fd_reason = _descriptors(pid)
    skipped = tuple(reason for reason in (thread_reason, fd_reason) if reason)
    return Census(processes=len(descendants(pid, table)), threads=threads, descriptors=fds, skipped=skipped)
