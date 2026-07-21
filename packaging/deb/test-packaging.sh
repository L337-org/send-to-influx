#!/bin/bash
# Scenario tests for the .deb's debconf/maintainer-script behaviour.
#
# Exercises the full install lifecycle against a built package: upgrade from
# the latest published release, fresh seeded install, upgrade silence,
# restart-on-upgrade, reconfigure semantics, question priority visibility,
# and purge. Everything asserted here is behaviour that regressed (or nearly
# regressed) at least once - see the commit history around PR #48.
#
# DESTRUCTIVE: installs/purges the package, creates the service user, and
# (where systemd is running) enables/starts/stops the service. Run it only on
# a disposable system - a CI runner (premerge.yaml's arm64-verify job) or a
# throwaway container, never a real install. Requires root.
#
# Usage: test-packaging.sh <path-to-built.deb>
#   HUE_TEST_SECRET       secret used for the seeded Hue credential
#                         (defaults to a random value; CI passes a masked one)
#   SKIP_RELEASE_UPGRADE  set to 1 to skip the released-package upgrade
#                         scenario (e.g. no network)
set -euo pipefail

DEB="${1:?usage: test-packaging.sh <path-to-built.deb>}"
DEB="$(cd "$(dirname "$DEB")" && pwd)/$(basename "$DEB")"
SETTINGS=/etc/send-to-influx/settings.yaml
CREDSTORE=/etc/send-to-influx/credstore.encrypted
HUE_TEST_SECRET="${HUE_TEST_SECRET:-test-hue-secret-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')}"
MQTT_TEST_SECRET="${MQTT_TEST_SECRET:-test-mqtt-secret-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')}"
MCP_TEST_SECRET="${MCP_TEST_SECRET:-test-mcp-secret-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')}"
export DEBIAN_FRONTEND=noninteractive

[ "$(id -u)" = 0 ] || { echo "must run as root (on a DISPOSABLE system)" >&2; exit 1; }

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

HAVE_SYSTEMD=0
[ -d /run/systemd/system ] && HAVE_SYSTEMD=1

# Give systemd-creds a host key if it doesn't have one yet (containers) -
# without this, credential migration is skipped rather than tested.
systemd-creds setup >/dev/null 2>&1 || true
CREDS_WORK=0
if printf 'probe' | systemd-creds encrypt --name=probe - - >/dev/null 2>&1; then
    CREDS_WORK=1
else
    echo "note: systemd-creds encryption unavailable - credential assertions will be relaxed"
fi

# grep -q exits 1 for "no match" (the outcome asserted here) but 2 for a real
# error (unreadable path) - a bare `! grep` would silently pass on an error
# without having checked anything. Assert the exact exit code instead.
assert_secret_absent() {
    local secret
    for secret in "$HUE_TEST_SECRET" "$MQTT_TEST_SECRET"; do
        set +e
        # -F --: the secret is a literal string, not a regex - a CI-provided
        # value containing metacharacters (or starting with a dash) must not
        # change what this asserts.
        grep -rqF -- "$secret" "$1"
        local status=$?
        set -e
        [ "$status" -eq 1 ] || fail "expected no match (exit 1) for a secret in $1, got exit $status"
    done
}

# Hue-only seeding - used against the *released* package, whose templates
# predate the nuki choice and the mqtt-* questions: preseeding answers for
# templates the old package doesn't ship (or a multiselect value outside its
# Choices) is undefined-behaviour territory, so the release-upgrade scenario
# sticks to what that package understands.
seed_answers() {
    debconf-set-selections <<EOF
send-to-influx send-to-influx/sources-to-configure multiselect hue
send-to-influx send-to-influx/influx-url string
send-to-influx send-to-influx/influx-identity string
send-to-influx send-to-influx/influx-secret password
send-to-influx send-to-influx/hue-host string ci-test-bridge.example.com
send-to-influx send-to-influx/hue-user password ${HUE_TEST_SECRET}
send-to-influx send-to-influx/hue-temperature-units select C
EOF
}

# Full seeding for the package under test: hue plus nuki, exercising the
# conditional shared-MQTT-block flow (broker fields applied, password migrated
# to systemd-creds) alongside the established hue path.
seed_answers_nuki() {
    seed_answers
    debconf-set-selections <<EOF
send-to-influx send-to-influx/sources-to-configure multiselect hue, nuki
send-to-influx send-to-influx/mqtt-broker-host string ci-mqtt-broker.example.com
send-to-influx send-to-influx/mqtt-username string ci-mqtt-reader
send-to-influx send-to-influx/mqtt-password password ${MQTT_TEST_SECRET}
send-to-influx send-to-influx/mcp-enable boolean true
send-to-influx send-to-influx/mcp-public-url string https://ci-mcp.example.org
send-to-influx send-to-influx/mcp-user string ci-mcp-user
send-to-influx send-to-influx/mcp-password password ${MCP_TEST_SECRET}
EOF
}

# $1 = description. Interactive-frontend install with no input available: any
# debconf prompt or dpkg conffile prompt would show up in the output (and get
# EOF), which is exactly what these upgrades must never produce.
upgrade_and_assert_silent() {
    local out
    out=$(DEBIAN_FRONTEND=teletype dpkg -i "$DEB" </dev/null 2>&1) || { echo "$out"; fail "$1: dpkg -i failed"; }
    echo "$out" | grep -qi "InfluxDB URL\|Hue bridge\|data sources do you want" && { echo "$out"; fail "$1: prompted debconf questions"; }
    echo "$out" | grep -q "(Y/I/N/O/D/Z)" && { echo "$out"; fail "$1: conffile prompt appeared"; }
    echo "$out" | grep -qi "not fully configured\|not provided - skipping" && { echo "$out"; fail "$1: emitted configuration warnings"; }
    echo "$out" | grep -q "send-to-influx upgraded" || { echo "$out"; fail "$1: upgrade message missing"; }
    LAST_UPGRADE_OUTPUT="$out"
}

# --- Scenario: upgrade over the latest published release ---------------------
# The released package is the conffile / db_unregister era - this proves the
# transition (obsolete-conffile handover, no re-prompt for the unregistered
# secret, config/credentials preserved) rather than only new->new upgrades.
if [ "${SKIP_RELEASE_UPGRADE:-0}" = 1 ]; then
    echo "note: skipping release-upgrade scenario (SKIP_RELEASE_UPGRADE=1)"
else
    OLD_DEB=""
    auth=()
    [ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
    old_url=$(curl -fsSL "${auth[@]}" https://api.github.com/repos/L337-org/send-to-influx/releases/latest 2>/dev/null \
        | grep -o 'https://[^"]*_all\.deb' | head -1) || true
    # No auth on the asset download: the token is only needed for API rate
    # limits, and the download redirects cross-host (older curl would forward
    # the Authorization header there, which the object store rejects).
    if [ -n "$old_url" ] && curl -fsSL -o /tmp/released.deb "$old_url"; then
        OLD_DEB=/tmp/released.deb
    fi
    if [ -z "$OLD_DEB" ]; then
        echo "note: skipping release-upgrade scenario (could not download the latest release .deb)"
    else
        echo "=== scenario: upgrade over the latest published release ($(dpkg-deb -f "$OLD_DEB" Version)) ==="
        seed_answers
        dpkg -i "$OLD_DEB" >/dev/null 2>&1 || dpkg -i "$OLD_DEB"
        sed -i 's/ci-test-bridge.example.com/hand-edited.example.com/' "$SETTINGS"
        sum_before=$(md5sum < "$SETTINGS")
        upgrade_and_assert_silent "release upgrade"
        [ "$(md5sum < "$SETTINGS")" = "$sum_before" ] || fail "release upgrade modified settings.yaml"
        # The venv's per-minor symlinks are created by postinst rather than shipped,
        # specifically so dpkg's cleanup of the previous version cannot resolve old
        # lib/python3.<minor>/... paths through them into the newly-unpacked tree -
        # which produced ~166 "unable to delete old directory" warnings on a release
        # upgrade. Assert both halves: the warnings are gone AND the symlinks that
        # make the venv usable on any supported interpreter are actually there.
        echo "$LAST_UPGRADE_OUTPUT" | grep -q "unable to delete old directory" \
            && { echo "$LAST_UPGRADE_OUTPUT" | grep -c "unable to delete"; fail "release upgrade printed stale-directory warnings"; }
        [ -d /opt/send-to-influx/venv/lib/python3 ] || fail "venv site-packages not at the version-independent path"
        for m in 10 12 30; do
            [ "$(readlink "/opt/send-to-influx/venv/lib/python3.$m")" = python3 ] \
                || fail "venv lib/python3.$m is not a symlink to python3"
        done
        if [ "$CREDS_WORK" = 1 ]; then
            [ -e "$CREDSTORE/hue-user.cred" ] || fail "stored credential lost across release upgrade"
        fi
        pass "release upgrade: silent, settings.yaml and credential preserved"

        # Simulate an install that predates the mqtt:/nuki: sections, regardless of
        # whether the currently-published "latest release" happens to already ship
        # them - it does as of 4.4, which made this scenario fail outright the
        # moment 4.4 became latest, since its own example_settings.yaml (copied
        # verbatim into settings.yaml on install) already has both sections
        # unconditionally. Relying on the real gap between "latest release" and
        # current dev content only works until a section they both already have
        # stops being new, so strip them here instead of asserting their absence.
        # Reconfiguring and selecting nuki must then back-fill both from the
        # shipped example - otherwise --set-field fails ("no 'mqtt:' section
        # found") and, worse, enabling the source writes it into sources: with no
        # nuki: block behind it, which makes load_settings() raise a fatal
        # ConfigError and stops the WHOLE service, taking every already-working
        # source down with it. Both were live bugs.
        # set -e does not trigger on a failing left side of && (it's exempt as
        # part of an and-or list), so a broken awk here would silently fall
        # through to mv never running - fail each step explicitly instead of
        # relying only on the content checks below to notice.
        awk '
            skip && /^[^ \t]/ { skip = 0 }
            /^mqtt:/ || /^nuki:/ { skip = 1; next }
            skip { next }
            { print }
        ' "$SETTINGS" > "$SETTINGS.tmp" || fail "test setup: awk failed to strip the mqtt:/nuki: sections"
        mv "$SETTINGS.tmp" "$SETTINGS" || fail "test setup: failed to replace settings.yaml with the stripped copy"
        grep -q "^mqtt:" "$SETTINGS" && fail "test setup: failed to strip the mqtt: section for the backfill scenario"
        grep -q "^nuki:" "$SETTINGS" && fail "test setup: failed to strip the nuki: section for the backfill scenario"
        debconf-set-selections <<EOF
send-to-influx send-to-influx/sources-to-configure multiselect nuki
send-to-influx send-to-influx/mqtt-broker-host string ci-mqtt-broker.example.com
send-to-influx send-to-influx/mqtt-username string ci-mqtt-reader
send-to-influx send-to-influx/mqtt-password password ${MQTT_TEST_SECRET}
EOF
        out=$(dpkg-reconfigure -fnoninteractive send-to-influx 2>&1) || { echo "$out"; fail "post-upgrade reconfigure failed"; }
        grep -q "^mqtt:" "$SETTINGS" || { echo "$out"; fail "mqtt: section not back-filled after upgrade"; }
        grep -q "^nuki:" "$SETTINGS" || { echo "$out"; fail "nuki: section not back-filled after upgrade"; }
        grep -q "ci-mqtt-broker.example.com" "$SETTINGS" || fail "mqtt.broker_host not written after back-fill"
        # dpkg-reconfigure runs preinst as "upgrade <version>" with no unpack
        # following it, so anything preinst deletes is gone for good - an earlier
        # version of it removed the whole venv here and permanently broke the install.
        [ -x /usr/sbin/send-to-influx-set-credential ] || fail "reconfigure destroyed the venv"
        /opt/send-to-influx/venv/bin/send-to-influx --settings "$SETTINGS" --check-config >/dev/null \
            || fail "settings.yaml no longer valid after post-upgrade reconfigure"
        pass "post-upgrade reconfigure: sections back-filled, venv intact, config still valid"

        dpkg -P send-to-influx >/dev/null 2>&1
        pass "released-then-upgraded install purged cleanly"
    fi
fi

# --- Scenario: fresh seeded install ------------------------------------------
echo "=== scenario: fresh seeded install ==="
seed_answers_nuki
dpkg -i "$DEB" >/dev/null 2>&1 || dpkg -i "$DEB"
/opt/send-to-influx/venv/bin/send-to-influx --version >/dev/null || fail "--version smoke test failed"
cp /usr/share/send-to-influx/example_settings.yaml /tmp/ci-settings.yaml
/opt/send-to-influx/venv/bin/send-to-influx --check-config --settings /tmp/ci-settings.yaml >/dev/null \
    || fail "--check-config smoke test failed on the shipped example settings"
[ -f "$SETTINGS" ] || fail "settings.yaml not created"
[ -f /usr/share/send-to-influx/example_settings.yaml ] || fail "example not shipped under /usr/share"
dpkg-deb -I "$DEB" conffiles >/dev/null 2>&1 && fail "package declares conffiles"
dpkg-deb -f "$DEB" Depends | grep -qw systemd && fail "package Depends on systemd"
[ "$(stat -c '%U:%G %a' "$SETTINGS")" = "send-to-influx:send-to-influx 644" ] || fail "settings.yaml owner/mode wrong"
[ "$(stat -c '%U' /opt/send-to-influx)" = root ] || fail "/opt/send-to-influx not root-owned"
/opt/send-to-influx/venv/bin/python3 - <<PYEOF
import yaml
with open("$SETTINGS", encoding="utf8") as f:
    data = yaml.safe_load(f)
assert data["hue"]["host"] == "ci-test-bridge.example.com", data["hue"]["host"]
assert data["mqtt"]["broker_host"] == "ci-mqtt-broker.example.com", data["mqtt"]["broker_host"]
assert data["mqtt"]["username"] == "ci-mqtt-reader", data["mqtt"]["username"]
# The MCP block (shared infrastructure, gated on its own mcp-enable boolean):
# public_url and user applied; password is a credential, so it must NOT be in
# plaintext here (asserted by assert_secret_absent below and the credstore check).
assert data["mcp"]["public_url"] == "https://ci-mcp.example.org", data["mcp"]["public_url"]
assert data["mcp"]["user"] == "ci-mcp-user", data["mcp"]["user"]
PYEOF
if [ "$CREDS_WORK" = 1 ]; then
    [ -e "$CREDSTORE/hue-user.cred" ] || fail "hue-user credential not migrated"
    [ -e "$CREDSTORE/mqtt-password.cred" ] || fail "mqtt-password credential not migrated"
    [ -e "$CREDSTORE/mcp-password.cred" ] || fail "mcp-password credential not migrated"
    grep -q "stored in systemd-creds" "$SETTINGS" || fail "hue.user not rewritten to the sentinel"
fi
# The seeded MCP secret must not leak into settings.yaml or debconf's database.
# -F (literal) and -- (end of options), like the assert_secret_absent helper: the
# secret is arbitrary text, so it must never be treated as a regex or a flag.
grep -qF -- "$MCP_TEST_SECRET" "$SETTINGS" && fail "MCP password leaked into settings.yaml"
grep -rqF -- "$MCP_TEST_SECRET" /var/cache/debconf/ && fail "MCP password left in debconf database"
assert_secret_absent "$SETTINGS"
assert_secret_absent /var/cache/debconf/
pass "fresh install: fields applied (incl. shared mqtt + mcp blocks), credentials migrated, secrets cleared everywhere"

# --- Scenario: plain upgrade is silent and touches nothing -------------------
echo "=== scenario: plain upgrade (interactive frontend, hand-edited config) ==="
sed -i 's/ci-test-bridge.example.com/hand-edited.example.com/' "$SETTINGS"
sum_before=$(md5sum < "$SETTINGS")
upgrade_and_assert_silent "plain upgrade"
[ "$(md5sum < "$SETTINGS")" = "$sum_before" ] || fail "plain upgrade modified settings.yaml"
echo "$LAST_UPGRADE_OUTPUT" | grep -q "was not running" || fail "expected the not-running upgrade message"
pass "plain upgrade: no prompts, no warnings, settings.yaml untouched"

# --- Scenario: a running service is restarted on upgrade ---------------------
if [ "$HAVE_SYSTEMD" = 1 ]; then
    echo "=== scenario: restart-on-upgrade ==="
    # The example config's placeholder values pass validation (workers fail
    # against the fake endpoints and retry with backoff), so the service
    # stays active without any real InfluxDB behind it.
    systemctl enable --now send-to-influx >/dev/null 2>&1
    sleep 3
    systemctl is-active --quiet send-to-influx || fail "service did not stay active on the example config"
    # The seeded settings.yaml has the MCP server enabled, so - when systemd-creds
    # actually migrated its password - it must be listening on its loopback port
    # under the FULL systemd sandbox: the hardening directives plus
    # LoadCredentialEncrypted decrypting mcp-password. This is the real test that
    # the hardened unit doesn't break the project's first network-facing server;
    # the unauthenticated OAuth-metadata endpoint is enough to prove it bound.
    if [ "$CREDS_WORK" = 1 ]; then
        mcp_up=0
        for _ in $(seq 1 15); do
            if curl -fsS http://127.0.0.1:8420/.well-known/oauth-authorization-server >/dev/null 2>&1; then
                mcp_up=1; break
            fi
            sleep 1
        done
        [ "$mcp_up" = 1 ] || fail "MCP server did not bind 127.0.0.1:8420 under the hardened systemd unit"
        pass "MCP server bound and served OAuth metadata under the hardened systemd sandbox"
    fi
    pid_before=$(systemctl show -p MainPID --value send-to-influx)
    upgrade_and_assert_silent "upgrade with running service"
    echo "$LAST_UPGRADE_OUTPUT" | grep -q "has been restarted" || fail "expected the restarted upgrade message"
    systemctl is-active --quiet send-to-influx || fail "service not active after restart-on-upgrade"
    pid_after=$(systemctl show -p MainPID --value send-to-influx)
    [ "$pid_before" != "$pid_after" ] || fail "MainPID unchanged - service was not actually restarted"
    pass "restart-on-upgrade: service restarted (pid $pid_before -> $pid_after) and active"
else
    echo "note: skipping restart-on-upgrade scenario (systemd not running here)"
fi

# --- Scenario: reconfigure applies answers; stored creds satisfy blank secrets
echo "=== scenario: dpkg-reconfigure ==="
out=$(dpkg-reconfigure -fnoninteractive send-to-influx 2>&1) || { echo "$out"; fail "reconfigure failed"; }
if [ "$CREDS_WORK" = 1 ]; then
    echo "$out" | grep -qi "Hue not fully configured" && { echo "$out"; fail "reconfigure warned despite stored hue-user credential"; }
    # The blank mqtt-password prompt must likewise be satisfied by the stored
    # credential (the shared-block analogue of the hue assertion above).
    echo "$out" | grep -qi "Nuki not fully configured" && { echo "$out"; fail "reconfigure warned despite stored mqtt-password credential"; }
fi
echo "$out" | grep -qi "InfluxDB user/org or password/token not provided" \
    || { echo "$out"; fail "expected the engaged-but-incomplete InfluxDB warning on reconfigure"; }
# Reconfigure (unlike an upgrade) deliberately re-asserts debconf's answers.
grep -q "ci-test-bridge.example.com" "$SETTINGS" || fail "reconfigure did not re-apply hue-host"
# The MCP block must survive reconfigure with the (blank-on-reconfigure) password
# satisfied by the stored mcp-password.cred: public_url/user are required strings
# that persist and pre-fill, so the server stays configured, not disabled. This is
# the password-only-rotation / blank-secret case that must not un-configure it.
if [ "$CREDS_WORK" = 1 ]; then
    echo "$out" | grep -qi "MCP server not fully configured" && { echo "$out"; fail "reconfigure un-configured MCP despite stored mcp-password and persisted url/user"; }
    grep -q "https://ci-mcp.example.org" "$SETTINGS" || fail "reconfigure did not keep mcp.public_url"
    grep -q "ci-mcp-user" "$SETTINGS" || fail "reconfigure did not keep mcp.user (required string must persist)"
fi
if [ "$HAVE_SYSTEMD" = 1 ]; then
    echo "$out" | grep -q "service restarted to apply the new configuration" \
        || { echo "$out"; fail "reconfigure did not report restarting the running service"; }
fi
pass "reconfigure: answers re-applied (incl. MCP), stored credential satisfied the blank secret"

if [ "$CREDS_WORK" = 1 ]; then
    # Migrate an InfluxDB token via the CLI (as an admin would), then
    # reconfigure with a changed URL and a blank secret: the stored token must
    # satisfy the secret requirement (no warning, auto-enable unblocked) AND
    # the new URL must still be applied - not silently dropped.
    printf 'ci-test-influx-token' | /usr/sbin/send-to-influx-set-credential influx-token >/dev/null
    debconf-set-selections <<'EOF'
send-to-influx send-to-influx/influx-url string http://influx-changed.example.com:8086
EOF
    out=$(dpkg-reconfigure -fnoninteractive send-to-influx 2>&1) || { echo "$out"; fail "reconfigure with stored token failed"; }
    echo "$out" | grep -qi "not provided - skipping" && { echo "$out"; fail "stored influx-token did not satisfy the blank secret"; }
    # --ensure-influx-storage is expected to fail here (the URL points at
    # nothing) - but it must fail on the *connection*, never on decrypting
    # its own stored credential. On systemd 250-253 (e.g. Raspberry Pi OS
    # bookworm, 252) an un-named `systemd-creds decrypt` derives the expected
    # credential name from the input filename WITHOUT stripping ".cred"
    # (only >= 254 strips it) and refuses the mismatch - a real-world 4.1
    # regression this assertion pins down on any such host.
    echo "$out" | grep -qi "decrypt failed" && { echo "$out"; fail "stored credential could not be decrypted by the CLI"; }
    grep -q "http://influx-changed.example.com:8086" "$SETTINGS" || fail "changed influx URL not applied alongside a stored token"
    pass "reconfigure: stored influx-token satisfied the blank secret and the changed URL was applied"

    # Rotation: a new secret with the identity left blank must re-encrypt the
    # stored credential (the common "new token, same org" case), not be
    # silently dropped.
    sum_cred=$(md5sum < "$CREDSTORE/influx-token.cred")
    debconf-set-selections <<'EOF'
send-to-influx send-to-influx/influx-secret password ci-rotated-token
EOF
    out=$(dpkg-reconfigure -fnoninteractive send-to-influx 2>&1) || { echo "$out"; fail "token-rotation reconfigure failed"; }
    [ "$(md5sum < "$CREDSTORE/influx-token.cred")" != "$sum_cred" ] || fail "secret entered with blank identity did not rotate the stored token"
    pass "reconfigure: a new secret alone rotates the stored influx-token"
fi

# --- Scenario: per-source questions are visible at debconf's default priority
echo "=== scenario: question visibility at priority high ==="
# teletype frontend at priority high (debconf's default threshold): blank the
# three InfluxDB prompts, pick "1" (hue) at the multiselect, blank the rest.
out=$(printf '\n\n\n1\n\n\n\n\n\n' | DEBIAN_FRONTEND=teletype dpkg-reconfigure -p high send-to-influx 2>&1) || true
echo "$out" | grep -q "Hue bridge hostname" || { echo "$out"; fail "hue-host not shown at priority high"; }
echo "$out" | grep -q "Hue bridge username" || fail "hue-user not shown at priority high"
echo "$out" | grep -q "Temperature units" || fail "hue-temperature-units not shown at priority high"
# The conditional shared-MQTT-block questions must NOT appear when no
# MQTT-based source is selected - the regression guard for the conditional
# gating (a non-MQTT install being prompted for a broker it doesn't have).
echo "$out" | grep -q "MQTT broker" && { echo "$out"; fail "mqtt questions shown without an MQTT source selected"; }
pass "per-source questions appear at priority high; mqtt questions correctly absent"

# Same frontend/priority, selecting nuki (choice 9) instead: the three
# shared-MQTT-block questions must now all be shown.
out=$(printf '\n\n\n9\n\n\n\n\n\n' | DEBIAN_FRONTEND=teletype dpkg-reconfigure -p high send-to-influx 2>&1) || true
echo "$out" | grep -q "MQTT broker hostname" || { echo "$out"; fail "mqtt-broker-host not shown at priority high with nuki selected"; }
echo "$out" | grep -q "MQTT broker username" || fail "mqtt-username not shown at priority high with nuki selected"
echo "$out" | grep -q "MQTT broker password" || fail "mqtt-password not shown at priority high with nuki selected"
pass "shared mqtt questions appear at priority high when an MQTT source is selected"

# --- Scenario: incoherent MQTT auth is not auto-enabled ----------------------
# A username with no password material would auto-enable straight into an
# authenticated connect with an empty password - a guaranteed CONNACK-rejection
# retry loop. Placed last (before purge) because it has to remove the stored
# credential to reach the guard; --remove is used rather than deleting the file,
# so the drop-in is regenerated first (a drop-in referencing a missing .cred
# hard-fails unit startup with 243/CREDENTIALS).
echo "=== scenario: incoherent MQTT auth (username, no password) ==="
if [ "$CREDS_WORK" = 1 ]; then
    /usr/sbin/send-to-influx-set-credential mqtt-password --remove >/dev/null
fi
debconf-set-selections <<'EOF'
send-to-influx send-to-influx/sources-to-configure multiselect nuki
send-to-influx send-to-influx/mqtt-broker-host string ci-mqtt-broker.example.com
send-to-influx send-to-influx/mqtt-username string ci-mqtt-reader
send-to-influx send-to-influx/mqtt-password password
EOF
out=$(dpkg-reconfigure -fnoninteractive send-to-influx 2>&1) || { echo "$out"; fail "incoherent-auth reconfigure failed"; }
echo "$out" | grep -qi "username given without a password" \
    || { echo "$out"; fail "expected the incoherent-auth warning instead of auto-enabling"; }
pass "incoherent MQTT auth warns instead of auto-enabling"

# --- Scenario: purge -----------------------------------------------------------
echo "=== scenario: purge ==="
dpkg -P send-to-influx >/dev/null 2>&1 || dpkg -P send-to-influx
[ ! -e /etc/send-to-influx ] || fail "/etc/send-to-influx survived purge"
[ ! -e /etc/systemd/system/send-to-influx.service.d ] || fail "systemd drop-in directory survived purge"
getent passwd send-to-influx >/dev/null 2>&1 && fail "service user survived purge"
if [ "$HAVE_SYSTEMD" = 1 ]; then
    systemctl is-active --quiet send-to-influx 2>/dev/null && fail "service still active after purge"
fi
debconf-show send-to-influx 2>/dev/null | grep -q . && fail "debconf answers survived purge"
# postinst creates the venv's python3.X symlinks itself, so dpkg does not own them
# and will not remove them - without postrm's cleanup /opt/send-to-influx survives a
# purge as a directory of dangling symlinks.
[ ! -e /opt/send-to-influx ] || fail "/opt/send-to-influx survived purge: $(ls -R /opt/send-to-influx | head -5)"
pass "purge: config, credentials, drop-in, user, and debconf answers all removed"

echo "ALL PACKAGING SCENARIOS PASSED"
