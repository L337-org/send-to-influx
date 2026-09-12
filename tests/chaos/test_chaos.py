"""Many controls, seeded random faults, and the same invariants the static scenarios use.

Excluded from the default run and scheduled instead, because a run long enough to be worth
anything is minutes rather than seconds. What is *not* excluded is any seed that has failed:
those live in `SEEDS_THAT_FAILED` and run with the ordinary suite from then on, which is how
a chaos failure becomes a permanent regression test instead of an anecdote.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import os
import random
import sys
import warnings

import pytest

from tests.harness import census, invariants
from tests.harness.bridge import plug
from tests.harness.chaos import SEEDS_THAT_FAILED, ChaosDriver
from tests.harness.installation import conservatory
from toinflux.supervision import Supervisor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: How many controls a chaos run drives. More than the static scenarios' two, because the
#: interactions worth finding are between controls sharing one bridge.
CONTROLS = 4


def _document(name, device):
    """Return a control with a short window and one device of its own.

    Args:
        name (str): the control's name
        device (str): the bridge device it owns

    Returns:
        dict: a control document
    """
    document = conservatory(name=name)
    document.pop("active_period", None)
    document["devices"] = {device: {"source": "hue", "device": device}}
    document["output"] = dict(
        document["output"],
        cycle_seconds=1,
        min_transition_seconds=1,
        stages=[{"level": 0, "set": {device: False}}, {"level": 1500, "set": {device: True}}],
    )
    return document


def _build(installation, count):
    """Write `count` controls, each owning its own plug on the shared bridge.

    Args:
        installation (Installation): where to write them
        count (int): how many

    Returns:
        dict: control name to its document
    """
    documents = {}
    for index in range(count):
        name, device = f"control{index}", f"heater{index}"
        installation.bridge.lights[str(90 + index)] = plug(device)
        documents[name] = _document(name, device)
        installation.write_control(documents[name])
    return documents


def _supervisor(installation, names):
    """Return a supervisor that starts controls from this checkout.

    Args:
        installation (Installation): the installation to run against
        names (iterable): the controls to supervise

    Returns:
        Supervisor: not yet started
    """

    def argv_for(name):
        """Return the command that runs one control.

        Args:
            name (str): the control to run

        Returns:
            list: the command
        """
        return [
            sys.executable,
            os.path.join(ROOT, "sendtoinflux.py"),
            "--control",
            name,
            "--settings",
            installation.settings_file,
        ]

    return Supervisor(
        names,
        settings_file=installation.settings_file,
        argv_for=argv_for,
        backoff=lambda failures: 0.1 * failures,
    )


def _chaos_run(installation, seed, ticks):
    """Drive one seeded run and check what must hold at the end of it.

    Args:
        installation (Installation): the installation to run against
        seed (int): the seed for every choice
        ticks (int): how many ticks to drive

    Raises:
        AssertionError: an invariant was violated, with the seed and the story
    """
    documents = _build(installation, CONTROLS)
    supervisor = _supervisor(installation, documents)
    driver = ChaosDriver(seed, supervisor, installation.bridge, installation.influx)
    try:
        supervisor.start_all()
        # The baseline is taken once every control is up and has talked to both far ends,
        # not before. A census of the system before it has done anything counts none of the
        # connection threads a working install has at rest, so every run would "leak" the
        # cost of starting - which is growth the invariant was never meant to report.
        for _ in range(5):
            supervisor.poll(timeout=0.2)
        before = census.take(os.getpid())
        for number in range(1, ticks + 1):
            driver.tick(number, documents)
        # Cleared and settled before judging what is left: a control cannot make its devices
        # safe through a bridge that is refusing connections, so asserting that mid-fault
        # would be asserting that an outage does not happen.
        driver.settle()
        supervisor.stop_all()
        reports = [
            invariants.devices_unenergised(
                installation.bridge, [d for doc in documents.values() for d in doc["devices"]]
            ),
            invariants.nothing_leaked(before, census.quiet_after(before, os.getpid())),
        ]
        broken = [f"{report.name}: {v}" for report in reports for v in report.violations]
        assert not broken, (
            f"chaos run with seed {seed} left the system wrong:\n  "
            + "\n  ".join(broken)
            + f"\n\nWhat led to it:\n  {driver.run.story()}\n\n"
            f"Add {seed} to SEEDS_THAT_FAILED so this runs from now on."
        )
        for report in reports:
            for reason in report.skipped:
                # Warned rather than swallowed, the same as invariants.check does: a check
                # that was not made reads exactly like a check that passed, and a scheduled
                # job nobody watches is the worst place for that difference to be invisible.
                warnings.warn(f"chaos run with seed {seed} skipped a check: {reason}", stacklevel=2)
    finally:
        supervisor.stop_all()


@pytest.mark.chaos
def test_a_long_seeded_run(state_directory):
    """One long run from a fresh seed, printed so a failure can be repeated.

    The seed comes from the environment where CI passes one, so a scheduled job can be told
    to re-run yesterday's, and is otherwise random - a chaos run that always chose the same
    seed would only ever find the same thing.
    """
    seed = int(os.environ.get("CHAOS_SEED") or random.randrange(2**32))
    print(f"chaos seed: {seed}")
    _chaos_run(state_directory, seed, ticks=int(os.environ.get("CHAOS_TICKS") or 60))


@pytest.mark.parametrize("seed", SEEDS_THAT_FAILED)
def test_a_seed_that_failed_before(state_directory, seed):
    """Every seed that has ever failed, on every run of the ordinary suite.

    Short, because the point is the particular sequence rather than endurance. An empty list
    is the honest state before the first failure: nothing has been found yet, and a
    placeholder seed would only assert that the harness starts.
    """
    _chaos_run(state_directory, seed, ticks=25)
