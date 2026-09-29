"""Writing to InfluxDB, off the caller's thread: a disk spool and the one thread that drains it.

**Nothing that collects or controls waits on InfluxDB.** ``DataHandler.send_data()`` used to post
in the caller's own thread, so an outage cost a collector its cadence - the failed write raised,
the worker backed off from its ``interval`` to as long as 300 s, and the readings in between were
never taken - and cost a control the start of its next cycle. Now ``send_data()`` hands the point
to this process's :class:`InfluxWriter`, which appends it to a spool on disk, syncs, and returns.
The point is committed at that moment, and a thread of the writer's own posts it.

**The spool.** JSON Lines in ``<state directory>/spool/<process>/``, split into fixed-size
segments so that sent points are removed by deleting a whole file rather than rewriting one. How
far the thread has got is kept in a small pointer file, rewritten by atomic rename after every
successful post and deliberately not synced: losing it in a crash costs at most one chunk sent
twice, and InfluxDB overwrites a point with the same series, fields and timestamp.

**Bounded by ``influx.buffer_mb``**, per process. When the spool is full the oldest segments go
first. ``0`` - and a disk that cannot be written - holds points in memory instead, 500 per
worker, lost on exit. The caller is never told which it got: nothing it could do would differ,
so the operator is told instead, once.

**What is carried over from the in-memory buffer this replaces:** a connection failure, a 5xx,
a 408 or a 429 says nothing about the point and never counts against it; a non-transient 4xx is
the server refusing the point, and a point refused ``MAX_POINT_REJECTIONS`` times is dropped. A
refused point goes to the back of the spool rather than blocking the points behind it.

**The destination is resolved when a point is sent**, from the latest settings a handler for its
source was built with, so a rebuilt database or a move between InfluxDB versions receives the
backlog as well as new points. A point whose source is no longer configured is dropped, said.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import atexit
import fcntl
import json
import logging
import os
import threading
import time
import warnings
from collections import deque
from dataclasses import dataclass

import requests
import urllib3

from toinflux.general import RepeatingProblem, resolve_state_dir

#: Where every process's spool directory lives, under the state directory.
SPOOL_DIR_NAME = "spool"

#: The spool's size bound when ``influx.buffer_mb`` is not set, and its range.
BUFFER_MB_DEFAULT = 100
BUFFER_MB_MIN = 0
BUFFER_MB_MAX = 1024

#: How large a segment grows before appends move to a new one. Small enough that even the 1 MB
#: minimum leaves eight segments to drop from, so the bound is kept to within an eighth.
SEGMENT_BYTES = 128 * 1024

#: How many points are posted per request. InfluxDB accepts newline-joined bodies, so draining a
#: long backlog costs a handful of requests rather than one per point.
CHUNK_POINTS = 100

#: How many points each worker may hold in memory, where there is no spool to hold them.
MEMORY_POINTS_PER_WORKER = 500

#: How many unbuffered points - heartbeats, annotations - may wait for the thread. They are live
#: signals with no replay value, so a full queue drops the oldest rather than growing.
LIVE_POINTS = 200

#: How many times the server may refuse one point before it is given up on.
MAX_POINT_REJECTIONS = 5

#: 4xx statuses that describe the connection or the server's state rather than the point:
#: 408 Request Timeout and 429 Too Many Requests. Counting them would age valid points out of the
#: spool during rate limiting.
TRANSIENT_CLIENT_ERRORS = frozenset({408, 429})

#: The wait after a failed post, doubling to the ceiling while InfluxDB stays unreachable. The
#: ceiling is short because a new point does not trigger a post during an outage - only this
#: timer does - so it is also the longest a recovery goes unnoticed.
RETRY_FIRST_SECONDS = 5.0
RETRY_MAX_SECONDS = 60.0

#: How long a stopping process gives the thread to post what is still waiting. Whatever is left
#: stays spooled for the next start.
CLOSE_SECONDS = 5.0


def is_point_rejection(status_code):
    """Return whether a status means the server received the point and refused it.

    A 4xx other than 408 and 429, as opposed to a connection failure (None), a server error
    (5xx) or a rate limit, none of which says anything about the point itself.

    Args:
        status_code (int or None): the status the post returned, or None where none arrived

    Returns:
        bool: True where the point itself was refused
    """
    return status_code is not None and 400 <= status_code < 500 and status_code not in TRANSIENT_CLIENT_ERRORS


def build_write_request(source_settings, influx_settings):
    """Return the URL and request arguments for posting a source's points.

    ``bucket``, falling back to ``db``, on InfluxDB 2 (``influx.token`` set); ``db`` on InfluxDB 1.
    The same choice ``influx.resolve_db()`` makes for reads, so reads and writes cannot disagree.

    Args:
        source_settings (dict): the source's own settings section
        influx_settings (dict): the ``influx`` section

    Returns:
        tuple: (url, kwargs for ``requests.Session.post``)

    Raises:
        KeyError: where a key the write needs is missing, which validation refuses first
    """
    timeout = influx_settings.get("timeout", 5)
    if influx_settings.get("token"):
        url = (
            f'{influx_settings["url"]}/api/v2/write'
            f'?org={influx_settings["org"]}'
            f'&bucket={source_settings.get("bucket", source_settings.get("db"))}'
            f"&precision=s"
        )
        kwargs = {"headers": {"Authorization": f'Token {influx_settings["token"]}'}}
    else:
        url = f'{influx_settings["url"]}/write?db={source_settings["db"]}&precision=s'
        kwargs = {"auth": (influx_settings["user"], influx_settings["password"])}
    kwargs["verify"] = not influx_settings.get("insecure", False)
    kwargs["timeout"] = timeout
    return url, kwargs


def buffer_mb_problem(influx_settings):
    """Return why ``influx.buffer_mb`` is unusable, or None where it is usable or absent.

    Args:
        influx_settings (dict): the ``influx`` section

    Returns:
        str or None: the error, naming the setting
    """
    if "buffer_mb" not in influx_settings:
        return None
    value = influx_settings["buffer_mb"]
    if isinstance(value, bool) or not isinstance(value, int) or not BUFFER_MB_MIN <= value <= BUFFER_MB_MAX:
        return (
            f"influx.buffer_mb must be a whole number of megabytes from {BUFFER_MB_MIN} to {BUFFER_MB_MAX} "
            f"(got {value!r}); 0 holds unsent points in memory only"
        )
    return None


def _destinations(settings):
    """Return every settings section's write destination, keyed by section name.

    Only sections that name a database. Used to seed a starting writer, so points spooled by a
    previous run can be sent before their source's handler has been built this time.

    Args:
        settings (dict): the parsed settings document

    Returns:
        dict: section name to (url, kwargs)
    """
    influx_settings = settings.get("influx") or {}
    found = {}
    for name, section in settings.items():
        if name == "influx" or not isinstance(section, dict):
            continue
        if not (section.get("db") or (influx_settings.get("token") and section.get("bucket"))):
            continue
        try:
            found[name] = build_write_request(section, influx_settings)
        except KeyError:
            # Validation refuses an influx section missing a key the write needs, so only a
            # process started without validating reaches here; its handlers fail loudly first.
            continue
    return found


@dataclass(slots=True)
class _Entry:
    """One point waiting to be sent.

    Attributes:
        source (str): the source it belongs to, which is how its destination is found
        instance (str or None): the instance that wrote it
        line (str): the line-protocol point
        rejections (int): how many times the server has refused it
        end (tuple or None): where it ends in the spool, as (segment, offset); None in memory
    """

    source: str
    instance: "str | None"
    line: str
    rejections: int = 0
    end: "tuple | None" = None

    def encoded(self):
        """Return this entry as one spool line.

        Returns:
            bytes: the JSON object and its newline
        """
        record = {"s": self.source, "i": self.instance, "l": self.line, "r": self.rejections}
        return (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")

    @property
    def label(self):
        """Return the worker this entry came from, for messages.

        Returns:
            str: ``source`` or ``source@instance``
        """
        return self.source if self.instance in (None, self.source) else f"{self.source}@{self.instance}"


class InfluxWriter:
    """This process's writer: the spool, the memory fallback, and the thread that posts both.

    One per process, from :func:`writer_for`. Thread-safe: callers append from any thread, and
    only the writer's own thread posts. The lock guards the spool's files and the queues, and is
    never held while posting.
    """

    def __init__(self, name, spool_root, settings, buffer_mb=BUFFER_MB_DEFAULT, inline=False, clock=time.monotonic):
        """Open this process's spool, or fall back to memory where it cannot be opened.

        Args:
            name (str): which process this is, and so which spool directory it owns
            spool_root (str): the directory every process's spool lives under
            settings (dict): the settings in effect, to seed destinations from
            buffer_mb (int): the spool's bound in megabytes; 0 for memory only
            inline (bool): post from the caller's thread as each point arrives, rather than
                from the writer's own. Only for tests, which need to see a point's fate
                before the call returns; everything else about the path is the same
            clock (callable): monotonic seconds, for the retry timer
        """
        self.name = name
        self.directory = os.path.join(spool_root, name)
        self.limit_bytes = int(buffer_mb) * 1024 * 1024
        self.inline = inline
        self._clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = False
        self._thread = None
        self._problems = RepeatingProblem()
        self._destinations = dict(_destinations(settings))
        self._last_line = {}
        self._memory = {}
        self._live = deque(maxlen=LIVE_POINTS)
        self._retry_at = 0.0
        self._retry_delay = RETRY_FIRST_SECONDS
        self._last_error = None
        self._session = requests.Session()
        self._segments = []
        self._sizes = {}
        self._append_segment = None
        self._append_file = None
        self._pointer = None
        self._stale_segment = 0
        self._lock_file = None
        self._disk = False
        if self.limit_bytes:
            self._open_spool()
        else:
            logging.info(
                "InfluxDB points waiting to be sent are held in memory only (influx.buffer_mb is 0), "
                "so they are lost if %s stops before InfluxDB can take them",
                name,
            )

    # ------------------------------------------------------------------ setup

    def _open_spool(self) -> None:
        """Take this process's spool directory, or fall back to memory, saying why."""
        try:
            # The shared parent too, and first: makedirs applies its mode to the last directory
            # only, so the parent was left at the default 0755 by the call below on its own.
            os.makedirs(os.path.dirname(self.directory), mode=0o700, exist_ok=True)
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            self._lock_file = open(os.path.join(self.directory, "lock"), "a", encoding="utf-8")
            os.chmod(self._lock_file.name, 0o600)
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._load_spool()
        except BlockingIOError:
            # Another process has this spool: a manual `--source` run beside the service, say.
            # Sharing it would have two processes deleting each other's segments.
            self._close_lock_file()
            self._memory_mode_problem(f"another process is using the spool in {self.directory!r}")
            return
        except OSError as exc:
            # Anywhere in the opening, not only the directory and the lock: a disk that is full
            # when the first segment is created raised out of the constructor and stopped the
            # service from starting at all, where the design promises the memory fallback.
            self._abandon_spool()
            self._memory_mode_problem(f"the spool in {self.directory!r} could not be opened: {exc!r}")
            return
        waiting = sum(self._sizes.values()) - self._sizes.get(self._append_segment, 0)
        # Said once for every process, so a spool that is not where the operator expects - the
        # wrong state directory, a checkout rather than /var/lib - is visible the first time.
        logging.info(
            "InfluxDB points for %s are buffered on disk in %r, up to influx.buffer_mb (%d MB)%s",
            self.name,
            self.directory,
            self.limit_bytes // (1024 * 1024),
            ", resuming the unsent points already there" if waiting else "",
        )

    def _load_spool(self) -> None:
        """Read what a previous run left, and open a segment of this run's own.

        Raises:
            OSError: where the spool cannot be read or a segment cannot be created
        """
        self._segments = sorted(
            int(entry[: -len(".jsonl")])
            for entry in os.listdir(self.directory)
            if entry.endswith(".jsonl") and entry[: -len(".jsonl")].isdigit()
        )
        self._sizes = {segment: os.path.getsize(self._segment_path(segment)) for segment in self._segments}
        # Every start opens a segment of its own, so a process that never wrote leaves one
        # behind empty. Removed here rather than left for the thread, which only looks when
        # something is written.
        for segment in [number for number, size in self._sizes.items() if not size]:
            os.remove(self._segment_path(segment))
            self._segments.remove(segment)
            del self._sizes[segment]
        self._pointer, self._stale_segment = self._read_pointer()
        # Appends always start a segment of their own: whatever a previous run left is read-only,
        # so a line it was cut off in the middle of stays the last line of its file.
        self._start_segment()
        self._disk = True

    def _abandon_spool(self) -> None:
        """Let go of a spool that could not be opened, so nothing half-open is used."""
        if self._append_file is not None:
            try:
                self._append_file.close()
            except OSError:
                # Closing a file that could not be written can fail the same way; it is being
                # abandoned either way, and the reason is already on its way to the log.
                pass
            self._append_file = None
        self._close_lock_file()
        self._segments, self._sizes = [], {}
        self._append_segment, self._pointer = None, None

    def _close_lock_file(self) -> None:
        """Close the lock file, if one was opened."""
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def _segment_path(self, segment):
        """Return a segment's path.

        Args:
            segment (int): the segment's number

        Returns:
            str: its path
        """
        return os.path.join(self.directory, f"{segment:012d}.jsonl")

    def _read_pointer(self):
        """Return where sending should resume, and the segment a stored pointer named.

        A missing or unreadable pointer resumes from the oldest segment, which re-sends what
        that segment holds; InfluxDB absorbs the duplicates. So does one naming an offset past
        the end of its segment, which no pointer this writer wrote can do.

        The stored segment is returned whatever happens to it, so that no segment created from
        here on is given that number: a pointer left naming a segment that has gone would
        otherwise be trusted by the next run that reused the number, and skip what it held.

        Returns:
            tuple: ((segment, offset) to resume from, the stored pointer's segment or 0)
        """
        oldest = (self._segments[0], 0) if self._segments else (0, 0)
        try:
            with open(os.path.join(self.directory, "pointer.json"), encoding="utf-8") as handle:
                stored = json.load(handle)
            segment, offset = int(stored["segment"]), int(stored["offset"])
        except FileNotFoundError:
            return oldest, 0
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logging.warning(
                "The spool pointer in %r is unreadable, so sending resumes from its oldest point: %r",
                self.directory,
                exc,
            )
            return oldest, 0
        if segment not in self._sizes:
            return oldest, segment
        if not 0 <= offset <= self._sizes[segment]:
            return (segment, 0), segment
        return (segment, offset), segment

    def _write_pointer(self) -> None:
        """Record how far sending has got. Not synced, deliberately: see the module docstring."""
        path = os.path.join(self.directory, "pointer.json")
        temporary = path + ".tmp"
        try:
            # 0600 like every other file here, created so rather than chmod-ed afterwards, so it
            # is never readable by anyone else even for a moment.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"segment": self._pointer[0], "offset": self._pointer[1]}, handle)
            os.replace(temporary, path)
        except OSError as exc:
            # Tolerated: the cost of a stale pointer is a chunk sent twice after a restart.
            self._problems.report(
                "pointer",
                logging.WARNING,
                "Could not record the spool pointer in %r: %r",
                self.directory,
                exc,
                identity="pointer",
            )

    def _start_segment(self) -> None:
        """Close the segment being appended to and start the next.

        Raises:
            OSError: where the new segment cannot be created
        """
        if self._append_file is not None:
            self._append_file.close()
            self._append_file = None
        # Above every number in use, the pointer's and a stale stored pointer's included, so no
        # pointer can ever come to name a segment it was not written for.
        segment = 1 + max(self._segments + [self._pointer[0] if self._pointer else 0, self._stale_segment])
        path = self._segment_path(segment)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._append_file = os.fdopen(descriptor, "ab")
        self._segments.append(segment)
        self._sizes[segment] = 0
        self._append_segment = segment
        if self._pointer is None or self._pointer[0] not in self._sizes:
            self._pointer = (self._segments[0], 0)

    # ---------------------------------------------------------------- appending

    def submit(self, source, instance, line, buffered=True) -> None:
        """Take one point, committed to the spool before this returns where there is one.

        Never raises for InfluxDB, the disk or the network: the caller is collecting or
        controlling, and nothing it could do about a write would help.

        Args:
            source (str): the source the point belongs to
            instance (str or None): the instance that wrote it
            line (str): the line-protocol point
            buffered (bool): False for a live signal with no replay value - a heartbeat - which
                is posted once if it can be and otherwise dropped
        """
        with self._lock:
            if not buffered:
                self._live.append(_Entry(source, instance, line))
            elif self._last_line.get((source, instance)) == line:
                # Octopus re-serves one reading, timestamp and all, for about half an hour;
                # a second copy would only take space, since posting it again changes nothing.
                return
            else:
                self._last_line[(source, instance)] = line
                self._append(_Entry(source, instance, line))
        if self.inline:
            self.run_until_idle()
        else:
            self._ensure_thread()
            self._wake.set()

    def _append(self, entry) -> None:
        """Append one entry to the spool, or to memory where the spool is not available.

        Args:
            entry (_Entry): the point
        """
        if self._disk:
            try:
                self._move_memory_to_spool()
                self._write_entry(entry)
                return
            except OSError as exc:
                self._disk = False
                self._memory_mode_problem(f"the spool in {self.directory!r} could not be written: {exc!r}")
        elif self._lock_file is not None:
            # The spool failed earlier and may be back: try it before falling back again.
            try:
                self._move_memory_to_spool()
                self._write_entry(entry)
                self._disk = True
                self._problems.cleared("memory", "InfluxDB points for %s are being spooled to disk again", self.name)
                return
            except OSError:
                pass
        queue = self._memory.setdefault((entry.source, entry.instance), deque())
        if len(queue) >= MEMORY_POINTS_PER_WORKER:
            queue.popleft()
            self._problems.report(
                "full",
                logging.WARNING,
                "InfluxDB points for %s are being dropped, oldest first: %d are held in memory per worker",
                entry.label,
                MEMORY_POINTS_PER_WORKER,
                identity="full",
            )
        queue.append(entry)

    def _move_memory_to_spool(self) -> None:
        """Spool whatever memory is holding, first, so no worker's points go out of order.

        Raises:
            OSError: where the spool cannot be written
        """
        for queue in self._memory.values():
            while queue:
                self._write_entry(queue[0])
                queue.popleft()

    def _write_entry(self, entry) -> None:
        """Append, sync, and keep the spool inside its bound.

        Args:
            entry (_Entry): the point

        Raises:
            OSError: where the spool cannot be written
        """
        if self._sizes[self._append_segment] >= SEGMENT_BYTES:
            self._start_segment()
        data = entry.encoded()
        self._append_file.write(data)
        self._append_file.flush()
        os.fsync(self._append_file.fileno())
        self._sizes[self._append_segment] += len(data)
        self._enforce_limit()

    def _enforce_limit(self) -> None:
        """Drop the oldest segments until the spool fits its bound, saying how many points went."""
        dropped = 0
        while sum(self._sizes.values()) > self.limit_bytes and len(self._segments) > 1:
            oldest = self._segments.pop(0)
            path = self._segment_path(oldest)
            skip = self._pointer[1] if self._pointer[0] == oldest else 0
            try:
                with open(path, "rb") as handle:
                    handle.seek(skip)
                    dropped += handle.read().count(b"\n")
                os.remove(path)
            except OSError as exc:
                logging.warning("Could not remove the spool segment %r: %r", path, exc)
            del self._sizes[oldest]
            if self._pointer[0] == oldest:
                self._pointer = (self._segments[0], 0)
        if dropped:
            self._problems.report(
                "full",
                logging.WARNING,
                "The InfluxDB spool for %s is full at influx.buffer_mb (%d MB), so its oldest %d point(s) were dropped",
                self.name,
                self.limit_bytes // (1024 * 1024),
                dropped,
                identity="full",
            )

    def _memory_mode_problem(self, why) -> None:
        """Say once that points are now held in memory, and why.

        Args:
            why (str): what went wrong with the spool
        """
        self._problems.report(
            "memory",
            logging.WARNING,
            "InfluxDB points for %s are held in memory until they can be sent, and are lost if it stops: %s",
            self.name,
            why,
            identity="memory",
        )

    # ------------------------------------------------------------ destinations

    def set_destination(self, source, url, kwargs) -> None:
        """Record where a source's points go now, from the settings its handler was built with.

        Args:
            source (str): the source
            url (str): its write URL
            kwargs (dict): its request arguments
        """
        with self._lock:
            self._destinations[source] = (url, kwargs)

    def pending(self):
        """Return whether any buffered point is still waiting to be sent.

        Returns:
            bool: True where the spool or memory holds something unsent
        """
        with self._lock:
            if any(self._memory.values()):
                return True
            if not self._disk:
                return False
            segment, offset = self._pointer
            return (
                any(size for number, size in self._sizes.items() if number > segment)
                or self._sizes.get(segment, 0) > offset
            )

    # ------------------------------------------------------------------ sending

    def _ensure_thread(self) -> None:
        """Start the writer's thread, once."""
        with self._lock:
            if self._thread is None and not self._stopping:
                self._thread = threading.Thread(target=self._run, name=f"influx-writer-{self.name}", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        """Post whatever is waiting, then wait for more or for the retry timer."""
        while not self._stopping:
            self.run_until_idle(force=False)
            with self._lock:
                waiting = self.pending()
            timeout = max(0.1, self._retry_at - self._clock()) if waiting else None
            self._wake.wait(timeout)
            self._wake.clear()

    def run_until_idle(self, force=True) -> None:
        """Post until nothing is waiting or a post fails for want of InfluxDB.

        Args:
            force (bool): ignore the retry timer. Tests pass True; the thread passes False, so
                an outage costs one attempt per retry rather than one per point
        """
        self._post_live()
        if not force and self._clock() < self._retry_at:
            return
        while True:
            outcome = self._post_next_chunk()
            if outcome == "idle":
                return
            if outcome in ("failed", "refused"):
                # A refusal waits for the timer too. The refused point has gone to the back of
                # the spool, and without the wait this same pass would read it straight back and
                # spend all five of its attempts in a moment - the guarantee is five *separate*
                # attempts, so a middlebox answering 4xx for a briefly-down InfluxDB cannot
                # discard what it holds.
                self._retry_at = self._clock() + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2, RETRY_MAX_SECONDS)
                return
            self._retry_delay = RETRY_FIRST_SECONDS
            self._retry_at = 0.0

    def _post_live(self) -> None:
        """Post each live signal once, dropping any that cannot be sent."""
        while True:
            with self._lock:
                if not self._live:
                    return
                entry = self._live.popleft()
                destination = self._destinations.get(entry.source)
            if destination is None:
                continue
            status = self._post(entry.line, destination)
            if status is not True:
                # Dropped, as a heartbeat always was: replaying one would record that the
                # collector was up at some past moment, which says nothing about now.
                logging.debug("Dropped a live InfluxDB point for %s: the post failed", entry.label)

    def _post_next_chunk(self):
        """Post the next chunk of waiting points.

        Returns:
            str: "idle" where nothing was waiting, "sent" where the chunk was dealt with,
            "refused" where it was dealt with but the server refused a point in it, and
            "failed" where InfluxDB could not be reached, leaving the rest of it where it was
        """
        with self._lock:
            chunk = self._read_chunk()
        if not chunk:
            return "idle"
        done = []
        outcome = "sent"
        for run in _runs(chunk):
            result = self._send_run(run, done)
            if result == "failed":
                outcome = "failed"
                break
            if result == "refused":
                outcome = "refused"
        with self._lock:
            self._consume(done)
        if outcome != "failed":
            self._problems.cleared("write", "InfluxDB is taking points from %s again", self.name)
        return outcome

    def _send_run(self, run, done):
        """Post a run of points bound for one database, adding each one dealt with to ``done``.

        Args:
            run (list): _Entry, all for the same source
            done (list): filled in, in order, with every entry sent, refused or dropped

        Returns:
            str: "sent", "refused" where the server refused at least one point, or "failed"
            where InfluxDB could not be reached, so the caller stops
        """
        with self._lock:
            destination = self._destinations.get(run[0].source)
        if destination is None:
            self._problems.report(
                ("unconfigured", run[0].source),
                logging.WARNING,
                "Dropping unsent InfluxDB points for %r: it has no database configured any more",
                run[0].source,
                identity="unconfigured",
            )
            done.extend(run)
            return "sent"
        status = self._post("\n".join(entry.line for entry in run), destination)
        if status is True:
            done.extend(run)
            return "sent"
        if not is_point_rejection(status):
            self._report_outage(destination, status)
            return "failed"
        # The server refused the run: find which point, one at a time, so one bad point cannot
        # cost the rest.
        for entry in run:
            single = self._post(entry.line, destination)
            if single is not True and not is_point_rejection(single):
                self._report_outage(destination, single)
                return "failed"
            if single is not True:
                self._refused(entry, single)
            done.append(entry)
        return "refused"

    def _read_chunk(self):
        """Return up to a chunk of the oldest waiting points, spool first, then memory.

        Returns:
            list: _Entry, oldest first
        """
        if self._disk or self._segments:
            chunk = self._read_spool()
            if chunk:
                return chunk
        for queue in self._memory.values():
            if queue:
                return list(queue)[:CHUNK_POINTS]
        return []

    def _read_spool(self):
        """Return up to a chunk of points from the spool, stepping past what cannot be read.

        Returns:
            list: _Entry, each carrying where it ends
        """
        while True:
            segment, offset = self._pointer
            if segment not in self._sizes:
                # The segment the pointer was in has gone - dropped by the size bound while its
                # points were out being posted - so sending carries on from the next one.
                later = [number for number in self._segments if number > segment]
                if not later:
                    return []
                self._pointer = (later[0], 0)
                continue
            if offset < self._sizes[segment]:
                try:
                    chunk = self._read_segment(segment, offset)
                except OSError as exc:
                    # A segment that cannot be read is set aside rather than retried for ever,
                    # and the rest of the spool carries on.
                    self._set_aside(segment, exc)
                    continue
                if chunk:
                    return chunk
            if segment == self._append_segment:
                return []
            self._retire(segment)

    def _read_segment(self, segment, offset):
        """Return up to a chunk of points from one segment, from an offset.

        A line that cannot be decoded is stepped past: where it comes before any point, the
        pointer is moved past it now, so it never has to point into the middle of it.

        Args:
            segment (int): the segment
            offset (int): where to start

        Returns:
            list: _Entry, each carrying where it ends

        Raises:
            OSError: where the segment cannot be read
        """
        chunk = []
        with open(self._segment_path(segment), "rb") as handle:
            handle.seek(offset)
            position = offset
            while len(chunk) < CHUNK_POINTS:
                raw = handle.readline()
                if not raw:
                    break
                position += len(raw)
                entry = self._decode(raw, segment)
                if entry is None:
                    if not chunk:
                        self._pointer = (segment, position)
                    continue
                entry.end = (segment, position)
                chunk.append(entry)
        return chunk

    def _decode(self, raw, segment):
        """Return one spool line as an entry, or None where it cannot be one.

        Args:
            raw (bytes): the line as read
            segment (int): its segment, for the message

        Returns:
            _Entry or None: the point, or None for a line cut off or unparseable
        """
        if not raw.endswith(b"\n"):
            # The last line of a segment a power cut interrupted. Expected, and not a point.
            self._problems.report(
                "truncated",
                logging.INFO,
                "Skipped a spooled point cut off mid-write in segment %d",
                segment,
                identity="truncated",
            )
            return None
        try:
            record = json.loads(raw)
            return _Entry(str(record["s"]), record.get("i"), str(record["l"]), int(record.get("r", 0)))
        except (ValueError, TypeError, KeyError) as exc:
            self._problems.report(
                "corrupt",
                logging.WARNING,
                "Skipped an unreadable spooled point in segment %d: %r",
                segment,
                exc,
                identity="corrupt",
            )
            return None

    def _consume(self, done) -> None:
        """Mark a prefix of the last chunk as dealt with.

        Args:
            done (list): _Entry, in the order they were read
        """
        if not done:
            return
        if done[0].end is None:
            # By identity, not by count. The queue is only ever trimmed from the front, but a
            # point arriving while this chunk was out may have evicted some of it for the bound
            # already, and removing len(done) from the front then took points never posted.
            queue = self._memory.get((done[0].source, done[0].instance))
            for entry in done:
                if queue and queue[0] is entry:
                    queue.popleft()
            return
        segment, offset = done[-1].end
        if segment not in self._sizes:
            # Dropped by the size bound while this chunk was being posted: the pointer was
            # already moved on past it, and must not be moved back.
            return
        self._pointer = (segment, offset)
        if offset >= self._sizes[segment] and segment != self._append_segment:
            self._retire(segment)
        self._write_pointer()

    def _retire(self, segment) -> None:
        """Delete a segment that has been sent, and move the pointer to the next.

        Args:
            segment (int): the segment
        """
        try:
            os.remove(self._segment_path(segment))
        except FileNotFoundError:
            pass
        except OSError as exc:
            logging.warning("Could not remove the sent spool segment %r: %r", self._segment_path(segment), exc)
        self._segments.remove(segment)
        del self._sizes[segment]
        self._pointer = (self._segments[0], 0) if self._segments else (segment + 1, 0)
        # Recorded now rather than at the next successful post: a run that retires segments and
        # never posts would otherwise leave the stored pointer naming a segment that has gone.
        self._write_pointer()

    def _set_aside(self, segment, exc) -> None:
        """Rename a segment that cannot be read, so the rest of the spool can carry on.

        Args:
            segment (int): the segment
            exc (OSError): why it could not be read
        """
        path = self._segment_path(segment)
        logging.warning("Setting aside the unreadable spool segment %r: %r", path, exc)
        try:
            os.replace(path, path + ".unreadable")
        except OSError:
            pass
        self._segments.remove(segment)
        del self._sizes[segment]
        self._pointer = (self._segments[0], 0) if self._segments else (segment + 1, 0)
        self._write_pointer()

    def _refused(self, entry, status) -> None:
        """Count a refusal, and put the point at the back or give up on it.

        Args:
            entry (_Entry): the refused point
            status (int): the status it was refused with
        """
        entry.rejections += 1
        if entry.rejections >= MAX_POINT_REJECTIONS:
            logging.warning(
                "Dropping an InfluxDB point for %s after %d refusals, the last with HTTP %s",
                entry.label,
                entry.rejections,
                status,
            )
            return
        with self._lock:
            self._append(_Entry(entry.source, entry.instance, entry.line, entry.rejections))

    def _report_outage(self, destination, status) -> None:
        """Say once for the outage that InfluxDB is not taking points.

        Args:
            destination (tuple): (url, kwargs) it was posting to
            status (int or str or None): the status, or what went wrong where none arrived
        """
        self._problems.report(
            "write",
            logging.ERROR,
            "InfluxDB at %s is not taking points from %s, which are kept until it does: %s",
            _without_query(destination[0]),
            self.name,
            f"HTTP {status}" if status is not None else self._last_error,
            identity="write",
        )

    def _post(self, body, destination):
        """Post a body, and say how it went.

        Args:
            body (str): newline-joined line-protocol points
            destination (tuple): (url, kwargs)

        Returns:
            True or int or None: True where it was accepted, the HTTP status where it was
            refused, and None where no response arrived - kept, verbatim, for the message
        """
        url, kwargs = destination
        try:
            with warnings.catch_warnings():
                if not kwargs.get("verify", True):
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = self._session.post(url, data=body, **kwargs)
            response.raise_for_status()
            return True
        except requests.exceptions.HTTPError as exc:
            self._last_error = repr(exc)
            return getattr(exc.response, "status_code", None)
        except requests.exceptions.RequestException as exc:
            self._last_error = repr(exc)
            return None

    # ----------------------------------------------------------------- stopping

    def close(self, seconds=CLOSE_SECONDS) -> None:
        """Give the thread a moment to post what is waiting, then stop; the rest stays spooled.

        Idempotent, and safe to call from an exit handler.

        Args:
            seconds (float): how long the thread may keep posting
        """
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            thread = self._thread
        self._wake.set()
        if thread is not None:
            thread.join(seconds)
        with self._lock:
            if self._append_file is not None:
                self._append_file.close()
                self._append_file = None
            # Anything submitted from here on is held in memory, and goes with the process.
            self._disk = False
            self._close_lock_file()
        self._session.close()


def _runs(chunk):
    """Split a chunk into runs of consecutive points bound for the same source's database.

    Args:
        chunk (list): _Entry, in order

    Returns:
        list: lists of _Entry
    """
    runs = []
    for entry in chunk:
        if runs and runs[-1][0].source == entry.source:
            runs[-1].append(entry)
        else:
            runs.append([entry])
    return runs


def _without_query(url):
    """Return a write URL without its query string, which can carry an org and a bucket name.

    Args:
        url (str): the URL

    Returns:
        str: the URL up to the query
    """
    return url.split("?", 1)[0]


# ---------------------------------------------------------------------- the process's writer

_WRITER = None
_WRITER_LOCK = threading.Lock()


def configure(name, settings, settings_file=None, spool_root=None, inline=False):
    """Create this process's writer, replacing any before it.

    Called once, early, by each entry point that writes: the service as ``main``, and each
    control process as ``control-<name>``. A process that writes without calling this gets a
    ``main`` writer on first use.

    Args:
        name (str): which process this is, and so which spool it owns
        settings (dict): the settings in effect
        settings_file (str or None): the settings path, which decides the state directory
        spool_root (str or None): where spools live; ``<state directory>/spool`` when None
        inline (bool): post from the caller's thread, for tests

    Returns:
        InfluxWriter: the writer
    """
    global _WRITER
    root = spool_root or os.path.join(resolve_state_dir(settings_file), SPOOL_DIR_NAME)
    buffer_mb = (settings.get("influx") or {}).get("buffer_mb", BUFFER_MB_DEFAULT)
    if buffer_mb_problem(settings.get("influx") or {}):
        buffer_mb = BUFFER_MB_DEFAULT
    with _WRITER_LOCK:
        previous, _WRITER = _WRITER, InfluxWriter(name, root, settings, buffer_mb=buffer_mb, inline=inline)
        writer = _WRITER
    if previous is not None:
        previous.close(0)
    atexit.register(writer.close)
    return writer


def writer_for(settings, settings_file=None):
    """Return this process's writer, creating a ``main`` one on first use.

    Args:
        settings (dict): the settings in effect, used only where a writer must be created
        settings_file (str or None): the settings path, likewise

    Returns:
        InfluxWriter: the writer
    """
    with _WRITER_LOCK:
        writer = _WRITER
    if writer is not None:
        return writer
    return configure("main", settings, settings_file)


def current():
    """Return this process's writer, or None where nothing has written yet.

    Returns:
        InfluxWriter or None: the writer
    """
    with _WRITER_LOCK:
        return _WRITER
