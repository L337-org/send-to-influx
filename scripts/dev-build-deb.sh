#!/bin/bash
# Developer convenience wrapper around packaging/deb/build-deb.sh: builds the
# .deb into the (gitignored) dist/ directory for local/manual testing.
#
# Dev use only - CI (premerge.yaml's arm64-verify/bookworm-verify jobs and
# release.yaml) invokes packaging/deb/build-deb.sh directly and must keep
# doing so; nothing here may become load-bearing for an automated build.
#
# Builds are version-stamped so an installed dev build is never mistaken for
# the release: 4.4 becomes 4.4~dev<UTC timestamp>.g<short sha> (plus .dirty if
# the work tree has uncommitted changes). "~" sorts BEFORE the bare version in
# Debian ordering, so installing the real 4.4 over a 4.4~dev build is a normal
# upgrade. `send-to-influx --version` reports the equivalent PEP 440 form
# (4.4.dev<timestamp>+g<sha>), since pip rejects "~".
#
# Usage: scripts/dev-build-deb.sh [--container|--native] [--release]
#
#   (default)    build natively if dpkg-deb is available, otherwise fall back
#                to a container automatically - so this just works on macOS
#   --container  always build in a container, even on a Debian host (useful
#                for reproducing a clean bookworm build)
#   --native     never use a container; fail if dpkg-deb is missing
#   --release    skip dev version stamping, producing the exact version in
#                pyproject.toml - for reproducing the release artifact locally
#
# Environment:
#   BUILD_IMAGE       container image (default: debian:12, matching CI's
#                     bookworm-verify job and the oldest supported target)
#   CONTAINER_ENGINE  docker or podman (default: whichever is found)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_IMAGE="${BUILD_IMAGE:-debian:12}"
MODE=auto
STAMP=1

for arg in "$@"; do
    case "$arg" in
        --container) MODE=container ;;
        --native) MODE=native ;;
        --release) STAMP=0 ;;
        -h|--help) sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "error: unknown option '$arg' (try --help)" >&2; exit 1 ;;
    esac
done

BASE_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_ROOT/pyproject.toml")"
[ -n "$BASE_VERSION" ] || { echo "error: could not read version from pyproject.toml" >&2; exit 1; }

if [ "$STAMP" = 1 ]; then
    TS="$(date -u +%Y%m%d%H%M%S)"
    SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    DIRTY=""
    git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null || DIRTY=".dirty"
    # Two spellings of the same thing: Debian sorts "~" before the bare version
    # (so the release upgrades over a dev build), while pip requires PEP 440,
    # where ".devN" means the same "pre-release of 4.4" and "+local" carries
    # the sha. Keeping both in step is why they're derived together here.
    DEB_VERSION="${BASE_VERSION}~dev${TS}.g${SHA}${DIRTY}"
    PY_VERSION="${BASE_VERSION}.dev${TS}+g${SHA}${DIRTY}"
else
    DEB_VERSION="$BASE_VERSION"
    PY_VERSION="$BASE_VERSION"
fi
OUT_NAME="send-to-influx_${DEB_VERSION}_all.deb"

find_engine() {
    if [ -n "${CONTAINER_ENGINE:-}" ]; then
        command -v "$CONTAINER_ENGINE" >/dev/null 2>&1 && { echo "$CONTAINER_ENGINE"; return 0; }
        echo "error: CONTAINER_ENGINE='$CONTAINER_ENGINE' not found" >&2
        return 1
    fi
    for engine in docker podman; do
        command -v "$engine" >/dev/null 2>&1 && { echo "$engine"; return 0; }
    done
    return 1
}

if [ "$MODE" = auto ]; then
    if command -v dpkg-deb >/dev/null 2>&1; then
        MODE=native
    elif find_engine >/dev/null 2>&1; then
        echo "note: dpkg-deb not found - building in a $BUILD_IMAGE container instead."
        MODE=container
    else
        echo "error: this needs either dpkg-deb (Debian/Ubuntu host) or a container" >&2
        echo "engine (docker/podman) to build in. Install one, or run this on a Debian host." >&2
        exit 1
    fi
fi

mkdir -p "$REPO_ROOT/dist"

# Always build from a copy with pyproject.toml's version rewritten, never from
# the work tree itself: build-deb.sh pip-installs the source and then reads the
# version back out of the installed metadata, so patching the copy is what makes
# `send-to-influx --version` agree with the package version - and doing it in a
# copy means the work tree is never modified, even transiently.
PATCH_PYPROJECT="import re,sys;p=sys.argv[1];s=open(p).read();open(p,'w').write(re.sub(r'^version = \".*\"',
'version = \"'+sys.argv[2]+'\"',s,count=1,flags=re.M))"

if [ "$MODE" = container ]; then
    ENGINE="$(find_engine)" || { echo "error: no container engine (docker/podman) found" >&2; exit 1; }
    echo "Building $OUT_NAME in a $BUILD_IMAGE container via $ENGINE..."
    # Source mounted read-only and copied inside, mirroring CI's bookworm-verify
    # job; only dist/ is writable, and the finished package is chown'ed back to
    # the invoking user so it isn't left root-owned.
    #
    # shellcheck disable=SC2016  # single quotes are deliberate: these expand
    # inside the container, from -e, not here.
    "$ENGINE" run --rm \
        -v "$REPO_ROOT:/src:ro" \
        -v "$REPO_ROOT/dist:/out" \
        -e "OUT_NAME=$OUT_NAME" \
        -e "PY_VERSION=$PY_VERSION" \
        -e "DEB_VERSION_OVERRIDE=$DEB_VERSION" \
        -e "PATCH_PYPROJECT=$PATCH_PYPROJECT" \
        -e "HOST_UID=$(id -u)" \
        -e "HOST_GID=$(id -g)" \
        "$BUILD_IMAGE" bash -ec \
        '
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -q >/dev/null
            apt-get install -yq python3 python3-venv python3-pip >/dev/null
            cp -r /src /build && cd /build
            # Stale setuptools output would be used in preference to the real
            # sources (build-deb.sh aborts if it sees any) - and .venv/.git are
            # just dead weight in the copy.
            rm -rf build *.egg-info dist .venv .git
            python3 -c "$PATCH_PYPROJECT" pyproject.toml "$PY_VERSION"
            bash packaging/deb/build-deb.sh "/out/$OUT_NAME"
            chown "$HOST_UID:$HOST_GID" "/out/$OUT_NAME"
        '
else
    if ! command -v dpkg-deb >/dev/null 2>&1; then
        echo "error: dpkg-deb not found - --native needs a Debian/Ubuntu host." >&2
        echo "Drop --native (or pass --container) to build in a container instead." >&2
        exit 1
    fi
    echo "Building $OUT_NAME..."
    BUILD_COPY="$(mktemp -d)"
    trap 'rm -rf "$BUILD_COPY"' EXIT
    # -a to preserve the executable bits the maintainer scripts rely on.
    cp -a "$REPO_ROOT/." "$BUILD_COPY/"
    # See the container branch above: stale setuptools output in the copy would be
    # shipped in preference to the actual sources.
    rm -rf "$BUILD_COPY/build" "$BUILD_COPY"/*.egg-info "$BUILD_COPY/dist" "$BUILD_COPY/.venv" "$BUILD_COPY/.git"
    python3 -c "$PATCH_PYPROJECT" "$BUILD_COPY/pyproject.toml" "$PY_VERSION"
    DEB_VERSION_OVERRIDE="$DEB_VERSION" "$BUILD_COPY/packaging/deb/build-deb.sh" "$REPO_ROOT/dist/$OUT_NAME"
fi

echo
echo "Built: dist/$OUT_NAME"
[ "$STAMP" = 1 ] && echo "  (dev build - 'send-to-influx --version' reports $PY_VERSION)"
echo "Install for manual testing (on a DISPOSABLE machine/container):"
echo "  sudo dpkg -i dist/$OUT_NAME"
echo "Or run the destructive scenario suite against it (throwaway container/CI only):"
echo "  sudo packaging/deb/test-packaging.sh dist/$OUT_NAME"
