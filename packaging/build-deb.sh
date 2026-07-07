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
# on an arm64 runner on every merge to main, to catch a future dependency
# change that makes a compiled extension load-bearing rather than optional.
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
    echo "Warning: /usr/bin/python3 not found, falling back to \$PATH's python3." >&2
    echo "Fine for local testing, but the resulting venv's interpreter symlink may not be portable." >&2
    BUILD_PYTHON=python3
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
Depends: systemd, python3 (>= 3.10)
Maintainer: Gavin Lucas
Description: Collects data from smart home / energy devices and writes it to InfluxDB
 send-to-influx polls Hue, MyEnergi, Octopus, Open-Meteo, National Grid Carbon
 Intensity and Speedtest sources and writes the results to InfluxDB using the
 line protocol, for visualisation in Grafana.
CONTROL

OUT_FILE="${1:-${REPO_ROOT}/${PKG_NAME}_${VERSION}_all.deb}"
dpkg-deb --build --root-owner-group "$PKG_ROOT" "$OUT_FILE"
echo "Built $OUT_FILE"
