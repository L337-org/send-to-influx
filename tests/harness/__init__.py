"""A harness for testing control processes as processes rather than as functions.

Unit tests cannot see the properties that matter here. A blocked read, an unenforced
timeout and a lock that locks nothing all pass a suite that never runs a real child, and
all three have happened in this project. So the harness runs real processes against real
HTTP endpoints, injects faults into those endpoints, and checks its invariants from
**outside** the thing under test: from the stub bridge's own record of what it was
commanded, and from the operating system's view of what is running.

That last point is the design rule. An invariant checked against the supervisor's own
report of its behaviour is a test of its reporting. Every check here reads a source the
subject does not write.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"
