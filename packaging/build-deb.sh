#!/usr/bin/env bash
# Build a .deb package for send-to-influx.
#
# Bundles the app and its Python dependencies into a venv under
# /opt/send-to-influx, with a systemd unit to run it as a service.
#
# The package is Architecture: all - the app and its dependencies are pure
# Python with pure-Python fallbacks for any optional compiled accelerators
# (see the .so-stripping step below), and the venv's own python3 is a symlink
# to the system-provided /usr/bin/python3 (declared as a Depends:), not a
# bundled interpreter binary. A CI job builds and smoke-tests this same script
# on an arm64 runner on every push/PR (a required status check), to catch a
# future dependency change that makes a compiled extension load-bearing
# rather than optional before it can merge.
#
# Usage: packaging/build-deb.sh [output-path.deb]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_NAME="send-to-influx"
BUILD_DIR="$(mktemp -d)"
PKG_ROOT="$BUILD_DIR/pkg"

# Use /usr/bin/python3 explicitly, not just "python3" from $PATH: during a CI
# build, $PATH's python3 can point at an ephemeral, tool-cache-specific
# interpreter (e.g. actions/setup-python's) that won't exist on the machine
# this .deb gets installed on, leaving the venv's own bin/python3 as a
# dangling symlink. /usr/bin/python3 is the FHS-standard location backed by
# the target's own `python3` package (see Depends: below), so it resolves
# correctly on any Debian/Ubuntu install target regardless of build host.
BUILD_PYTHON=/usr/bin/python3
if [ ! -x "$BUILD_PYTHON" ]; then
    echo "Error: /usr/bin/python3 not found. This script builds a distributable .deb, and a" >&2
    echo "fallback to whatever python3 happens to be on \$PATH would silently reintroduce a" >&2
    echo "non-portable interpreter symlink - run this on a real Debian/Ubuntu host instead." >&2
    exit 1
fi

cleanup() {
    rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

mkdir -p "$PKG_ROOT/DEBIAN" "$PKG_ROOT/opt/send-to-influx" "$PKG_ROOT/etc/send-to-influx" "$PKG_ROOT/lib/systemd/system"

echo "Building venv payload from $REPO_ROOT ..."
"$BUILD_PYTHON" -m venv "$PKG_ROOT/opt/send-to-influx/venv"
"$PKG_ROOT/opt/send-to-influx/venv/bin/pip" install --upgrade pip --quiet
"$PKG_ROOT/opt/send-to-influx/venv/bin/pip" install "$REPO_ROOT" --quiet

# pip bakes the venv's *build-time* staging path (this script's mktemp -d, not
# where the venv actually ends up once installed) into console-script shebangs
# and venv/bin/activate*'s VIRTUAL_ENV - rewrite them to the real install path,
# or the resulting .deb installs "successfully" but every entry point is
# non-executable ("cannot execute: required file not found", a missing
# shebang interpreter) and activate exports the wrong VIRTUAL_ENV.
VENV_STAGING_PATH="$PKG_ROOT/opt/send-to-influx/venv"
VENV_INSTALL_PATH="/opt/send-to-influx/venv"
grep -rl "$VENV_STAGING_PATH" "$VENV_STAGING_PATH/bin" 2>/dev/null | while IFS= read -r f; do
    sed -i.bak "s|$VENV_STAGING_PATH|$VENV_INSTALL_PATH|g" "$f"
done
find "$VENV_STAGING_PATH/bin" -name "*.bak" -delete

# A venv's site-packages lives at lib/pythonX.Y/site-packages, named after the exact
# major.minor of the interpreter that created it - a system python3 of a *different*
# minor version would still satisfy a loose "python3 (>= 3.10)" dependency, but would
# look for that directory under its own X.Y and find it missing (silent
# ModuleNotFoundError at runtime, not a clean dpkg/apt failure). Pin Depends: to the
# exact major.minor used here so apt can only install this onto a matching interpreter.
PYTHON_MAJOR_MINOR="$("$PKG_ROOT/opt/send-to-influx/venv/bin/python" -c \
    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_NEXT_MINOR="$("$PKG_ROOT/opt/send-to-influx/venv/bin/python" -c \
    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor + 1}")')"

# Strip any compiled extensions pip's dependency resolution happened to pull
# in (e.g. PyYAML's / charset-normalizer's optional C accelerators) - both
# have documented pure-Python fallbacks, and stripping these makes the
# resulting package genuinely architecture-independent regardless of what
# wheels were available on the build host.
find "$PKG_ROOT/opt/send-to-influx/venv/lib" -type f \( -name "*.so" -o -name "*.pyd" \) -delete

VERSION="$("$PKG_ROOT/opt/send-to-influx/venv/bin/python" -c \
    "from importlib.metadata import version; print(version('${PKG_NAME}'))")"

# Config (marked as a conffile below so dpkg preserves local edits on upgrade)
cp "$REPO_ROOT/example_settings.yaml" "$PKG_ROOT/etc/send-to-influx/settings.yaml"

# systemd unit and maintainer scripts
cp "$REPO_ROOT/packaging/send-to-influx.service" "$PKG_ROOT/lib/systemd/system/send-to-influx.service"
cp "$REPO_ROOT/packaging/postinst" "$PKG_ROOT/DEBIAN/postinst"
cp "$REPO_ROOT/packaging/prerm" "$PKG_ROOT/DEBIAN/prerm"
cp "$REPO_ROOT/packaging/postrm" "$PKG_ROOT/DEBIAN/postrm"
chmod 755 "$PKG_ROOT/DEBIAN/postinst" "$PKG_ROOT/DEBIAN/prerm" "$PKG_ROOT/DEBIAN/postrm"

cat > "$PKG_ROOT/DEBIAN/conffiles" <<CONFFILES
/etc/send-to-influx/settings.yaml
CONFFILES

cat > "$PKG_ROOT/DEBIAN/control" <<CONTROL
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: all
Depends: systemd, python3 (>= ${PYTHON_MAJOR_MINOR}), python3 (<< ${PYTHON_NEXT_MINOR})
Maintainer: Gavin Lucas
Description: Collects data from smart home / energy devices and writes it to InfluxDB
 send-to-influx polls Hue, MyEnergi, Octopus, Open-Meteo, National Grid Carbon
 Intensity and Speedtest sources and writes the results to InfluxDB using the
 line protocol, for visualisation in Grafana.
CONTROL

OUT_FILE="${1:-${REPO_ROOT}/${PKG_NAME}_${VERSION}_all.deb}"
dpkg-deb --build --root-owner-group "$PKG_ROOT" "$OUT_FILE"
echo "Built $OUT_FILE"
