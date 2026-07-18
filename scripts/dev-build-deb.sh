#!/bin/bash
# Developer convenience wrapper around packaging/deb/build-deb.sh: builds the
# .deb into the (gitignored) dist/ directory for local/manual testing.
#
# Dev use only - CI (premerge.yaml's arm64-verify/bookworm-verify jobs and
# release.yaml) invokes packaging/deb/build-deb.sh directly and must keep
# doing so; nothing here may become load-bearing for an automated build.
#
# Usage: scripts/dev-build-deb.sh
# Requires a Debian/Ubuntu-ish host (dpkg-deb, python3) and network access
# for pip - the same preconditions as build-deb.sh itself, just checked up
# front with friendlier errors.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "error: dpkg-deb not found - the .deb can only be built on a Debian/Ubuntu-ish" >&2
    echo "host (or in a container, e.g.: docker run --rm -v \"$REPO_ROOT\":/src -w /src debian:12 \\" >&2
    echo "  sh -c 'apt-get update && apt-get install -y python3-venv dpkg-dev && scripts/dev-build-deb.sh')" >&2
    exit 1
fi

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_ROOT/pyproject.toml")"
[ -n "$VERSION" ] || { echo "error: could not read version from pyproject.toml" >&2; exit 1; }

mkdir -p "$REPO_ROOT/dist"
OUT_FILE="$REPO_ROOT/dist/send-to-influx_${VERSION}_all.deb"

"$REPO_ROOT/packaging/deb/build-deb.sh" "$OUT_FILE"

echo
echo "Built: ${OUT_FILE#"$REPO_ROOT"/}"
echo "Install for manual testing (on a DISPOSABLE machine/container):"
echo "  sudo dpkg -i ${OUT_FILE#"$REPO_ROOT"/}"
echo "Or run the destructive scenario suite against it (throwaway container/CI only):"
echo "  sudo packaging/deb/test-packaging.sh ${OUT_FILE#"$REPO_ROOT"/}"
