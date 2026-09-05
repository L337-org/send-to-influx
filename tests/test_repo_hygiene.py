"""Repo-wide conventions that are cheaper to enforce than to remember.

Everything here is mechanical: a rule that could otherwise only live as prose in AGENTS.md and
be re-broken by whoever did not read it. The project's standing preference is a failing test
over a note asking someone to be careful.

**What is no longer here, and where it went.** Five checks moved to
``scripts/check-repo-hygiene.py``, which is vendored byte-identically into every repository in
the organisation: tracker keys and wiki links in a public repository, every CI job declaring a
timeout, those timeouts being real bounds, and the instruction file and its pointers existing.
They were not dropped - they are now enforced in four repositories instead of one, by the
``Repository hygiene`` job. Reimplementing them here as well would be divergence by
construction, which is the thing the convergence experiment is meant to measure.

What stays is what would mean nothing in a repository that had never heard of this product: the
supported-Python floor, the licence-header convention, the documented constants, the
``influx_header`` escaping rule, and the two checks whose generic form is genuinely weaker than
their specific one - ``test_at_least_one_file_is_searched`` names files only this repository has,
and ``test_shipped_files_do_not_send_readers_to_the_pointer_file`` depends on knowing which paths
here are shipped.
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


def test_the_docstring_exemption_matches_the_decorators_in_use():
    """The docstring rules must keep skipping every advertised MCP registration.

    A tool, prompt or resource docstring is the advertised interface a client loads and a model
    reads, so CS.6.14 hands it to the AI-consumer rules rather than to CS.6's. tox.ini exempts
    them by matching the decorator, which is a regex over decorator source - and a regex goes
    stale silently. Rename `register_tool` and the sixteen advertised descriptions quietly fall
    under rules that are wrong for them: D417 would start demanding an `Args:` block duplicating
    what the schema already carries, on every session that loads the surface.

    Asserts in both directions. Every registration decorator actually in use is matched, so a
    rename fails here; and the count is floored, so a scan that found nothing cannot pass.
    """
    import ast
    import configparser

    config = configparser.ConfigParser()
    config.read(REPO_ROOT / "tox.ini")
    pattern = config["flake8"]["ignore-decorators"]
    exempt = re.compile(pattern)

    # Decorator expressions applied to functions in the registration modules. Matched by
    # source text rather than by name, because flake8-docstrings sees the whole call for a
    # decorator that takes arguments.
    found = []
    for module in ("mcp_read.py", "mcp_write.py", "mcp_dashboards.py", "mcp_prompts.py", "mcp_resources.py"):
        tree = ast.parse((REPO_ROOT / "toinflux" / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                source = ast.unparse(decorator)
                if "register" in source or "prompt" in source:
                    found.append(f"{module}:{node.name}: {source.splitlines()[0]}")

    assert len(found) >= 12, (
        f"only found {len(found)} registration decorator(s), so this check is not seeing the "
        f"surface it is meant to protect"
    )
    unmatched = [f for f in found if not exempt.search(f.split(": ", 1)[1])]
    assert not unmatched, (
        f"tox.ini's ignore-decorators pattern {pattern!r} no longer matches these registration "
        f"decorators, so their advertised docstrings would fall under the CS.6 rules:\n  " + "\n  ".join(unmatched)
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


def _test_index():
    """Index every test under `tests/`, keeping enough structure to judge a reference runnable.

    Parsed rather than imported: importing the suite to inspect it would run module-level
    fixtures and make this check depend on the rest of the suite being importable, which is
    exactly when a documentation reference most needs verifying.

    :return: tuple of (bare test names, complete nodeids, {file::name: owning class})
    """
    import ast

    bare, nodeids, owner = set(), set(), {}
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                bare.add(node.name)
                nodeids.add(f"{path.name}::{node.name}")
            elif isinstance(node, ast.ClassDef):
                nodeids.add(f"{path.name}::{node.name}")
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith("test_"):
                        bare.add(member.name)
                        nodeids.add(f"{path.name}::{node.name}::{member.name}")
                        owner[f"{path.name}::{member.name}"] = node.name
    return bare, nodeids, owner


def test_every_named_guard_exists():
    """A test named in the documentation must still be a test, or the reference is worse than none.

    `AGENTS.md` omits an invariant that CI already guarantees and names the guard where the
    invariant would have been, so that deleting the guard leaves a reference to something that
    no longer exists rather than silence. That only works while the reference resolves: a
    renamed test turns the pointer back into the silence it was meant to replace, and does it
    invisibly, because a wrong name reads exactly like a right one.

    Two forms are allowed, and the difference is what the reference claims to be.

    A `::`-qualified reference claims to be a **pytest nodeid**, so it must be one you can
    actually run. `file.py::test_name` for a test that is a method on a class is rejected: pytest
    cannot collect it, so a reader who copies the pointer to go and look at the guard gets an
    error rather than the guard. The failure names the class to add.

    A bare `test_name` claims only that a test by that name exists, which is what prose wants
    when it names a file once and then several of its tests - three full nodeids in one sentence
    is unreadable, and the existence check is no weaker for the shorter form.
    """
    bare, nodeids, owner = _test_index()
    referenced = {}
    for relative in sorted(p.relative_to(REPO_ROOT) for p in _tracked_files() if p.suffix == ".md"):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        pattern = r"`(?:[\w/]+/)?([A-Za-z_][A-Za-z0-9_]*\.py(?:::[A-Za-z_][A-Za-z0-9_]*)+|test_[a-z0-9_]+)`"
        for match in re.finditer(pattern, text):
            token = match.group(1)
            # A bare `test_foo.py` names a file, not a test. Any directory prefix is dropped by
            # the pattern, so `tests/test_x.py::test_y` and `test_x.py::test_y` are one claim -
            # prose uses both spellings and neither is wrong.
            if not token.endswith(".py"):
                referenced.setdefault(token, str(relative))

    missing, unrunnable = [], []
    for token, relative in sorted(referenced.items()):
        if "::" not in token:
            if token not in bare:
                missing.append(f"{relative} names `{token}`, which is not a test")
        elif token in nodeids:
            continue
        elif token in owner:
            unrunnable.append(
                f"{relative} names `{token}`, which pytest cannot collect - "
                f"write `{token.replace('::', '::' + owner[token] + '::', 1)}`"
            )
        else:
            missing.append(f"{relative} names `{token}`, which is not a test")

    assert referenced, "found no test references in any tracked document - the scan is not working"
    assert not missing, (
        "these documents name a test that does not exist, so the reference resolves to nothing "
        "and the invariant it stands in for is now guarded by neither:\n  " + "\n  ".join(missing)
    )
    assert not unrunnable, (
        "these references are missing the class that owns the test, so they are not runnable "
        "nodeids and a reader following one gets a collection error:\n  " + "\n  ".join(unrunnable)
    )
