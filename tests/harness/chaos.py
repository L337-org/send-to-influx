"""The chaos driver: many controls, seeded random faults, invariants checked on a tick.

The static scenarios and this differ only in the driver. There, two controls and one
scripted fault each; here, a run of faults chosen at random from the same injector, against
the same invariants. Nothing about the system under test changes, which is the point - a
scenario that needed its own special build would be testing that build.

**Seeded, and the seed is printed on failure.** A run that breaks something is worth nothing
if it cannot be repeated, so every choice comes from one seeded generator and any failure
carries the seed it came from. A seed that has failed once is added to
:data:`SEEDS_THAT_FAILED` and runs on every suite from then on - which is how a chaos run
becomes a permanent regression test rather than an anecdote about a Tuesday.

**What can be checked while a fault is running is not everything.** A control cannot make
its devices safe through a bridge that is refusing connections, so "nothing is left
energised" is a question for the end of the run, once the faults are cleared and the system
has been allowed to settle. Checking it mid-fault would be asserting that an outage does not
happen. What *is* true throughout: every state a control settles into is one its own ladder
declares, and a control still trying against a far end that will not answer has not stalled.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import random
import time
from dataclasses import dataclass, field

from tests.harness import faults, invariants

#: Seeds that have failed a chaos run. Add one here when a run fails, with a comment saying
#: what it found, and it runs from then on whether or not anybody schedules a chaos job. An
#: empty tuple is the honest state before the first failure rather than a placeholder.
SEEDS_THAT_FAILED = ()


@dataclass
class Tick:
    """What the driver did on one tick, so a failure can say what led to it.

    Attributes:
        number (int): which tick this was.
        action (str): what was done - a fault started, a fault cleared, a control killed.
        detail (str): what it was done to.
    """

    number: int
    action: str
    detail: str = ""


@dataclass
class Run:
    """The record of one chaos run.

    Attributes:
        seed (int): what produced every choice in it.
        ticks (list): what happened, in order.
    """

    seed: int
    ticks: list = field(default_factory=list)

    def story(self):
        """Return the run as something readable in a failure message.

        Returns:
            str: one line per tick
        """
        return "\n  ".join(f"tick {tick.number}: {tick.action} {tick.detail}".rstrip() for tick in self.ticks)


class ChaosDriver:
    """Runs a supervised set of controls through randomly chosen faults.

    Carries ``run`` (the record so far) and the endpoints it breaks.
    """

    def __init__(self, seed, supervisor, bridge, influx, poll=None):
        """Prepare a driver over a supervisor that has not been started.

        Args:
            seed (int): the seed for every choice this driver makes
            supervisor (Supervisor): the supervisor to drive
            bridge (StubBridge): the devices' far end, to break
            influx (StubInflux): the readings' far end, to break
            poll (callable or None): how to advance the supervisor, for a test that wants
                to watch; ``supervisor.poll`` by default
        """
        self.seed = seed
        self.random = random.Random(seed)
        self.supervisor = supervisor
        self.bridge = bridge
        self.influx = influx
        self.run = Run(seed=seed)
        self._poll = poll or supervisor.poll
        self._active = None

    def _choose(self, tick):
        """Start a fault, clear one, or kill a control, at random.

        Args:
            tick (int): which tick this is

        Returns:
            contextlib.AbstractContextManager or None: a fault that has been entered, or
            None where this tick did something else
        """
        if self._active is not None and self.random.random() < 0.4:
            self._active.__exit__(None, None, None)
            self._active = None
            self.run.ticks.append(Tick(tick, "cleared the fault"))
            return None
        if self._active is None:
            choice = self.random.choice(
                [
                    ("the bridge is unreachable", lambda: faults.unreachable(self.bridge)),
                    ("the database is unreachable", lambda: faults.unreachable(self.influx)),
                    ("the bridge is slow", lambda: faults.hanging(self.bridge, 0.3)),
                    ("the database is erroring", lambda: faults.erroring(self.influx, 503)),
                    ("the readings are frozen", lambda: faults.frozen(self.influx)),
                    ("nothing", None),
                ]
            )
            if choice[1] is not None:
                self._active = choice[1]()
                self._active.__enter__()
                self.run.ticks.append(Tick(tick, "started a fault:", choice[0]))
                return self._active
            self.run.ticks.append(Tick(tick, "left everything alone"))
            return None
        alive = [child for child in self.supervisor.children.values() if child.running]
        if alive and self.random.random() < 0.3:
            victim = self.random.choice(alive)
            victim.process.kill()
            self.run.ticks.append(Tick(tick, "killed", victim.name))
        return None

    def tick(self, number, documents) -> None:
        """Advance one tick: choose something to do, poll, and check what must hold.

        Args:
            number (int): which tick this is
            documents (dict): control name to its document, for the state invariant

        Raises:
            AssertionError: an invariant that holds under a fault was violated
        """
        self._choose(number)
        self._poll(timeout=0.2)
        reports = [
            invariants.states_were_declared(self.bridge, document, settle=1.0) for document in documents.values()
        ]
        broken = [f"{report.name}: {violation}" for report in reports for violation in report.violations]
        if broken:
            # The seed and the story, because a chaos failure that cannot be repeated is an
            # anecdote. Raised here rather than collected, so the run stops at the tick that
            # broke it and the endpoints still hold the evidence.
            raise AssertionError(
                f"chaos run with seed {self.seed} violated an invariant on tick {number}:\n  "
                + "\n  ".join(broken)
                + f"\n\nWhat led to it:\n  {self.run.story()}\n\n"
                f"Add {self.seed} to SEEDS_THAT_FAILED so this runs from now on."
            )

    def settle(self, seconds=2.0) -> None:
        """Clear any fault and let the system reach a state worth judging.

        Args:
            seconds (float): how long to keep polling after the faults are cleared
        """
        if self._active is not None:
            self._active.__exit__(None, None, None)
            self._active = None
            self.run.ticks.append(Tick(len(self.run.ticks), "cleared the fault to settle"))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._poll(timeout=0.1)
