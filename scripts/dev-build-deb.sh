#!/bin/bash
# Developer convenience wrapper around packaging/deb/build-deb.sh: builds the
# .deb into the (gitignored) dist/ directory for local/manual testing.
#
# Dev use only - CI (premerge.yaml's arm64-verify/bookworm-verify jobs and
# release.yaml) invokes packaging/deb/build-deb.sh directly and must keep
# doing so; nothing here may become load-bearing for an automated build.
#
# Usage: scripts/dev-build-deb.sh [--container|--native]
#
#   (default)    build natively if dpkg-deb is available, otherwise fall back
#                to a container automatically - so this just works on macOS
#   --container  always build in a container, even on a Debian host (useful
#                for reproducing a clean bookworm build)
#   --native     never use a container; fail if dpkg-deb is missing
#
# Environment:
#   BUILD_IMAGE       container image (default: debian:12, matching CI's
#                     bookworm-verify job and the oldest supported target)
#   CONTAINER_ENGINE  docker or podman (default: whichever is found)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_IMAGE="${BUILD_IMAGE:-debian:12}"
MODE=auto

for arg in "$@"; do
    case "$arg" in
        --container) MODE=container ;;
        --native) MODE=native ;;
        -h|--help) sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "error: unknown option '$arg' (try --help)" >&2; exit 1 ;;
    esac
done

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_ROOT/pyproject.toml")"
[ -n "$VERSION" ] || { echo "error: could not read version from pyproject.toml" >&2; exit 1; }
OUT_NAME="send-to-influx_${VERSION}_all.deb"

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

if [ "$MODE" = container ]; then
    ENGINE="$(find_engine)" || { echo "error: no container engine (docker/podman) found" >&2; exit 1; }
    echo "Building $OUT_NAME in a $BUILD_IMAGE container via $ENGINE..."
    # The source is mounted read-only and copied inside, mirroring CI's
    # bookworm-verify job: the build writes only to its own mktemp dir, and
    # copying keeps a stray root-owned artifact from ever landing in the work
    # tree. Only dist/ is mounted writable, and the finished package is
    # chown'ed back to the invoking user so it isn't left root-owned.
    #
    # shellcheck disable=SC2016  # single quotes are deliberate: $OUT_NAME and
    # $HOST_UID/$HOST_GID must expand inside the container, from -e, not here.
    "$ENGINE" run --rm \
        -v "$REPO_ROOT:/src:ro" \
        -v "$REPO_ROOT/dist:/out" \
        -e "OUT_NAME=$OUT_NAME" \
        -e "HOST_UID=$(id -u)" \
        -e "HOST_GID=$(id -g)" \
        "$BUILD_IMAGE" bash -ec \
        '
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -q >/dev/null
            apt-get install -yq python3 python3-venv python3-pip >/dev/null
            cp -r /src /build && cd /build
            bash packaging/deb/build-deb.sh "/out/$OUT_NAME"
            chown "$HOST_UID:$HOST_GID" "/out/$OUT_NAME"
        '
else
    if ! command -v dpkg-deb >/dev/null 2>&1; then
        echo "error: dpkg-deb not found - --native needs a Debian/Ubuntu host." >&2
        echo "Drop --native (or pass --container) to build in a container instead." >&2
        exit 1
    fi
    "$REPO_ROOT/packaging/deb/build-deb.sh" "$REPO_ROOT/dist/$OUT_NAME"
fi

echo
echo "Built: dist/$OUT_NAME"
echo "Install for manual testing (on a DISPOSABLE machine/container):"
echo "  sudo dpkg -i dist/$OUT_NAME"
echo "Or run the destructive scenario suite against it (throwaway container/CI only):"
echo "  sudo packaging/deb/test-packaging.sh dist/$OUT_NAME"
