"""Tests for the writer: the spool, the thread that drains it, and what survives what.

Most drive an ``InfluxWriter`` directly against a scripted HTTP post, calling
``run_until_idle()`` where the service's thread would, so each step is deterministic. The last
class runs the real thread against a post that hangs, because the property that matters most -
a caller never waits on InfluxDB - only exists with a thread in it.

The scripted post raises what ``requests`` raises: ``ConnectionError`` for no connection, and a
response whose ``raise_for_status()`` raises ``HTTPError`` carrying ``response.status_code`` for
a refusal. That is the shape ``requests.Response.raise_for_status`` has in requests 2.x, which is
what ``InfluxWriter._post`` reads.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import json
import logging
import os
import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

from toinflux import writer as writer_module
from toinflux.writer import InfluxWriter, buffer_mb_problem

DESTINATION = ("http://influx/write?db=hue_db&precision=s", {"timeout": 5})


class ScriptedPost:
    """An HTTP post that answers from a script, and records every body it was sent.

    Attributes:
        bodies (list): (url, body) for every post, in order
        answer (callable): body -> True for accepted, an int status for refused, or None for
            no connection; accepts everything by default
    """

    def __init__(self):
        self.bodies = []
        self.answer = lambda body: True

    def __call__(self, url, data=None, **kwargs):
        self.bodies.append((url, data))
        outcome = self.answer(data)
        if outcome is None:
            raise requests.exceptions.ConnectionError("connection refused")
        response = MagicMock()
        if outcome is True:
            response.raise_for_status = MagicMock()
        else:
            error = requests.exceptions.HTTPError(f"{outcome} refused")
            error.response = MagicMock(status_code=outcome)
            response.raise_for_status = MagicMock(side_effect=error)
        return response

    def lines(self):
        """Return every line accepted or attempted, flattened from the bodies.

        Returns:
            list: line-protocol points
        """
        return [line for _url, body in self.bodies for line in body.split("\n")]


@pytest.fixture
def post():
    return ScriptedPost()


def _writer(tmp_path, post, name="collectors", buffer_mb=1, settings=None, **kwargs):
    """Return a writer whose posts go to ``post``, with ``hue`` pointed at DESTINATION.

    Returns:
        InfluxWriter: the writer, not yet closed
    """
    writer = InfluxWriter(name, str(tmp_path / "spool"), settings or {}, buffer_mb=buffer_mb, **kwargs)
    writer._session.post = post
    writer.set_destination("hue", *DESTINATION)
    return writer


def _spooled(writer):
    """Return every line the spool directory holds, sent or not, oldest segment first.

    Returns:
        list: line-protocol points
    """
    lines = []
    for name in sorted(os.listdir(writer.directory)):
        if name.endswith(".jsonl"):
            with open(os.path.join(writer.directory, name), encoding="utf-8") as handle:
                lines.extend(json.loads(raw)["l"] for raw in handle if raw.endswith("\n"))
    return lines


class TestAPointIsCommittedBeforeTheCallReturns:
    def test_it_is_on_disk_before_anything_is_posted(self, tmp_path, post):
        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        writer.submit("hue", None, "hue x=1 1700000000")
        assert _spooled(writer) == ["hue x=1 1700000000"]
        assert post.bodies == []
        writer.close(0)

    def test_the_spool_is_readable_only_by_its_owner(self, tmp_path, post):
        """It holds measurements, which are the owner's business, never credentials. Every
        file, the pointer included - the live run found it created 0644."""
        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        writer.submit("hue", None, "hue x=1 1700000000")
        writer.run_until_idle()
        assert os.stat(writer.directory).st_mode & 0o777 == 0o700
        # The directory every process's spool shares, as well as this one's: created by the
        # writer, it was left 0755 before the mode was applied to it explicitly.
        assert os.stat(os.path.dirname(writer.directory)).st_mode & 0o777 == 0o700
        for name in os.listdir(writer.directory):
            mode = os.stat(os.path.join(writer.directory, name)).st_mode & 0o777
            assert mode == 0o600, f"{name} is {mode:o}"
        segment = next(name for name in os.listdir(writer.directory) if name.endswith(".jsonl"))
        with open(os.path.join(writer.directory, segment), encoding="utf-8") as handle:
            assert "timeout" not in handle.read(), "a destination's settings reached the spool"
        writer.close(0)

    def test_sending_it_empties_the_backlog(self, tmp_path, post):
        writer = _writer(tmp_path, post, inline=True)
        writer.submit("hue", None, "hue x=1 1700000000")
        assert post.lines() == ["hue x=1 1700000000"]
        assert not writer.pending()
        writer.close(0)


class TestWhatStartingSays:
    def test_a_writer_says_where_it_buffers_and_how_much(self, tmp_path, post, caplog):
        with caplog.at_level(logging.INFO):
            writer = _writer(tmp_path, post, buffer_mb=7)
        assert f"are buffered on disk in {writer.directory!r}, up to influx.buffer_mb (7 MB)" in caplog.text
        assert "resuming" not in caplog.text
        writer.close(0)

    def test_it_says_when_it_is_resuming_a_backlog(self, tmp_path, post, caplog):
        first = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: None
        first.submit("hue", None, "hue x=1 1700000000")
        first.close(0)
        with caplog.at_level(logging.INFO):
            second = _writer(tmp_path, post)
        assert "resuming the unsent points already there" in caplog.text
        second.close(0)


class TestAnOutage:
    def test_points_wait_and_are_sent_in_order_once_it_ends(self, tmp_path, post):
        writer = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: None
        for value in range(3):
            writer.submit("hue", None, f"hue x={value} 170000000{value}")
        assert writer.pending()
        post.bodies.clear()
        post.answer = lambda body: True
        writer.run_until_idle()
        assert post.lines() == ["hue x=0 1700000000", "hue x=1 1700000001", "hue x=2 1700000002"]
        assert not writer.pending()
        writer.close(0)

    def test_it_is_said_once_and_its_end_is_said_once(self, tmp_path, post, caplog):
        writer = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: None
        with caplog.at_level(logging.INFO):
            for value in range(4):
                writer.submit("hue", None, f"hue x={value} 1700000000")
            post.answer = lambda body: True
            writer.run_until_idle()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, [r.getMessage() for r in errors]
        assert "is not taking points" in errors[0].getMessage()
        assert "http://influx/write" in errors[0].getMessage()
        assert "db=hue_db" not in errors[0].getMessage(), "the query string reached the log"
        assert sum("taking points from collectors again" in r.getMessage() for r in caplog.records) == 1
        writer.close(0)

    def test_the_thread_waits_for_its_timer_rather_than_trying_on_every_point(self, tmp_path, post):
        """An outage costs one attempt per retry, not one per point: a new point during it
        does not trigger a post."""
        now = [1000.0]
        writer = _writer(tmp_path, post, clock=lambda: now[0])
        writer._ensure_thread = lambda: None
        post.answer = lambda body: None
        writer.submit("hue", None, "hue x=0 1700000000")
        writer.run_until_idle(force=False)
        attempts = len(post.bodies)
        writer.submit("hue", None, "hue x=1 1700000001")
        writer.run_until_idle(force=False)
        assert len(post.bodies) == attempts, "a post was tried inside the retry wait"
        now[0] += writer_module.RETRY_FIRST_SECONDS
        writer.run_until_idle(force=False)
        assert len(post.bodies) == attempts + 1
        writer.close(0)


class TestARestart:
    """Nothing committed is lost to a restart, and not much is sent twice."""

    def test_what_was_unsent_is_sent_by_the_next_process(self, tmp_path, post):
        first = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: None
        first.submit("hue", None, "hue x=1 1700000000")
        first.close(0)
        post.answer = lambda body: True
        post.bodies.clear()
        second = _writer(tmp_path, post)
        second.run_until_idle()
        assert post.lines() == ["hue x=1 1700000000"]
        second.close(0)

    def test_sending_resumes_from_the_pointer_not_the_start(self, tmp_path, post, monkeypatch):
        """Two points sent, then InfluxDB went away: the next process sends the third only."""
        monkeypatch.setattr(writer_module, "CHUNK_POINTS", 2)
        first = _writer(tmp_path, post)
        first._ensure_thread = lambda: None
        for value in range(3):
            first.submit("hue", None, f"hue x={value} 1700000000")
        post.answer = lambda body: True if "x=2" not in body else None
        first.run_until_idle()
        first.close(0)
        post.bodies.clear()
        post.answer = lambda body: True
        second = _writer(tmp_path, post)
        second.run_until_idle()
        assert post.lines() == ["hue x=2 1700000000"]
        second.close(0)

    def test_a_lost_pointer_costs_duplicates_not_points(self, tmp_path, post, monkeypatch):
        monkeypatch.setattr(writer_module, "CHUNK_POINTS", 2)
        first = _writer(tmp_path, post)
        first._ensure_thread = lambda: None
        for value in range(3):
            first.submit("hue", None, f"hue x={value} 1700000000")
        post.answer = lambda body: True if "x=2" not in body else None
        first.run_until_idle()
        first.close(0)
        os.remove(os.path.join(first.directory, "pointer.json"))
        post.bodies.clear()
        post.answer = lambda body: True
        second = _writer(tmp_path, post)
        second.run_until_idle()
        assert sorted(post.lines()) == ["hue x=0 1700000000", "hue x=1 1700000000", "hue x=2 1700000000"]
        second.close(0)

    def test_a_segment_number_is_never_reused_under_an_old_pointer(self, tmp_path, post):
        """Found in review. A run that sent everything left the pointer naming segment 1; the
        next retired it without rewriting the pointer and left an empty segment behind; the one
        after removed that, found no segments, and numbered its own 1 again - so the old
        pointer was trusted against a new file, and the first point committed to it was
        skipped for good."""
        first = _writer(tmp_path, post, inline=True)
        first.submit("hue", None, "hue n=1 1")
        first.close(0)
        second = _writer(tmp_path, post)
        second.run_until_idle()
        second.close(0)
        post.answer = lambda body: None
        third = _writer(tmp_path, post, inline=True)
        for n in range(2, 6):
            third.submit("hue", None, f"hue n={n} {n}")
        third.close(0)
        post.answer = lambda body: True
        post.bodies.clear()
        fourth = _writer(tmp_path, post)
        fourth.run_until_idle()
        assert post.lines() == [f"hue n={n} {n}" for n in range(2, 6)]
        fourth.close(0)

    def test_a_pointer_past_the_end_of_its_segment_resumes_from_its_start(self, tmp_path, post):
        """No pointer this writer wrote can do that, so the pointer is what is wrong, and the
        cost of not trusting it is at most a segment sent twice rather than points skipped."""
        first = _writer(tmp_path, post)
        first._ensure_thread = lambda: None
        first.submit("hue", None, "hue n=1 1")
        segment = first._append_segment
        first.close(0)
        with open(os.path.join(first.directory, "pointer.json"), "w", encoding="utf-8") as handle:
            json.dump({"segment": segment, "offset": 10**6}, handle)
        second = _writer(tmp_path, post)
        second.run_until_idle()
        assert post.lines() == ["hue n=1 1"]
        second.close(0)

    def test_a_line_cut_off_by_a_power_cut_is_skipped(self, tmp_path, post):
        first = _writer(tmp_path, post)
        first._ensure_thread = lambda: None
        first.submit("hue", None, "hue x=1 1700000000")
        path = os.path.join(first.directory, f"{first._append_segment:012d}.jsonl")
        first.close(0)
        with open(path, "ab") as handle:
            handle.write(b'{"s":"hue","i":null,"l":"hue x=2 17')
        second = _writer(tmp_path, post)
        second.run_until_idle()
        assert post.lines() == ["hue x=1 1700000000"]
        second.close(0)

    def test_an_unreadable_line_is_skipped_and_said(self, tmp_path, post, caplog):
        first = _writer(tmp_path, post)
        first._ensure_thread = lambda: None
        path = os.path.join(first.directory, f"{first._append_segment:012d}.jsonl")
        first.close(0)
        with open(path, "ab") as handle:
            handle.write(b"not json\n")
            handle.write(b'{"s":"hue","i":null,"l":"hue x=2 1700000000","r":0}\n')
        with caplog.at_level(logging.WARNING):
            second = _writer(tmp_path, post)
            second.run_until_idle()
        assert post.lines() == ["hue x=2 1700000000"]
        assert "unreadable spooled point" in caplog.text
        second.close(0)

    def test_a_segment_that_cannot_be_read_is_set_aside(self, tmp_path, post, caplog):
        first = _writer(tmp_path, post)
        first._ensure_thread = lambda: None
        first.submit("hue", None, "hue x=1 1700000000")
        path = os.path.join(first.directory, f"{first._append_segment:012d}.jsonl")
        first.close(0)
        second = _writer(tmp_path, post)
        real_open = open

        def refusing(file, *args, **kwargs):
            if file == path:
                raise PermissionError("denied")
            return real_open(file, *args, **kwargs)

        with caplog.at_level(logging.WARNING):
            import builtins

            builtins.open, saved = refusing, builtins.open
            try:
                second.run_until_idle()
            finally:
                builtins.open = saved
        assert os.path.exists(path + ".unreadable")
        assert "Setting aside the unreadable spool segment" in caplog.text
        second.close(0)

    def test_the_segment_being_appended_to_can_be_set_aside_and_writing_carries_on(self, tmp_path, post, monkeypatch):
        """Found in review. Setting aside the segment appends were going to left the writer
        pointing at a segment it no longer knew the size of, and the next append raised
        KeyError out of send_data - every write after it, for the life of the process."""
        import builtins

        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        writer.submit("hue", None, "hue n=1 1")
        path = writer._segment_path(writer._append_segment)
        real = builtins.open

        def refusing(file, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if file == path and "r" in mode:
                raise PermissionError("denied")
            return real(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", refusing)
        writer.run_until_idle()
        monkeypatch.setattr(builtins, "open", real)
        assert os.path.exists(path + ".unreadable")
        post.bodies.clear()
        writer.submit("hue", None, "hue n=2 2")
        writer.run_until_idle()
        assert post.lines() == ["hue n=2 2"]
        writer.close(0)

    def test_and_where_no_new_segment_can_be_made_writing_carries_on_in_memory(self, tmp_path, post, monkeypatch):
        """Found in review, after the fix above. Where the fresh segment could not be created
        either, only the disk flag was cleared: the removed segment stayed named and the lock
        held, so the next append tried the disk again and raised the same KeyError."""
        import builtins

        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        writer.submit("hue", None, "hue n=1 1")
        path = writer._segment_path(writer._append_segment)
        real_open, real_os_open = builtins.open, writer_module.os.open

        def refusing(file, *args, **kwargs):
            if file == path:
                raise PermissionError("denied")
            return real_open(file, *args, **kwargs)

        def no_new_segment(file, *args, **kwargs):
            if str(file).endswith(".jsonl"):
                raise OSError(5, "Input/output error")
            return real_os_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", refusing)
        monkeypatch.setattr(writer_module.os, "open", no_new_segment)
        writer.run_until_idle()
        monkeypatch.undo()
        post.bodies.clear()
        writer.submit("hue", None, "hue n=2 2")
        writer.run_until_idle()
        assert not writer._disk
        assert post.lines() == ["hue n=2 2"]
        writer.close(0)

    def test_a_failed_write_leaves_the_file_and_its_size_in_agreement(self, tmp_path, post, monkeypatch):
        """Found in review. A failed sync left its bytes in the file uncounted, so the reader
        ran ahead of the size, and a point appended afterwards could read as already sent and
        be retired with its segment unsent. The failed write is cut back off, and each point is
        sent once."""
        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        real_fsync = writer_module.os.fsync
        calls = []

        def flaky(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError(5, "Input/output error")
            return real_fsync(descriptor)

        monkeypatch.setattr(writer_module.os, "fsync", flaky)
        for n in range(1, 4):
            writer.submit("hue", None, f"hue n={n} {n}")
        monkeypatch.undo()
        for segment in writer._segments:
            assert writer._sizes[segment] == os.path.getsize(writer._segment_path(segment)), f"segment {segment}"
        writer.run_until_idle()
        assert post.lines() == ["hue n=1 1", "hue n=2 2", "hue n=3 3"]
        writer.close(0)


class TestRefusals:
    """Carried over from the in-memory buffer: only the server refusing the point counts."""

    @pytest.mark.parametrize("status", [None, 408, 429, 500, 503])
    def test_what_says_nothing_about_the_point_never_counts(self, tmp_path, post, status):
        writer = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: status
        writer.submit("hue", None, "hue x=1 1700000000")
        for _ in range(writer_module.MAX_POINT_REJECTIONS + 2):
            writer.run_until_idle()
        post.answer = lambda body: True
        writer.run_until_idle()
        assert post.lines()[-1] == "hue x=1 1700000000"
        writer.close(0)

    def test_a_refused_point_does_not_hold_up_the_rest(self, tmp_path, post):
        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        for value in range(3):
            writer.submit("hue", None, f"hue x={value} 1700000000")
        post.answer = lambda body: 400 if "x=0" in body else True
        writer.run_until_idle()
        accepted = [body for (_url, body), in zip(post.bodies) if "x=0" not in body]
        assert "hue x=1 1700000000" in accepted and "hue x=2 1700000000" in accepted
        writer.close(0)

    def test_a_refusal_is_charged_once_per_attempt_however_many_points_arrive(self, tmp_path, post):
        """Five locks' points arriving during a refusal must not spend a waiting point's five
        attempts at once: the attempts are five *separate* ones, so a middlebox answering 4xx
        for a briefly-down InfluxDB cannot discard the backlog. The in-memory buffer met this by
        flushing once per cycle rather than once per lock; the writer meets it with its timer."""
        now = [1000.0]
        writer = _writer(tmp_path, post, clock=lambda: now[0])
        writer._ensure_thread = lambda: None
        writer.submit("nuki", None, "nuki,device=Gate stateValue=1 1700000000")
        writer.set_destination("nuki", *DESTINATION)
        post.answer = lambda body: 400
        writer.run_until_idle(force=False)
        for lock in range(5):
            writer.submit("nuki", None, f"nuki,device=Lock{lock} stateValue=1 1700000000")
            writer.run_until_idle(force=False)
        with writer._lock:
            waiting = writer._read_spool()
        charged = [entry.rejections for entry in waiting if "Gate" in entry.line]
        assert charged == [1], f"the waiting point was charged {charged} for one attempt"
        writer.close(0)

    @pytest.mark.parametrize("status", [400, 404, 422])
    def test_a_point_refused_five_times_is_dropped_and_said(self, tmp_path, post, status, caplog):
        writer = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: status
        with caplog.at_level(logging.WARNING):
            writer.submit("hue", None, "hue x=1 1700000000")
            for _ in range(writer_module.MAX_POINT_REJECTIONS):
                writer.run_until_idle()
        assert not writer.pending()
        assert sum("after 5 refusals" in r.getMessage() for r in caplog.records) == 1
        assert f"HTTP {status}" in caplog.text
        writer.close(0)


class TestTheBound:
    def test_the_oldest_points_go_first_and_it_is_said(self, tmp_path, post, monkeypatch, caplog):
        monkeypatch.setattr(writer_module, "SEGMENT_BYTES", 200)
        writer = _writer(tmp_path, post, inline=True)
        writer.limit_bytes = 600
        post.answer = lambda body: None
        with caplog.at_level(logging.WARNING):
            for value in range(20):
                writer.submit("hue", None, f"hue x={value} 1700000000")
        post.answer = lambda body: True
        post.bodies.clear()
        writer.run_until_idle()
        sent = post.lines()
        assert "hue x=19 1700000000" in sent, "the newest point was not kept"
        assert "hue x=0 1700000000" not in sent, "the oldest point was not dropped"
        assert sum("is full at influx.buffer_mb" in r.getMessage() for r in caplog.records) == 1
        writer.close(0)

    @pytest.mark.parametrize("value", [0, 1, 100, 1024])
    def test_the_setting_accepts_its_range(self, value):
        assert buffer_mb_problem({"buffer_mb": value}) is None

    @pytest.mark.parametrize("value", [-1, 1025, 1.5, "100", True, None])
    def test_the_setting_refuses_anything_else_and_names_itself(self, value):
        assert buffer_mb_problem({"buffer_mb": value}).startswith("influx.buffer_mb must be")

    def test_absent_is_the_default(self):
        assert buffer_mb_problem({}) is None

    def test_validation_reports_it(self):
        from toinflux.general import _validate_influx_block

        assert any(
            "influx.buffer_mb" in error
            for error in _validate_influx_block({"url": "u", "user": "a", "password": "b", "buffer_mb": 2048})
        )


class TestMemoryOnly:
    def test_zero_holds_points_in_memory_and_says_so_at_startup(self, tmp_path, post, caplog):
        with caplog.at_level(logging.INFO):
            writer = _writer(tmp_path, post, buffer_mb=0, inline=True)
        assert "held in memory only (influx.buffer_mb is 0)" in caplog.text
        post.answer = lambda body: None
        writer.submit("hue", None, "hue x=1 1700000000")
        assert not os.path.exists(writer.directory), "memory mode created a spool"
        post.answer = lambda body: True
        writer.run_until_idle()
        assert post.lines()[-1] == "hue x=1 1700000000"
        writer.close(0)

    def test_it_is_bounded_per_worker(self, tmp_path, post, monkeypatch, caplog):
        monkeypatch.setattr(writer_module, "MEMORY_POINTS_PER_WORKER", 3)
        writer = _writer(tmp_path, post, buffer_mb=0, inline=True)
        post.answer = lambda body: None
        with caplog.at_level(logging.WARNING):
            for value in range(5):
                writer.submit("hue", None, f"hue x={value} 1700000000")
        post.answer = lambda body: True
        post.bodies.clear()
        writer.run_until_idle()
        assert post.lines() == ["hue x=2 1700000000", "hue x=3 1700000000", "hue x=4 1700000000"]
        assert "are being dropped, oldest first" in caplog.text
        writer.close(0)

    def test_a_point_arriving_while_a_chunk_is_out_is_not_lost(self, tmp_path, post, monkeypatch):
        """Found in review. At the bound, the new point evicted one already in the chunk being
        posted; counting the chunk off the front afterwards then removed the new point, which
        had never been sent."""
        monkeypatch.setattr(writer_module, "MEMORY_POINTS_PER_WORKER", 3)
        writer = _writer(tmp_path, post, buffer_mb=0)
        writer._ensure_thread = lambda: None
        for n in (1, 2, 3):
            writer.submit("hue", None, f"hue n={n} {n}")

        def arriving(body):
            if len(post.bodies) == 1:
                writer.submit("hue", None, "hue n=4 4")
            return True

        post.answer = arriving
        writer.run_until_idle()
        assert "hue n=4 4" in post.lines()
        assert not writer.pending()
        writer.close(0)

    def test_it_does_not_survive_a_restart_as_documented(self, tmp_path, post):
        first = _writer(tmp_path, post, buffer_mb=0, inline=True)
        post.answer = lambda body: None
        first.submit("hue", None, "hue x=1 1700000000")
        first.close(0)
        second = _writer(tmp_path, post, buffer_mb=0)
        assert not second.pending()
        second.close(0)


class TestADiskThatFails:
    def test_points_go_to_memory_and_it_is_said_once(self, tmp_path, post, caplog):
        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        real = writer._write_entry

        def failing(entry):
            raise OSError(28, "No space left on device")

        writer._write_entry = failing
        with caplog.at_level(logging.WARNING):
            writer.submit("hue", None, "hue x=1 1700000000")
            writer.submit("hue", None, "hue x=2 1700000000")
        assert sum("held in memory until they can be sent" in r.getMessage() for r in caplog.records) == 1
        assert "No space left on device" in caplog.text
        writer._write_entry = real
        writer.run_until_idle()
        assert post.lines() == ["hue x=1 1700000000", "hue x=2 1700000000"]
        writer.close(0)

    def test_when_it_recovers_memory_goes_into_the_spool_first(self, tmp_path, post, caplog):
        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        real = writer._write_entry
        writer._write_entry = MagicMock(side_effect=OSError(5, "Input/output error"))
        writer.submit("hue", None, "hue x=1 1700000000")
        writer._write_entry = real
        with caplog.at_level(logging.INFO):
            writer.submit("hue", None, "hue x=2 1700000000")
        assert _spooled(writer) == ["hue x=1 1700000000", "hue x=2 1700000000"]
        assert "being spooled to disk again" in caplog.text
        writer.close(0)


class TestASpoolThatCannotBeOpened:
    def test_a_full_disk_at_startup_falls_back_to_memory(self, tmp_path, post, monkeypatch, caplog):
        """Found in review. Only the directory and the lock were under the fallback, so a disk
        that was full when the first segment was created raised out of the constructor, and
        the service would not start at all."""
        real = os.open

        def full(path, *args, **kwargs):
            if str(path).endswith(".jsonl"):
                raise OSError(28, "No space left on device")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(writer_module.os, "open", full)
        with caplog.at_level(logging.WARNING):
            writer = _writer(tmp_path, post, inline=True)
        assert not writer._disk
        assert "could not be opened" in caplog.text and "No space left on device" in caplog.text
        monkeypatch.setattr(writer_module.os, "open", real)
        writer.submit("hue", None, "hue n=1 1")
        assert post.lines() == ["hue n=1 1"]
        writer.close(0)


class TestConfiguringAgain:
    def test_a_second_configure_with_the_same_name_keeps_the_spool(self, tmp_path, caplog):
        """Found in review. The new writer was built while the old one still held the spool's
        lock, so it said another process had the spool and fell back to memory."""
        root = str(tmp_path / "spool")
        writer_module.configure("main", {}, spool_root=root)
        with caplog.at_level(logging.WARNING):
            second = writer_module.configure("main", {}, spool_root=root)
        assert second._disk
        assert "another process is using the spool" not in caplog.text


class TestOneSpoolPerProcess:
    def test_a_second_process_on_the_same_spool_uses_memory_instead(self, tmp_path, post, caplog):
        """A manual run beside the service would otherwise delete the service's segments."""
        first = _writer(tmp_path, post)
        with caplog.at_level(logging.WARNING):
            second = _writer(tmp_path, post)
        assert "another process is using the spool" in caplog.text
        assert not second._disk
        second.close(0)
        first.close(0)


class TestWhereAPointGoes:
    def test_the_destination_is_the_one_current_when_it_is_sent(self, tmp_path, post):
        """A database rebuilt under a new name gets the backlog too, not only new points."""
        writer = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: None
        writer.submit("hue", None, "hue x=1 1700000000")
        writer.set_destination("hue", "http://influx/write?db=renamed&precision=s", {})
        post.answer = lambda body: True
        post.bodies.clear()
        writer.run_until_idle()
        assert post.bodies == [("http://influx/write?db=renamed&precision=s", "hue x=1 1700000000")]
        writer.close(0)

    def test_a_restarted_writer_finds_destinations_in_the_settings(self, tmp_path, post):
        first = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: None
        first.submit("hue", None, "hue x=1 1700000000")
        first.close(0)
        settings = {"influx": {"url": "http://influx", "user": "u", "password": "p"}, "hue": {"db": "hue_db"}}
        second = InfluxWriter("collectors", str(tmp_path / "spool"), settings, buffer_mb=1)
        second._session.post = post
        post.answer = lambda body: True
        post.bodies.clear()
        second.run_until_idle()
        assert post.bodies == [("http://influx/write?db=hue_db&precision=s", "hue x=1 1700000000")]
        second.close(0)

    def test_a_point_for_a_source_no_longer_configured_is_dropped_and_said(self, tmp_path, post, caplog):
        writer = _writer(tmp_path, post, inline=True)
        with caplog.at_level(logging.WARNING):
            writer.submit("octopus", None, "octopus x=1 1700000000")
        assert not writer.pending()
        assert "Dropping unsent InfluxDB points for 'octopus'" in caplog.text
        writer.close(0)


class TestRepeatsAndLiveSignals:
    def test_an_identical_reading_is_not_spooled_twice(self, tmp_path, post):
        """Octopus re-serves one reading, timestamp and all, for about half an hour."""
        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        for _ in range(3):
            writer.submit("octopus", None, "octopus x=1 1700000000")
        writer.submit("octopus", None, "octopus x=2 1700001800")
        assert _spooled(writer) == ["octopus x=1 1700000000", "octopus x=2 1700001800"]
        writer.close(0)

    def test_a_live_signal_is_never_spooled(self, tmp_path, post):
        writer = _writer(tmp_path, post, inline=True)
        writer.submit("hue", None, "collector_status,source=hue ok=1 1700000000", buffered=False)
        assert post.lines() == ["collector_status,source=hue ok=1 1700000000"]
        assert _spooled(writer) == []
        writer.close(0)

    def test_live_signals_are_not_posted_while_waiting_on_the_retry_timer(self, tmp_path, post):
        """Found in review. They were posted before the timer was looked at, so during an outage
        every heartbeat cost the whole of influx.timeout against a server that drops
        connections. They are dropped instead, and posted again once the timer has run."""
        now = [1000.0]
        writer = _writer(tmp_path, post, clock=lambda: now[0])
        writer._ensure_thread = lambda: None
        post.answer = lambda body: None
        writer.submit("hue", None, "hue x=1 1700000000")
        writer.run_until_idle(force=False)
        attempts = len(post.bodies)
        for n in range(3):
            writer.submit("hue", None, f"collector_status,source=hue ok=1 {n}", buffered=False)
            writer.run_until_idle(force=False)
        assert len(post.bodies) == attempts, "a live point was posted inside the retry wait"
        now[0] += writer_module.RETRY_FIRST_SECONDS
        post.answer = lambda body: True
        post.bodies.clear()
        writer.submit("hue", None, "collector_status,source=hue ok=1 9", buffered=False)
        writer.run_until_idle(force=False)
        assert "collector_status,source=hue ok=1 9" in post.lines()
        writer.close(0)

    def test_a_live_post_that_fails_starts_the_retry_timer(self, tmp_path, post):
        """With no backlog, a heartbeat is the only thing that discovers an outage; without
        this, each one after it paid the full timeout too."""
        now = [1000.0]
        writer = _writer(tmp_path, post, clock=lambda: now[0])
        writer._ensure_thread = lambda: None
        post.answer = lambda body: None
        writer.submit("hue", None, "collector_status,source=hue ok=1 1", buffered=False)
        writer.run_until_idle(force=False)
        writer.submit("hue", None, "collector_status,source=hue ok=1 2", buffered=False)
        writer.run_until_idle(force=False)
        assert len(post.bodies) == 1
        writer.close(0)

    def test_a_live_signal_goes_ahead_of_a_backlog(self, tmp_path, post):
        """Behind the backlog it would be posted after it, however long that took, and arrive
        describing the past."""
        writer = _writer(tmp_path, post)
        writer._ensure_thread = lambda: None
        for n in range(5):
            writer.submit("hue", None, f"hue n={n} {n}")
        writer.submit("hue", None, "collector_status,source=hue ok=1 9", buffered=False)
        writer.run_until_idle()
        assert post.bodies[0][1] == "collector_status,source=hue ok=1 9"
        assert post.lines()[1:] == [f"hue n={n} {n}" for n in range(5)]
        writer.close(0)

    def test_a_live_post_that_succeeds_clears_the_back_off(self, tmp_path, post):
        """The same evidence as a data post that succeeds, and handled by the same code: with
        no backlog, a heartbeat was the only thing that could show InfluxDB was back, and the
        back-off an outage had grown stayed grown."""
        now = [1000.0]
        writer = _writer(tmp_path, post, clock=lambda: now[0])
        writer._ensure_thread = lambda: None
        post.answer = lambda body: None
        for _ in range(3):
            writer.submit("hue", None, "collector_status,source=hue ok=1 1", buffered=False)
            writer.run_until_idle(force=True)
        assert writer._retry_delay > writer_module.RETRY_FIRST_SECONDS
        post.answer = lambda body: True
        now[0] = writer._retry_at
        writer.submit("hue", None, "collector_status,source=hue ok=1 2", buffered=False)
        writer.run_until_idle(force=False)
        assert writer._retry_delay == writer_module.RETRY_FIRST_SECONDS
        assert writer._retry_at == 0.0
        writer.close(0)

    def test_a_refused_live_signal_is_not_kept_or_counted(self, tmp_path, post):
        writer = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: 400
        writer.submit("hue", None, "collector_status,source=hue ok=1 1", buffered=False)
        assert not writer.pending()
        assert _spooled(writer) == []
        writer.close(0)

    def test_a_live_signal_that_cannot_be_sent_is_dropped_not_kept(self, tmp_path, post):
        writer = _writer(tmp_path, post, inline=True)
        post.answer = lambda body: None
        writer.submit("hue", None, "collector_status,source=hue ok=1 1700000000", buffered=False)
        post.answer = lambda body: True
        post.bodies.clear()
        writer.run_until_idle()
        assert post.bodies == []
        writer.close(0)


class TestNobodyWaits:
    """The point of the whole design, with the real thread running."""

    def test_a_caller_returns_while_influxdb_hangs(self, tmp_path):
        release = threading.Event()
        arrived = threading.Event()

        def hanging(url, data=None, **kwargs):
            arrived.set()
            release.wait(10)
            response = MagicMock()
            response.raise_for_status = MagicMock()
            return response

        writer = InfluxWriter("collectors", str(tmp_path / "spool"), {}, buffer_mb=1)
        writer._session.post = hanging
        writer.set_destination("hue", *DESTINATION)
        started = time.monotonic()
        writer.submit("hue", None, "hue x=1 1700000000")
        assert arrived.wait(5), "the thread never posted"
        writer.submit("hue", None, "hue x=2 1700000000")
        assert time.monotonic() - started < 1, "a caller waited on a hanging InfluxDB"
        release.set()
        deadline = time.monotonic() + 5
        while writer.pending() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not writer.pending()
        writer.close(1)

    @pytest.mark.parametrize("buffer_mb", [1, 0])
    def test_closing_posts_what_is_waiting(self, tmp_path, buffer_mb):
        """Found in review. close() stopped the thread before it could post, so points waiting
        on the retry timer were not tried at all - spooled for the next start on disk, but lost
        outright in memory, where the process's exit is the end of them."""
        writer = InfluxWriter("collectors", str(tmp_path / "spool"), {}, buffer_mb=buffer_mb)
        writer.set_destination("hue", *DESTINATION)
        writer._session.post = MagicMock(side_effect=requests.exceptions.ConnectionError("down"))
        writer.submit("hue", None, "hue x=1 1700000000")
        deadline = time.monotonic() + 5
        while writer._session.post.call_count == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        accepted = ScriptedPost()
        writer._session.post = accepted
        writer.close(3)
        assert accepted.lines() == ["hue x=1 1700000000"]

    def test_an_unexpected_error_does_not_end_the_thread(self, tmp_path, caplog, monkeypatch):
        """Found in review. An exception reaching the top of the thread ended it, and every
        later point waited on disk until the process restarted. It is a bug, so it is said in
        full at ERROR - and the writer carries on."""
        monkeypatch.setattr(writer_module, "RETRY_FIRST_SECONDS", 0.2)
        writer = InfluxWriter("collectors", str(tmp_path / "spool"), {}, buffer_mb=1)
        writer.set_destination("hue", *DESTINATION)
        accepted = ScriptedPost()
        writer._session.post = accepted
        real = writer._post_next_chunk
        failures = []

        def once():
            if not failures:
                failures.append(True)
                raise ValueError("a bug in the writer")
            return real()

        writer._post_next_chunk = once
        with caplog.at_level(logging.ERROR):
            writer.submit("hue", None, "hue x=1 1700000000")
            deadline = time.monotonic() + 5
            while writer.pending() and time.monotonic() < deadline:
                time.sleep(0.05)
        assert accepted.lines() == ["hue x=1 1700000000"], "the point was never sent after the error"
        assert writer._thread.is_alive()
        assert "hit an unexpected error" in caplog.text and "ValueError: a bug in the writer" in caplog.text
        writer.close(1)

    def test_a_thread_that_has_died_is_started_again(self, tmp_path):
        writer = InfluxWriter("collectors", str(tmp_path / "spool"), {}, buffer_mb=1)
        writer.set_destination("hue", *DESTINATION)
        accepted = ScriptedPost()
        writer._session.post = accepted
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        writer._thread = dead
        writer.submit("hue", None, "hue x=1 1700000000")
        deadline = time.monotonic() + 5
        while writer.pending() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert accepted.lines() == ["hue x=1 1700000000"]
        writer.close(1)

    def test_stopping_does_not_wait_longer_than_its_deadline(self, tmp_path):
        release = threading.Event()

        def hanging(url, data=None, **kwargs):
            release.wait(10)
            raise requests.exceptions.ConnectionError("gave up")

        writer = InfluxWriter("collectors", str(tmp_path / "spool"), {}, buffer_mb=1)
        writer._session.post = hanging
        writer.set_destination("hue", *DESTINATION)
        writer.submit("hue", None, "hue x=1 1700000000")
        started = time.monotonic()
        writer.close(0.5)
        assert time.monotonic() - started < 2
        release.set()
        assert _spooled(writer) == ["hue x=1 1700000000"], "an unsent point did not stay spooled"
