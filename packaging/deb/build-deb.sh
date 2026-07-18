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
# Usage: packaging/deb/build-deb.sh [output-path.deb]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Refuse to build from a tree containing stale setuptools artefacts. `pip install
# "$REPO_ROOT"` runs setuptools' build_py, which copies package sources into
# build/lib and SKIPS any file already there that looks newer - so a leftover
# build/ directory silently ships the code it holds instead of the code in the
# tree. This is not hypothetical: a locally-built package shipped pre-Nuki library
# code under a 4.4 version banner, producing "Source nuki not found" and
# "send_data() got an unexpected keyword argument use_buffer" at runtime while
# every file in the working tree was correct. Aborting is right rather than
# deleting them: they are the developer's, and silently shipping the wrong code is
# far worse than one clear message. CI is unaffected (it builds fresh checkouts);
# scripts/dev-build-deb.sh strips them from its own build copy.
for stale in "$REPO_ROOT/build" "$REPO_ROOT"/*.egg-info; do
    if [ -e "$stale" ]; then
        echo "error: stale Python build artefact: $stale" >&2
        echo "It would be used in preference to the current sources, shipping the wrong code." >&2
        echo "Remove it and re-run:  rm -rf '$REPO_ROOT/build' '$REPO_ROOT'/*.egg-info" >&2
        exit 1
    fi
done
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

# /etc/send-to-influx ships as an (empty) directory even though settings.yaml
# itself is no longer packaged (see the /usr/share example note below): dpkg
# then keeps owning the directory across upgrades from versions that *did*
# ship a file inside it, instead of warning "unable to delete old directory:
# Directory not empty" on the first upgrade past that change.
mkdir -p "$PKG_ROOT/DEBIAN" "$PKG_ROOT/opt/send-to-influx" "$PKG_ROOT/usr/share/send-to-influx" \
    "$PKG_ROOT/etc/send-to-influx" "$PKG_ROOT/lib/systemd/system" "$PKG_ROOT/usr/sbin"

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
# -I skips binary files (e.g. the python3 symlink target); the process substitution
# (rather than a literal `| while`) plus `|| true` keeps `grep` finding zero matches
# from tripping `set -o pipefail` and aborting the whole build.
while IFS= read -r f; do
    sed -i.bak "s|$VENV_STAGING_PATH|$VENV_INSTALL_PATH|g" "$f"
done < <(grep -rlI "$VENV_STAGING_PATH" "$VENV_STAGING_PATH/bin" 2>/dev/null || true)
find "$VENV_STAGING_PATH/bin" -name "*.bak" -delete

# venv/bin isn't on $PATH, so send-to-influx-set-credential (meant for direct human/
# debconf invocation, unlike the main send-to-influx binary which is only ever
# invoked via the systemd unit's absolute ExecStart= path) needs a $PATH-reachable
# symlink. Shipped as part of the package's own file tree (built here, at package
# build time) rather than created imperatively by postinst - /usr/local is reserved
# for locally-installed, non-package-managed software (FHS/Debian policy), so
# /usr/sbin is the correct target; and a dpkg-tracked file gets removed automatically
# on both `dpkg -r` and `--purge`, unlike a symlink postinst/postrm would otherwise
# have to create and clean up by hand outside dpkg's own file-list tracking.
ln -s /opt/send-to-influx/venv/bin/send-to-influx-set-credential "$PKG_ROOT/usr/sbin/send-to-influx-set-credential"

# Strip any compiled extensions pip's dependency resolution happened to pull
# in (e.g. PyYAML's / charset-normalizer's optional C accelerators) - both
# have documented pure-Python fallbacks, and stripping these makes the
# resulting package genuinely architecture-independent regardless of what
# wheels were available on the build host.
find "$PKG_ROOT/opt/send-to-influx/venv/lib" -type f \( -name "*.so" -o -name "*.pyd" \) -delete

# A venv's site-packages lives at lib/pythonX.Y/site-packages, named after the exact
# major.minor of the interpreter that created it - so a target system whose python3 is
# a *different* minor than the build host's would normally find that directory missing
# (silent ModuleNotFoundError at runtime). An earlier version of this script "fixed"
# that by pinning Depends: to the exact build-time major.minor - but that just traded
# one failure for another: it made the package installable only on systems whose python3
# happens to match whatever GitHub's CI runner image ships *at build time*, which drifts
# out of sync with real target systems (confirmed in practice: built against 3.12,
# rejected on a target running 3.13). Since everything left in the venv after the
# .so-stripping above is pure Python (no version-specific compiled ABI code), any 3.10+
# interpreter can safely use the exact same site-packages - the only thing tying it to
# one minor is the directory name. Symlink every other plausible minor to the real one so
# apt can use a plain floor (Depends: below) instead of a brittle exact-version pin.
# PYTHON_MAX_SUPPORTED_MINOR bounds both the symlink range and Depends:'s upper bound
# together (below) - keeping them as one source of truth, since a target whose python3
# is newer than the last symlinked minor would otherwise hit the exact same missing-
# site-packages failure this change exists to prevent, just at a higher version number.
PYTHON_MAJOR_MINOR="$("$PKG_ROOT/opt/send-to-influx/venv/bin/python" -c \
    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_MAX_SUPPORTED_MINOR=30
VENV_LIB_DIR="$PKG_ROOT/opt/send-to-influx/venv/lib"
# Give the real site-packages directory a version-INDEPENDENT name and point every
# supported minor at it as a symlink, rather than leaving the real directory named
# after whichever interpreter happened to build the package.
#
# The reason is dpkg, not Python: with a version-named real directory, a package
# built against a different Python minor than the installed one needs the real
# directory and one of the symlinks to swap places, and dpkg cannot reliably
# replace a directory with a symlink or vice versa. In practice it emits a long
# list of "unable to delete old directory ... Directory not empty" warnings and can
# leave the OLD directory shadowing the new one. With this layout the topology is
# byte-identical no matter which interpreter built the package - lib/python3 is
# always the real directory, every lib/python3.X is always a symlink - so there is
# never a swap to perform. (preinst still clears the venv on upgrade, which is what
# gets installs created under the old scheme onto this one.)
mv "$VENV_LIB_DIR/python${PYTHON_MAJOR_MINOR}" "$VENV_LIB_DIR/python3"
# The per-minor python3.X -> python3 symlinks are deliberately NOT shipped in the
# package; postinst creates them (and postrm removes them). If they were shipped,
# they would exist during dpkg's post-unpack cleanup of the previous version's
# files, so every old lib/python3.<built-minor>/... path would resolve through the
# new symlink into the freshly-unpacked tree - which is populated, so each rmdir
# fails and dpkg prints "unable to delete old directory ... Directory not empty".
# That is ~166 alarming warnings on an upgrade that is in fact completely fine (no
# file is lost - verified by diffing the package contents against disk afterwards).
# Creating them after the unpack sidesteps it entirely: at cleanup time the paths
# simply do not exist, so there is nothing to warn about.

VERSION="$("$PKG_ROOT/opt/send-to-influx/venv/bin/python" -c \
    "from importlib.metadata import version; print(version('${PKG_NAME}'))")"
# Optional override for locally-built dev packages (scripts/dev-build-deb.sh),
# which stamp a "~dev<timestamp>.g<sha>" version so an installed dev build is
# distinguishable from the release. "~" sorts BEFORE the bare version in Debian
# ordering, so the real 4.4 release still upgrades cleanly over a 4.4~dev build.
# Never set by CI or release.yaml - those always ship the pyproject version.
VERSION="${DEB_VERSION_OVERRIDE:-$VERSION}"

# The example settings ship under /usr/share; postinst copies them to
# /etc/send-to-influx/settings.yaml only if that file doesn't exist yet.
# Deliberately NOT a dpkg conffile: postinst and send-to-influx-set-credential
# write debconf answers/sentinels into settings.yaml, and dpkg's conffile
# machinery treats any such write as a local modification - guaranteeing a
# confusing "modified (by you or by a script)" prompt on every upgrade that
# ships a changed example_settings.yaml, with a one-keypress path to replacing
# a fully-configured file with the pristine example. Debian Policy 10.7.3
# forbids maintainer scripts modifying conffiles for exactly this reason; this
# is the Policy-blessed alternative ("configuration files" managed by the
# maintainer scripts, removed by postrm on purge). Upgrades never touch the
# file at all - the earlier conffile-shipped copy simply goes obsolete in
# dpkg's records and stays in place on disk.
cp "$REPO_ROOT/example_settings.yaml" "$PKG_ROOT/usr/share/send-to-influx/example_settings.yaml"

# systemd unit (format-agnostic, stays at the top of packaging/) and .deb-specific maintainer scripts
cp "$REPO_ROOT/packaging/send-to-influx.service" "$PKG_ROOT/lib/systemd/system/send-to-influx.service"
cp "$REPO_ROOT/packaging/deb/preinst" "$PKG_ROOT/DEBIAN/preinst"
cp "$REPO_ROOT/packaging/deb/postinst" "$PKG_ROOT/DEBIAN/postinst"
cp "$REPO_ROOT/packaging/deb/prerm" "$PKG_ROOT/DEBIAN/prerm"
cp "$REPO_ROOT/packaging/deb/postrm" "$PKG_ROOT/DEBIAN/postrm"
cp "$REPO_ROOT/packaging/deb/config" "$PKG_ROOT/DEBIAN/config"
cp "$REPO_ROOT/packaging/deb/send-to-influx.templates" "$PKG_ROOT/DEBIAN/templates"
chmod 755 "$PKG_ROOT/DEBIAN/preinst" "$PKG_ROOT/DEBIAN/postinst" "$PKG_ROOT/DEBIAN/prerm" "$PKG_ROOT/DEBIAN/postrm" "$PKG_ROOT/DEBIAN/config"

# No hard Depends: on systemd - shipping a unit file doesn't require it
# (Debian packages with units conventionally don't depend on an init), every
# systemctl call in the maintainer scripts is already guarded on
# /run/systemd/system (systemd running, not merely installed - the guard a
# hard dependency couldn't replace anyway, e.g. in a chroot/container), and
# systemd-creds availability is checked at runtime with its own specific
# error message (see CLAUDE.md's "Credential storage" section).
cat > "$PKG_ROOT/DEBIAN/control" <<CONTROL
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: all
Depends: debconf (>= 0.5), python3 (>= 3.10), python3 (<< 3.$((PYTHON_MAX_SUPPORTED_MINOR + 1)))
Suggests: mosquitto, mosquitto-clients
Maintainer: Gavin Lucas
Description: Collects data from smart home / energy devices and writes it to InfluxDB
 send-to-influx polls Hue, MyEnergi, Octopus, Open-Meteo, National Grid Carbon
 Intensity, Nuki smart locks (via MQTT) and Speedtest sources and writes the
 results to InfluxDB using the line protocol, for visualisation in Grafana.
 .
 The Nuki source needs an MQTT broker on the local network - mosquitto (and
 mosquitto-clients, for verifying the setup) are suggested rather than
 depended on, since the broker may equally run on a different host.
CONTROL

OUT_FILE="${1:-${REPO_ROOT}/${PKG_NAME}_${VERSION}_all.deb}"
dpkg-deb --build --root-owner-group "$PKG_ROOT" "$OUT_FILE"
echo "Built $OUT_FILE"
