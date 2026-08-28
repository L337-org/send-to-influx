"""Repo-wide conventions that are cheaper to enforce than to remember.

Everything here is mechanical: a rule that could otherwise only live as prose in CLAUDE.md and
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


def test_the_assistant_instruction_files_both_exist():
    """Neither is inheritable org-wide, so a per-repo copy of each is the only way either
    takes effect - and they are meant to move together, which is the most-forgotten step."""
    assert (REPO_ROOT / "AGENTS.md").is_file()
    assert (REPO_ROOT / "CLAUDE.md").is_file()
    assert (REPO_ROOT / ".github" / "copilot-instructions.md").is_file()


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

    Values must be literal, for the same reason: a `${{ }}` expression is legal in Actions but
    its value is unknowable here, so it would satisfy the check while leaving the job
    effectively unbounded.
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
                f"{path}:{name} has timeout-minutes={minutes!r}, which is not a literal number. "
                f"An expression is valid Actions, but its value cannot be checked here, so the "
                f"bound would stop meaning anything"
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
