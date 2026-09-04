"""Repo-wide conventions that are cheaper to enforce than to remember.

Everything here is mechanical: a rule that could otherwise only live as prose in AGENTS.md and
be re-broken by whoever did not read it. The project's standing preference is a failing test
over a note asking someone to be careful.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Only files git actually tracks. Deliberately not a filesystem walk: that would sweep in .venv
# and build output, and - the reason it matters - a developer's own settings.yaml, which is
# gitignored, holds real credentials and is none of this test's business.
#
# Every tracked *text* file, decided by sniffing rather than by a suffix allowlist. An allowlist
# has holes by construction and silently falls behind: the first version listed extensions and
# missed 14 tracked files, including every debconf maintainer script (`postinst`, `preinst`,
# `config`, ...) - shell files with no extension, and exactly the kind of place a comment cites
# an issue key. Sniffing covers anything added later with no list to maintain.
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".gz", ".zip", ".deb", ".whl")

# This file necessarily contains the patterns it searches for, so it cannot check itself.
SELF = Path(__file__).name

# This repo is public; the tracker and the wiki are not. The issue carries the link to the PR,
# never the reverse - that is the only surviving connection, and it lives on the private side.
# Matched by the tracker's key shape rather than one project's prefix, so a second project's
# keys are caught too.
#
# The prefix is letters only, and at least two. Allowing digits in it (as Jira technically does)
# made this match `Z0-9` inside the regex character class `[a-zA-Z0-9]` in pylintrc - a false
# positive, and the kind that gets a guard switched off wholesale, which costs more than it ever
# caught. A project key with a digit in it would slip past; that is the right side to err on,
# since the cost of a miss is a human noticing, while the cost of a false alarm is the check
# being disabled.
TRACKER_KEY = re.compile(r"\b[A-Z]{2,10}-\d+\b")

# Strings with the same shape that are not tracker keys. An over-broad guard is one someone
# eventually switches off wholesale, which costs more than it ever caught - so each of these is
# a real external standard or identifier, evidenced, not a way to quiet a genuine hit.
NOT_A_TRACKER_KEY = re.compile(
    r"\b(?:"
    r"ISO-\d+"  # ISO 8601, ISO 3166
    r"|RFC-\d+"  # RFC 3339
    r"|SHA-\d+"  # SHA-256
    r"|UTF-\d+"
    r"|AES-\d+"
    r"|PEP-\d+"
    r"|CVE-\d+"
    r"|SMETS-\d+"  # UK smart meter generations
    # Octopus Energy tariff codes, which genuinely look like this: FLEX-22-11-25,
    # SILVER-FLEX-22-11-25, AGILE-FLEX-22-11-25. They appear in example_settings.yaml and the
    # Octopus tests as real configuration values.
    r"|FLEX-\d+"
    r")\b"
)

WIKI_HOSTS = ("atlassian.net", "/wiki/spaces/")

# Documents that restate the .deb's `Depends:` python3 floor. Folded into the floor check
# below rather than left as prose: each copy is correct today, and nothing else would notice
# the day the floor rises and a document keeps the old number - at which point the stale copy
# reads as authoritative to whoever finds it first.
DOC_COPIES_OF_THE_DEB_PYTHON_FLOOR = (
    "README.md",
    "architecture/packaging.md",
    "architecture/mcp-server.md",
)

# Figures a document states that a named constant actually owns. Same argument as the floor
# copies: correct today, unpinned, and a false finding the day one drifts. The rendering is a
# function rather than a format string because a document states a duration in whichever unit
# reads well, not the unit the constant happens to hold.
DOCUMENTED_CONSTANTS = (
    ("architecture/mcp-server.md", "toinflux/mcpserver.py", "LOGIN_FAILURE_LIMIT", lambda v: f"{v} failures"),
    ("architecture/mcp-server.md", "toinflux/mcpserver.py", "LOGIN_LOCKOUT_SECONDS", lambda v: f"{v} s lockout"),
    ("architecture/mcp-server.md", "toinflux/mcpserver.py", "ACCESS_TOKEN_TTL_SECONDS", lambda v: f"{v // 3600} h TTL"),
    ("architecture/mcp-server.md", "toinflux/general.py", "MCP_DEFAULT_BIND_ADDRESS", lambda v: f"`{v}`"),
    ("AGENTS.md", "toinflux/speedtest.py", "MAX_PLAUSIBLE_PING_MS", lambda v: f"{v} ms"),
)


def _tracked_files():
    """Every source file git tracks that the conventions below apply to."""
    try:
        listing = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        # No git, or not a checkout - a source tarball, say. That is a fact about the
        # environment, not about the repo's hygiene, and failing here would report the wrong
        # thing and take the whole suite down with it. An over-broad guard is one someone
        # eventually switches off wholesale.
        #
        # But a skip must never be how the guard quietly stops running where it is the actual
        # gate, so under CI this is an error rather than a skip. GitHub Actions always sets CI.
        if os.environ.get("CI"):
            raise RuntimeError(
                f"cannot list tracked files with git ({exc}), and CI is set - this check is a "
                f"merge gate there, so it must fail rather than skip"
            ) from exc
        pytest.skip(f"not a git checkout, or git is unavailable ({exc}) - repo hygiene is not checkable here")
    paths = []
    for name in listing.stdout.split("\0"):
        if not name or Path(name).name == SELF:
            continue
        path = REPO_ROOT / name
        if not path.is_file() or not _is_text(path):
            continue
        paths.append(path)
    return sorted(paths)


def _is_text(path):
    """Whether a file can be scanned as text.

    Sniffed rather than inferred from the name, so a text file with no extension - every
    maintainer script here - is covered, and a future binary asset cannot break the scan. A NUL
    byte is the usual binary marker, and anything that is not valid UTF-8 is not text we can
    meaningfully search either.

    :param path: the file to check
    :type path: pathlib.Path
    :return: True if the file should be scanned
    :rtype: bool
    """
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\0" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        # Could be a multi-byte character split by the 8 KiB boundary; only reject if the whole
        # file fails, so a large UTF-8 file is not skipped on a boundary artefact.
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return False
    return True


def test_at_least_one_file_is_searched():
    """Guards the guard: a glob that silently matched nothing would make every test below
    pass while checking absolutely nothing."""
    files = _tracked_files()
    assert len(files) > 40, f"only found {len(files)} files to check - the listing is wrong"
    names = {path.name for path in files}
    for expected in ("influx.py", "test_influx.py", "README.md", "CLAUDE.md", "build-deb.sh"):
        assert expected in names, f"{expected} is not being checked"
    # The developer's own settings.yaml is gitignored and holds real credentials; it must never
    # be read by this test, whatever the pattern list says.
    assert "settings.yaml" not in names
    assert "example_settings.yaml" in names
    # Files the first, suffix-allowlist version silently skipped. Named individually so the hole
    # cannot reopen: the maintainer scripts have no extension and are exactly where a comment
    # would cite an issue key.
    for expected in (
        "postinst",
        "preinst",
        "prerm",
        "postrm",
        "config",
        "pyproject.toml",
        "requirements.txt",
        "CODEOWNERS",
        "send-to-influx.rsyslog",
        "send-to-influx.logrotate",
    ):
        assert expected in names, f"{expected} is tracked but not being checked"


def test_no_tracker_keys_in_a_public_repo():
    """A tracker key in a public repo leaks the internal board's structure and is a dead
    reference to anyone reading the repo, including generated release notes.

    Describe the work instead - the same rule a TODO follows here.
    """
    offenders = []
    for path in _tracked_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for match in TRACKER_KEY.finditer(line):
                span = match.group(0)
                if NOT_A_TRACKER_KEY.search(span):
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {span}")
    assert not offenders, "tracker keys must not appear in a public repo:\n  " + "\n  ".join(offenders)


def test_no_wiki_links_in_a_public_repo():
    """Same reasoning as the tracker keys: an internal wiki URL is a leak and a dead link."""
    offenders = []
    for path in _tracked_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for host in WIKI_HOSTS:
                if host in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {host}")
    assert not offenders, "internal wiki links must not appear in a public repo:\n  " + "\n  ".join(offenders)


def test_shipped_files_do_not_send_readers_to_the_pointer_file():
    """`CLAUDE.md` is a generated pointer, so nothing may cite it as the home of content.

    The exit-code table moved into `AGENTS.md`, but two shipped files still told readers to
    look for it in `CLAUDE.md` - a reference that reads as authoritative and leads nowhere.
    Documentation may name `CLAUDE.md` when describing the pointer arrangement itself; shipped
    source and packaging have no reason to name it at all.
    """
    offenders = []
    for path in (REPO_ROOT / "sendtoinflux.py", *(REPO_ROOT / "packaging").rglob("*")):
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "CLAUDE.md" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert not offenders, "shipped files must point at AGENTS.md, not the generated pointer:\n  " + "\n  ".join(
        offenders
    )


def test_the_shared_instruction_file_and_its_pointers_exist():
    """AGENTS.md carries the rules; CLAUDE.md and copilot-instructions.md only point at it.
    None is inheritable org-wide, so a per-repo copy is the only way any of them takes effect,
    and deleting the shared file would leave every assistant unbriefed with nothing failing."""
    agents = REPO_ROOT / "AGENTS.md"
    pointers = (REPO_ROOT / "CLAUDE.md", REPO_ROOT / ".github" / "copilot-instructions.md")

    assert agents.is_file()
    shared = agents.read_text(encoding="utf-8")
    # Assert what makes this a real AGENTS.md, rather than a size threshold that
    # a legitimate condensation would trip: it routes to the detail layer, and
    # it carries a generated section routing to the on-demand policy files.
    for sentinel in (
        "## Read these before changing the matching area",
        "<!-- BEGIN GENERATED -->",
        "<!-- END GENERATED -->",
    ):
        assert sentinel in shared, f"AGENTS.md is missing {sentinel!r}"
    # Scope the routing check to the generated block rather than the whole file,
    # so it means "the router routes" rather than "the path is mentioned
    # somewhere", and match on the path alone: bullet style and backticks are
    # presentation, and a test that pins them fails on a change that breaks
    # nothing.
    routed = shared.partition("<!-- BEGIN GENERATED -->")[2].partition("<!-- END GENERATED -->")[0]
    targets = set(re.findall(r"\.agents/policy/[A-Za-z0-9_-]+\.md", routed))
    assert targets, "AGENTS.md routes to no policy file"

    # A pointer at a file that is not there is worse than no pointer: it reads
    # as authoritative and resolves to nothing, and the reader has no way to
    # tell the difference from the routing table alone.
    for target in sorted(targets):
        assert (REPO_ROOT / target).is_file(), f"AGENTS.md routes to {target}, which does not exist"

    # The other direction, so a policy file cannot be added and left unreachable.
    on_disk = {f".agents/policy/{f.name}" for f in (REPO_ROOT / ".agents" / "policy").glob("*.md")}
    assert on_disk == targets, (
        f"the policy files on disk and the ones AGENTS.md routes to disagree: "
        f"unrouted={sorted(on_disk - targets)} missing={sorted(targets - on_disk)}"
    )

    for pointer in pointers:
        assert pointer.is_file()
        body = pointer.read_text(encoding="utf-8")
        # Catches a pointer that stops pointing - renamed target, dropped line.
        assert "AGENTS.md" in body, f"{pointer.name} no longer references AGENTS.md"
        # Catches the regression this change exists to prevent: a pointer file
        # quietly regaining rules of its own, which is how the two drifted apart
        # before there was a shared file. Forbidding headings was too narrow -
        # markdown allows up to three spaces before a "##", and prose needs no
        # heading at all. The promise is "a pointer and nothing else", so assert
        # exactly that: outside the generated block, the only content is the H1.
        before, marker, rest = body.partition("<!-- BEGIN GENERATED -->")
        assert marker, f"{pointer.name} has no generated block"
        _, end_marker, after = rest.partition("<!-- END GENERATED -->")
        assert end_marker, f"{pointer.name} has no closing marker"

        outside = [ln.strip() for ln in before.splitlines() if ln.strip()]
        outside += [ln.strip() for ln in after.splitlines() if ln.strip()]
        stray = [ln for ln in outside if not ln.startswith("# ")]
        assert not stray, (
            f"{pointer.name} carries content outside its generated block: {stray}. "
            "It should hold a title and a pointer, with every rule in AGENTS.md"
        )
        assert sum(1 for ln in outside if ln.startswith("# ")) == 1, f"{pointer.name} should have exactly one title"

        # A clean outside is not enough on its own: content added inside the
        # block would satisfy it while still breaking "a pointer and nothing
        # else". A pointer file holds exactly one line, so assert that, and
        # drift is caught wherever it is put.
        inside = [ln.strip() for ln in rest.split("<!-- END GENERATED -->")[0].splitlines() if ln.strip()]
        assert len(inside) == 1, (
            f"{pointer.name} has {len(inside)} lines inside its generated block; "
            f"a pointer file holds exactly one: {inside}"
        )
        assert "AGENTS.md" in inside[0], f"{pointer.name} pointer does not name AGENTS.md"


def _workflow_jobs():
    """Every CI job, as ``(workflow path, job name, job body)``.

    Sourced from ``_tracked_files()`` rather than a filesystem glob, for the same reason
    everything else here is: an untracked or gitignored workflow file a developer left in the
    directory is not part of the repo and must not fail their suite.

    Each job body is checked for being a mapping as it is yielded. A workflow with ``jobname:``
    and no body parses as ``None``, and the callers would then fail with an AttributeError from
    ``job.get(...)`` - which says nothing about which workflow or job is malformed.
    """
    import yaml

    workflows = [
        path
        for path in _tracked_files()
        if path.parent == REPO_ROOT / ".github" / "workflows" and path.suffix in (".yml", ".yaml")
    ]
    assert workflows, "no workflow files found - this check would pass while verifying nothing"
    for path in workflows:
        relative = path.relative_to(REPO_ROOT)
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{relative} does not parse as a YAML mapping"
        # Not `document.get("jobs") or {}`: a missing or null `jobs:` would become an empty
        # mapping, and the workflow would be skipped silently while the timeout checks reported
        # success - a guard that quietly stops guarding. A workflow with no jobs is invalid to
        # Actions anyway, so there is no legitimate case to accommodate.
        jobs = document.get("jobs")
        assert isinstance(jobs, dict) and jobs, (
            f"{relative} has no usable `jobs:` mapping (got {jobs!r}). Every workflow must "
            f"declare its jobs, or the timeout checks would pass without inspecting it"
        )
        for name, job in jobs.items():
            assert isinstance(job, dict), f"{relative}:{name} has no body, or a body that is not a mapping"
            yield relative, name, job


def test_every_ci_job_has_a_timeout():
    """An unbounded job can block a PR for six hours on an infrastructure problem.

    GitHub's default job timeout is 360 minutes, and nothing here needs more than a few. That
    is not hypothetical: the integration job once wedged on `apt-get update` against an
    unresponsive mirror and sat there until it was cancelled by hand, with the merge blocked
    throughout and no signal about what was wrong.

    A timeout converts that into a failure in minutes, naming the step it died in. Enforced
    here rather than written down, because the failure mode of forgetting is invisible until
    the day something hangs.
    """
    unbounded = [f"{path}:{name}" for path, name, job in _workflow_jobs() if job.get("timeout-minutes") is None]
    assert (
        not unbounded
    ), "these CI jobs have no timeout-minutes and would run to GitHub's 6-hour default:\n  " + "\n  ".join(unbounded)


def test_ci_job_timeouts_are_sane():
    """A timeout so large it never fires is the same as not having one, and one so small it
    fires on a healthy run gets raised until it is the former. These are sized at roughly ten
    times the observed maximum, so the bound is meaningful without being flaky.

    What is required is that the bound be *knowable at review time*, which is not the same as
    requiring a YAML number - a quoted digit string is accepted and coerced, for the reason
    given at the check below. A `${{ }}` expression is refused, because one resolving to 600 at
    run time would satisfy a static bound while leaving the job effectively unbounded.
    """
    for path, name, job in _workflow_jobs():
        minutes = job.get("timeout-minutes")
        if minutes is None:
            continue  # reported by the test above
        if isinstance(minutes, str):
            # YAML keeps a quoted value a string and Actions accepts that, so a digit string is
            # a valid timeout and is coerced.
            #
            # A `${{ }}` expression is refused, and NOT because Actions rejects it - it does
            # not. GitHub's context-availability table lists jobs.<job_id>.timeout-minutes as
            # accepting the github, needs, strategy, matrix, vars and inputs contexts, so an
            # expression is perfectly legal there. It is refused because its value is unknowable
            # at review time, which is precisely what this test checks: a timeout resolving to
            # 600 at run time would satisfy a static bound while leaving the job effectively
            # unbounded. Nothing here needs one, so the bound stays real; if a job ever does,
            # change this deliberately rather than working around it.
            assert minutes.strip().isdigit(), (
                f"{path}:{name} has timeout-minutes={minutes!r}, whose value cannot be known at "
                f"review time. An expression is valid Actions, but one resolving to 600 at run "
                f"time would satisfy this bound while leaving the job unbounded. A quoted number "
                f"is accepted; this is not"
            )
            minutes = int(minutes)
        assert isinstance(minutes, int) and not isinstance(
            minutes, bool
        ), f"{path}:{name} has timeout-minutes={minutes!r} of type {type(minutes).__name__}, expected a number"
        assert 1 <= minutes <= 60, f"{path}:{name} has timeout-minutes={minutes}, outside 1-60"


def _declared_minimum_pythons():
    """The minimum supported Python minor, as each place that declares it states it.

    Read with regexes rather than a TOML parser on purpose: ``tomllib`` is stdlib only from
    3.11, and the suite runs on 3.10 in CI. Each pattern asserts it matched, so a file
    reshuffled beyond their reach fails loudly instead of quietly checking nothing.

    :return: ``{source description: minor version}``
    :rtype: dict
    """
    found = {}

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Capture the whole specifier, then find the floor inside it. Matching the minor directly
    # against the closing quote would reject the perfectly ordinary ">=3.10,<4" - and would do
    # so as "could not find requires-python", sending the reader hunting for a line that is
    # plainly there. The floor is whatever `>=` or `~=` states; an upper bound is not a floor.
    requires = re.search(r'^requires-python\s*=\s*"([^"]*)"', pyproject, re.M)
    assert requires, "could not find requires-python in pyproject.toml"
    floor = re.search(r"(?:>=|~=)\s*3\.(\d+)", requires.group(1))
    assert floor, f"requires-python is {requires.group(1)!r}, which states no 3.x lower bound"
    found["pyproject requires-python"] = int(floor.group(1))

    targets = re.search(r"^target-version\s*=\s*\[([^\]]*)\]", pyproject, re.M)
    assert targets, "could not find [tool.black] target-version in pyproject.toml"
    minors = [int(m) for m in re.findall(r'"py3(\d+)"', targets.group(1))]
    assert minors, f"target-version {targets.group(1)!r} names no py3XX version"
    found["black target-version"] = min(minors)

    build = (REPO_ROOT / "packaging" / "deb" / "build-deb.sh").read_text(encoding="utf-8")
    deb = re.search(r"^PYTHON_MIN_SUPPORTED_MINOR=(\d+)", build, re.M)
    assert deb, "could not find PYTHON_MIN_SUPPORTED_MINOR in build-deb.sh"
    found["deb PYTHON_MIN_SUPPORTED_MINOR"] = int(deb.group(1))

    for path, name, job in _workflow_jobs():
        matrix = ((job.get("strategy") or {}).get("matrix") or {}).get("python-version")
        if matrix:
            found[f"{path}:{name} matrix"] = min(int(str(v).split(".")[1]) for v in matrix)

    # Whitespace is normalised before matching because one copy wraps across a line break, and
    # a copy that only fails when it happens to sit on one line is worse than no check. Each
    # occurrence is keyed separately: a document stating the floor twice must have both right,
    # and a dict keyed by filename alone would let the second overwrite the first unchecked.
    # Asserted per file rather than on the total. An aggregate count lets one document go
    # silent as long as another happens to state the floor twice, which is the same guard
    # quietly checking less than it appears to.
    for relative in DOC_COPIES_OF_THE_DEB_PYTHON_FLOOR:
        text = re.sub(r"\s+", " ", (REPO_ROOT / relative).read_text(encoding="utf-8"))
        matches = list(re.finditer(r"python3 \(>= 3\.(\d+)\)", text))
        assert matches, (
            f"{relative} no longer states the .deb's python3 floor, so it has removed itself "
            f"from this check - restore the `Depends: python3 (>= 3.X)` mention, or drop the "
            f"file from DOC_COPIES_OF_THE_DEB_PYTHON_FLOOR deliberately"
        )
        for index, match in enumerate(matches, 1):
            found[f"{relative} `Depends:` prose #{index}"] = int(match.group(1))

    return found


def test_the_supported_python_floor_is_declared_consistently():
    """Four places state the minimum supported Python, and they must agree.

    `requires-python` gates installation, the .deb's PYTHON_MIN_SUPPORTED_MINOR drives its
    `Depends:` and the venv symlinks, the CI matrix decides what is actually tested, and
    black's target-version decides what syntax the formatter may emit. Raise the floor in one
    and forget another, and the failure is remote from the cause: a package that installs on a
    version nothing tested, or formatting a supported interpreter cannot parse.

    Cheap to check, easy to forget, and the project already keeps the .deb's own range in one
    place for exactly this reason - this extends that to the rest.
    """
    declared = _declared_minimum_pythons()
    assert len(declared) >= 4, f"expected at least four declarations, found {sorted(declared)}"
    assert len(set(declared.values())) == 1, "the supported Python floor is declared inconsistently:\n  " + "\n  ".join(
        f"{source}: 3.{minor}" for source, minor in sorted(declared.items())
    )


# The header block every non-test module carries. CS.9.5 makes this a test rather than a habit:
# the MCP modules arrived later than the collectors and could as easily have landed without it,
# and a convention only review enforces is one that drifts between reviewers.
#
# The year is deliberately not asserted. It records when a file was written, and both 2025 and
# 2026 appear legitimately across the tree; pinning it would turn every new module into a
# failing test for no benefit. The holder is asserted, because CS.9.4 wants the stable owner
# rather than whoever happened to write the file.
LICENCE_HEADER_FIELDS = ("__author__", "__copyright__", "__license__")
COPYRIGHT_HOLDER = "Gavin Lucas"


def _modules_that_carry_a_header():
    """Every tracked Python module the header convention applies to.

    Tests are excluded: the convention is about what ships and what a reader of the source
    finds at the top of a module, and a test file is neither. Derived from what git tracks
    rather than from a list, so a module added later is covered without anyone remembering.

    :return: absolute paths, as ``_tracked_files()`` yields them
    :rtype: list
    """
    return [path for path in _tracked_files() if path.suffix == ".py" and "tests" not in path.parts]


def _header_assignments(text):
    """The dunders assigned above a module's first import or definition, with their values.

    Position is asserted rather than mere presence, because the convention is a header block:
    the same three assignments buried between two functions would satisfy a substring search
    while being no header at all. "Above the first import or definition" is what "at the top"
    means here in practice - comments and the module docstring do not count as statements.

    :param str text: the module's source
    :return: ``{name: value}`` for every literal assignment in the header region
    :rtype: dict
    """
    import ast

    found = {}
    for node in ast.parse(text).body:
        if isinstance(
            node,
            (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            break
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            # A header dunder built from an expression is not a literal we can read. Skipped
            # rather than fatal: the caller then reports it as a missing field, which is what
            # it effectively is, and names the module.
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = value
    return found


def test_every_module_carries_the_licence_header():
    """Author, copyright and licence appear at the top of every non-test module.

    "At the top" is enforced, not just "somewhere in the file": the three assignments must sit
    above the module's first import or definition, which is what makes them a header rather
    than three statements that happen to exist.

    ``__license__`` is validated against the SPDX licence list rather than compared to a string
    literal here, because CS.9.2 asks for a *machine-readable* identifier and the only way to
    know a value is one is to ask the standard. This is not pedantry about a synonym: SPDX has
    no entry for "MIT License", so it is rejected outright as an invalid expression while "MIT"
    is accepted, and the same applies to "MPL 2.0" against "MPL-2.0". Validating rather than
    matching also means a relicence needs no edit here, while a typo in one module still fails.

    Collected into one report rather than failing on the first offender, so adding several
    modules at once shows every miss instead of one at a time across as many CI runs.
    """
    from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

    modules = _modules_that_carry_a_header()
    # A guard that searched nothing reports the same green as one that found nothing wrong,
    # which is the failure VE.2.4 names. The floor is well below the current count so an
    # ordinary addition or removal does not trip it, but a broken discovery does.
    assert len(modules) >= 20, (
        f"only found {len(modules)} module(s) to check, so discovery is broken rather than " f"the tree being clean"
    )

    missing = []
    for path in modules:
        header = _header_assignments(path.read_text(encoding="utf-8"))
        relative = path.relative_to(REPO_ROOT)
        absent = [field for field in LICENCE_HEADER_FIELDS if field not in header]
        if absent:
            missing.append(f"{relative} declares no {', '.join(absent)}")
            continue
        if COPYRIGHT_HOLDER not in str(header["__copyright__"]):
            missing.append(f"{relative} does not name {COPYRIGHT_HOLDER!r} as the copyright holder")
        declared = header["__license__"]
        try:
            canonical = canonicalize_license_expression(str(declared))
        except InvalidLicenseExpression as exc:
            missing.append(f"{relative} has __license__ = {declared!r}: {exc}")
        else:
            if canonical != declared:
                missing.append(
                    f"{relative} has __license__ = {declared!r}, which SPDX canonicalises to "
                    f"{canonical!r} - declare the canonical form"
                )
    assert not missing, f"{len(missing)} module(s) break the licence-header convention:\n  " + "\n  ".join(missing)


def test_the_declared_licence_agrees_across_the_project():
    """pyproject.toml and every module header must name the same SPDX licence.

    There are exactly two homes for this - the packaging metadata and the header block - and
    nothing previously compared them. They had in fact diverged in form for as long as both
    existed: the modules said "MIT License" while pyproject pointed at a file, so neither
    stated a machine-readable identifier and no single value was authoritative.

    Read with a regex rather than ``tomllib`` for the same reason as the Python floor above:
    ``tomllib`` is stdlib only from 3.11 and the suite runs on 3.10 in CI.

    A relicence now has to touch both places or fail here, which is the whole point - the
    licence a package advertises and the licence its source claims disagreeing is the one
    outcome worth a test.
    """
    from packaging.licenses import canonicalize_license_expression

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^license\s*=\s*"([^"]*)"', pyproject, re.M)
    assert declared, (
        "could not find a string `license` in pyproject.toml. PEP 639 deprecated the table "
        "forms, so a `license = { file = ... }` here is the thing to fix rather than this test"
    )
    packaged = canonicalize_license_expression(declared.group(1))

    disagreeing = {}
    for path in _modules_that_carry_a_header():
        header = _header_assignments(path.read_text(encoding="utf-8"))
        if header.get("__license__") != packaged:
            disagreeing[str(path.relative_to(REPO_ROOT))] = header.get("__license__")
    assert not disagreeing, f"pyproject.toml declares {packaged!r} but these modules disagree:\n  " + "\n  ".join(
        f"{where}: {what!r}" for where, what in sorted(disagreeing.items())
    )


def _module_constant(relative, name):
    """The value of a module-level constant, read without importing the module.

    Parsed rather than imported: importing a collector runs its module body and pulls in its
    dependencies, which a hygiene check has no business doing, and the MCP modules are
    optional at runtime so importing them here would make this test depend on an extra.

    :param str relative: module path relative to the repository root
    :param str name: the constant's name
    :return: the constant's literal value
    """
    import ast

    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                # Turning a constant into a computed expression is a legitimate change.
                # Failing here with a raw traceback would report that Python could not
                # evaluate something, rather than which constant this check can no longer
                # read and what to do instead.
                raise AssertionError(
                    f"{relative} declares {name} as a computed expression rather than a "
                    f"literal ({exc}), so it cannot be read without importing the module. "
                    f"Keep it literal, or drop it from DOCUMENTED_CONSTANTS and guard the "
                    f"documented figure another way"
                ) from exc
    raise AssertionError(f"{relative} declares no module-level {name}")


def test_every_documented_constant_matches_its_source():
    """A figure stated in a document must still match the constant that owns it.

    These copies are all correct at the time of writing, which is the argument for the test
    rather than against it: the failure mode is not a wrong number today but a right one that
    silently stops being right, leaving two documents describing the same behaviour
    differently and no way for a reader to tell which is current.

    Reported as a collected list rather than one assertion per row, so a floor change that
    invalidates four copies shows all four instead of hiding three behind the first.
    """
    stale = []
    for doc, module, name, render in DOCUMENTED_CONSTANTS:
        expected = render(_module_constant(module, name))
        text = re.sub(r"\s+", " ", (REPO_ROOT / doc).read_text(encoding="utf-8"))
        if expected not in text:
            stale.append(f"{doc} no longer states {expected!r}, which {module} sets in {name}")
    assert not stale, "documented figures no longer match their source:\n  " + "\n  ".join(stale)


def test_the_documented_rpds_py_hold_matches_requirements():
    """Two documents restate the `rpds-py` hold; `requirements.txt` owns it.

    The hold exists so a wheel version cannot drop a Python minor the .deb supports, so a
    document naming the wrong one misleads a reader about which minors can still be built -
    the one question the hold exists to answer.
    """
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pin = re.search(r"^rpds-py(~=[\d.]+)", requirements, re.M)
    assert pin, "could not find the rpds-py pin in requirements.txt"
    for relative in ("architecture/mcp-server.md", "architecture/packaging.md"):
        text = re.sub(r"\s+", " ", (REPO_ROOT / relative).read_text(encoding="utf-8"))
        assert (
            pin.group(1) in text
        ), f"{relative} does not state the rpds-py hold {pin.group(1)!r} from requirements.txt"


def test_every_dynamic_tag_value_in_a_header_is_escaped():
    """No `influx_header` may be built from a computed value without escaping it.

    Line protocol gives a tag value's spaces, commas and equals signs structural meaning, and
    has no escape for a newline at all - so an unescaped value can silently truncate a point
    or forge a second one. Every source that builds a header from data therefore passes it
    through ``escape_key_or_tag_value()``.

    Written as a sweep because remembering did not work: Hue, MyEnergi and Nuki were all done
    in one pass and Speedtest's own header was missed, because its value comes from the OS
    rather than from configuration and so did not look like input. It was found in review,
    not here, which is the argument for the check existing at all.

    Scoped to the enclosing function rather than the single line, deliberately. The heartbeat
    builds its tag string over several lines and splices the finished string into the header,
    which a line-level check flags as a false positive - and an over-broad guard is one
    someone eventually switches off wholesale, which costs more than it ever caught. Asking
    "does this function escape anything at all?" allows that shape while still catching a
    value interpolated straight into the header, which is the bug that actually happened.
    """
    import ast

    offenders = []
    for path in _tracked_files():
        if path.suffix != ".py" or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(node)
            # Does this function assign a header built by interpolation at all?
            builds_header = any(
                isinstance(inner, ast.Assign)
                and any(
                    (isinstance(t, ast.Attribute) and t.attr == "influx_header")
                    or (isinstance(t, ast.Name) and t.id == "influx_header")
                    for t in inner.targets
                )
                and isinstance(inner.value, ast.JoinedStr)
                and any(isinstance(v, ast.FormattedValue) for v in inner.value.values)
                for inner in ast.walk(node)
            )
            if builds_header and "escape_key_or_tag_value" not in body:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.name}()")
    assert not offenders, (
        "these functions build an influx_header by interpolation without escaping anything, so "
        "a space, comma or newline in the value would corrupt or split the point:\n  " + "\n  ".join(offenders)
    )
