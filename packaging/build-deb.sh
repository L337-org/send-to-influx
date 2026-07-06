#!/usr/bin/env bash
# Build a .deb package for send-to-influx.
#
# Bundles the app and its Python dependencies into a venv under
# /opt/send-to-influx, with a systemd unit to run it as a service. Must be
# run on the target architecture, since the venv bundles compiled dependency
# wheels (e.g. via pip) rather than pure source.
#
# Usage: packaging/build-deb.sh [output-path.deb]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_NAME="send-to-influx"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
BUILD_DIR="$(mktemp -d)"
PKG_ROOT="$BUILD_DIR/pkg"

cleanup() {
    rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

mkdir -p "$PKG_ROOT/DEBIAN" "$PKG_ROOT/opt/send-to-influx" "$PKG_ROOT/etc/send-to-influx" "$PKG_ROOT/lib/systemd/system"

echo "Building venv payload from $REPO_ROOT ..."
python3 -m venv "$PKG_ROOT/opt/send-to-influx/venv"
"$PKG_ROOT/opt/send-to-influx/venv/bin/pip" install --upgrade pip --quiet
"$PKG_ROOT/opt/send-to-influx/venv/bin/pip" install "$REPO_ROOT" --quiet

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
Architecture: ${ARCH}
Depends: systemd
Maintainer: Gavin Lucas
Description: Collects data from smart home / energy devices and writes it to InfluxDB
 send-to-influx polls Hue, MyEnergi, Octopus, Open-Meteo, National Grid Carbon
 Intensity and Speedtest sources and writes the results to InfluxDB using the
 line protocol, for visualisation in Grafana.
CONTROL

OUT_FILE="${1:-${REPO_ROOT}/${PKG_NAME}_${VERSION}_${ARCH}.deb}"
dpkg-deb --build --root-owner-group "$PKG_ROOT" "$OUT_FILE"
echo "Built $OUT_FILE"
