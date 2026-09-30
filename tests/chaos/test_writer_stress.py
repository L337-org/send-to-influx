"""The writer under load and random outages, with its real thread running.

Several threads submit data and heartbeats at once while the post fails and recovers at random,
the way a busy install sees an unreliable InfluxDB. One more source writes to a database that
refuses every point, as a missing one does, and must hold up none of the others. What is checked
is what matters and can be lost silently: every data point arrives, no heartbeat is posted long
after it was submitted, the backlog drains in batches rather than a point at a time, and the
writer's thread never dies.

Seeded like the chaos run: the seed is printed, and ``CHAOS_SEED`` repeats it.
``WRITER_STRESS_SECONDS`` sets how long it submits for - short on a pull request, long nightly
(see ``.github/workflows/chaos.yaml``).
"""

import logging
import os
import random
import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

from toinflux import writer as writer_module
from toinflux.writer import InfluxWriter

pytestmark = pytest.mark.chaos

SOURCES = ("hue", "octopus", "nuki", "zappi")

#: The longest a heartbeat may reach the post after being submitted. Generous: it has to wait for
#: the post in flight and the one chunk after it, never for the backlog.
STALE_AFTER_SECONDS = 3.0


class _UnreliableInflux:
    """A post that takes a little while, and fails for as long as ``down`` is set.

    Attributes:
        down (threading.Event): InfluxDB is unreachable while set
        data (set): every data line accepted
        stale (list): how late each heartbeat posted after STALE_AFTER_SECONDS was
        posts (list): points per accepted post
    """

    def __init__(self, rng):
        self._rng = rng
        self._lock = threading.Lock()
        self.down = threading.Event()
        self.data, self.stale, self.posts = set(), [], []

    def __call__(self, url, data=None, **kwargs):
        with self._lock:
            delay = self._rng.uniform(0, 0.01)
        time.sleep(delay)
        if self.down.is_set():
            raise requests.exceptions.ConnectionError("down")
        if "db=refused" in url:
            response = MagicMock()
            error = requests.exceptions.HTTPError("404 database not found")
            error.response = MagicMock(status_code=404)
            response.raise_for_status = MagicMock(side_effect=error)
            return response
        now = time.monotonic()
        lines = data.split("\n")
        with self._lock:
            self.posts.append(len(lines))
            for line in lines:
                if line.startswith("heartbeat,"):
                    submitted = float(line.split("t=")[1].split()[0])
                    if now - submitted > STALE_AFTER_SECONDS:
                        self.stale.append(now - submitted)
                else:
                    self.data.add(line)
        response = MagicMock()
        response.raise_for_status = MagicMock()
        return response


def test_the_writer_under_load_and_random_outages(tmp_path, monkeypatch, caplog):
    seed = int(os.environ.get("CHAOS_SEED") or random.randrange(2**32))
    seconds = float(os.environ.get("WRITER_STRESS_SECONDS") or 10)
    print(f"writer stress seed: {seed}, {seconds:.0f}s")
    rng = random.Random(seed)
    # Fast retries, so a run of seconds sees many outages and many recoveries.
    monkeypatch.setattr(writer_module, "RETRY_FIRST_SECONDS", 0.1)
    monkeypatch.setattr(writer_module, "RETRY_MAX_SECONDS", 0.5)
    influx = _UnreliableInflux(random.Random(seed + 1))
    writer = InfluxWriter("stress", str(tmp_path / "spool"), {}, buffer_mb=100)
    writer._session.post = influx
    for source in SOURCES + ("refused",):
        writer.set_destination(source, f"http://influx/write?db={source}", {})
    died = []
    monkeypatch.setattr(threading, "excepthook", lambda args: died.append(repr(args.exc_value)))
    stop = time.monotonic() + seconds
    submitted = []
    submitted_lock = threading.Lock()

    def feed(source, feed_rng):
        n = 0
        while time.monotonic() < stop:
            line = f"{source} n={n}i {n}"
            writer.submit(source, None, line)
            with submitted_lock:
                submitted.append(line)
            if n % 10 == 0:
                writer.submit(source, None, f"heartbeat,source={source} t={time.monotonic()} {n}", buffered=False)
            n += 1
            time.sleep(feed_rng.uniform(0, 0.005))

    def outages():
        while time.monotonic() < stop:
            time.sleep(rng.uniform(0.3, 2.0))
            influx.down.set()
            time.sleep(rng.uniform(0.1, 1.5))
            influx.down.clear()

    threads = [
        threading.Thread(target=feed, args=(source, random.Random(seed + i + 2))) for i, source in enumerate(SOURCES)
    ]
    threads.append(threading.Thread(target=_feed_refused, args=(writer, stop)))
    threads.append(threading.Thread(target=outages))
    with caplog.at_level(logging.ERROR):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        influx.down.clear()
        drain_from = len(influx.posts)
        deadline = time.monotonic() + max(60.0, seconds)
        while writer.pending() and time.monotonic() < deadline:
            time.sleep(0.05)
        alive = writer._thread.is_alive()
        writer.close(2)

    _assert_healthy(seed, died, alive, caplog, submitted, influx, drain_from)


def _feed_refused(writer, stop) -> None:
    """Submit points to the database that refuses every one, until the run ends.

    Args:
        writer (InfluxWriter): the writer under test
        stop (float): the monotonic time to stop at
    """
    n = 0
    while time.monotonic() < stop:
        writer.submit("refused", None, f"refused n={n}i {n}")
        n += 1
        time.sleep(0.05)


def _assert_healthy(seed, died, alive, caplog, submitted, influx, drain_from) -> None:
    """Fail, naming the seed, on anything that went wrong in the run.

    Args:
        seed (int): the run's seed, so a failure can be repeated
        died (list): exceptions that ended a thread
        alive (bool): whether the writer's thread was still running at the end
        caplog (LogCaptureFixture): the run's log
        submitted (list): every data line submitted
        influx (_UnreliableInflux): what the post accepted
        drain_from (int): where in ``influx.posts`` the drain after the run began
    """
    assert not died, f"seed {seed}: the writer's thread died: {died}"
    assert alive, f"seed {seed}: the writer's thread is not running"
    bugs = [record.getMessage() for record in caplog.records if "this is a bug" in record.getMessage()]
    assert not bugs, f"seed {seed}: {bugs[0]}"
    missing = set(submitted) - influx.data
    assert not missing, f"seed {seed}: {len(missing)} of {len(submitted)} data points never arrived"
    assert (
        not influx.stale
    ), f"seed {seed}: {len(influx.stale)} heartbeat(s) posted late, the worst {max(influx.stale):.1f}s"
    drain = influx.posts[drain_from:]
    if drain:
        assert (
            sum(drain) / len(drain) >= 10
        ), f"seed {seed}: the drain averaged {sum(drain) / len(drain):.1f} points a post"
