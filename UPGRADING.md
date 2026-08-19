# Upgrading send-to-influx

Version-specific steps needed when moving between releases. Anything not listed here upgrades
with no action: `settings.yaml` is never rewritten by an upgrade, and new configuration is
optional with a safe default.

## 5.2 to 5.3

Two changes affect data already in InfluxDB. Only the Nuki one needs anything done, and only
if you collect from Nuki locks.

### Nuki locks: the lock moves from the field key to a tag

**Who this affects:** anyone with `nuki` in `sources:`. If you do not collect from Nuki, skip
this section entirely.

Before 5.3, every lock's state was written as one point per collection cycle, with the lock's
name built into each field key:

```
nuki,host=mqtt.example.com Front_Door_Lock_stateValue=1,Front_Door_Lock_batteryChargeState=51 ...
```

That made the lock impossible to query as a dimension. You could chart one lock by naming its
field keys, but you could not ask "which locks are unlocked" without knowing every lock's name
in advance and parsing it back out of the key.

From 5.3 each lock is its own point, tagged with the lock's name, and the field keys are bare:

```
nuki,device=Front_Door_Lock stateValue=1,batteryChargeState=51 ...
```

The broker `host` tag is gone. All locks arrive through one broker, so it identified nothing
useful, and changing broker should not change the data.

**What happens if you do nothing:** the collector starts writing the new shape immediately and
keeps working. Your existing history stays exactly where it is, in the old shape. Nothing is
lost and nothing breaks - but the old and new data are in different series, so a Grafana panel
built on `Front_Door_Lock_stateValue` stops gaining new points, and a panel built on the new
shape shows no history. The migration below joins them back together.

#### Before you start

- **Take a backup.** The rewrite phase adds data and removes nothing, so it is safe on its own,
  but the delete phase is irreversible. If your InfluxDB is not backed up, back it up first.
- **Find the script.** On a packaged install it is at
  `/usr/share/send-to-influx/migrate-nuki-device-tag.py`. In a source checkout it is at
  `scripts/migrate-nuki-device-tag.py`.
- **Run it with the interpreter that has `requests`.** The script needs `requests`, and the
  package deliberately bundles its dependencies in its own virtualenv rather than depending on
  system Python packages - so run it with `/opt/send-to-influx/venv/bin/python3`, as the
  examples below do. A bare `python3` works only if that machine happens to have
  `python3-requests` installed. In a source checkout, use that checkout's `.venv/bin/python`.
- **Know your InfluxDB details.** You need the URL, the database (v1) or bucket (v2) holding
  the `nuki` measurement, and a credential that can both read and write it. Both versions are
  supported, with one difference at the very last step: see the note under "Phase 2" about v2
  having no `DROP SERIES`. The script asks for
  the credential every time and never reads it from `settings.yaml` or systemd-creds - see
  "Why it asks for the credential" below.
- **You can leave the collector running.** The migration writes to different series from the
  ones it reads, and re-writing a point that already exists is an overwrite, so a collection
  cycle landing mid-migration is harmless.
- **Run it once, from one host.** If several machines collect into the same database, the
  migration only needs running once against that database, not once per machine.

The credential is supplied either interactively or on stdin. For InfluxDB v1 it is
`username:password`; for v2 it is a token:

```bash
# interactive, masked prompt
/opt/send-to-influx/venv/bin/python3 \
    /usr/share/send-to-influx/migrate-nuki-device-tag.py rewrite \
    --url http://influx.example.com:8086 --database home --dry-run

# or piped, if you keep it in a password manager
pass influx/token | /opt/send-to-influx/venv/bin/python3 \
    /usr/share/send-to-influx/migrate-nuki-device-tag.py rewrite \
    --url http://influx.example.com:8086 --database home --dry-run
```

Add `--insecure` if your InfluxDB uses a certificate the host does not trust, and `--no-auth`
if it needs no credential at all.

#### Phase 1: rewrite

Always dry-run first. It reads and reports, and writes nothing:

```bash
/opt/send-to-influx/venv/bin/python3 \
    /usr/share/send-to-influx/migrate-nuki-device-tag.py rewrite \
    --url http://influx.example.com:8086 --database home --dry-run
```

You should see a line per lock, and a sample of what would be written:

```
Found 17 pre-5.3 field key(s).
Read 8034 old point(s), producing 16068 new point(s):
  1A2B3C4D: 8034 point(s) from 2 field key(s)
  Front_Door_Lock: 8034 point(s) from 15 field key(s)
```

Check the lock names are the ones you expect. A lock that never published its name to the
broker appears under its Nuki device ID instead - that is normal, and it is the name your old
field keys already used.

If the run stops with `cannot split field key ...`, **it has written nothing.** It found a
field key it cannot attribute to a lock, and it refuses to continue rather than skip it and
report success - a skipped key would be data silently left behind, which the delete phase
would then remove. Report the key name rather than working around it.

Then run it for real, naming a manifest file to write:

```bash
/opt/send-to-influx/venv/bin/python3 \
    /usr/share/send-to-influx/migrate-nuki-device-tag.py rewrite \
    --url http://influx.example.com:8086 --database home \
    --manifest ~/nuki-migration-manifest.json
```

**Keep that manifest.** The delete phase is driven by it, and will not run without it.

#### Verify before deleting anything

This is the point of the two phases: the old data is still there, so you can compare. There
is no time limit - phase 2 can wait as long as you like.

In Grafana or via the MCP tools, check that:

- Each lock now appears as a `device` tag value on the `nuki` measurement.
- A lock's history goes as far back as it used to. Chart `stateValue` grouped by `device` over
  the longest range you have data for, and confirm it does not start at the moment you ran the
  migration.
- The new points and the old ones agree where they overlap. `stateValue` for a lock should read
  the same whether you chart the new `stateValue` grouped by `device` or the old
  `Front_Door_Lock_stateValue`.
- Text fields are still text. `firmware` should read `3.10.7`, not a number.

The MCP tools will do this too, if you have the server enabled:

```
list_fields("nuki")                 -> bare field names, no lock prefixes
get_current_state("nuki")           -> one entry per lock, keyed by lock name
query_history("nuki", "stateValue") -> per-lock results
```

**If something looks wrong, stop here.** Do not run phase 2. The old data is untouched, so you
can delete the migration's own output instead and be exactly where you started.

On InfluxDB v1:

```
DROP SERIES FROM "nuki" WHERE "device" != ''
```

On InfluxDB v2, which has no `DROP SERIES`, delete by predicate instead - substituting your
organisation, bucket and token. This removes the series carrying a `device` tag, which is
exactly what the migration wrote, and leaves your original `host`-tagged history alone:

```bash
curl -X POST 'https://influx.example.com/api/v2/delete?org=YOUR_ORG&bucket=YOUR_BUCKET' \
    -H 'Authorization: Token YOUR_TOKEN' \
    -H 'Content-Type: application/json' \
    -d '{"start": "1970-01-01T00:00:00Z", "stop": "2100-01-01T00:00:00Z",
         "predicate": "_measurement=\"nuki\" AND device!=\"\""}'
```

#### Phase 2: delete the old series

Only once you are satisfied. This is irreversible.

```bash
/opt/send-to-influx/venv/bin/python3 \
    /usr/share/send-to-influx/migrate-nuki-device-tag.py delete \
    --url http://influx.example.com:8086 --database home \
    --manifest ~/nuki-migration-manifest.json
```

It prints what it is about to drop, then asks you to type `delete` to confirm. `--dry-run`
shows the same summary and drops nothing; `--yes` skips the prompt, for a scripted run.

**On InfluxDB v2 this last step is done by hand.** v2 has no `DROP SERIES` - its
v1-compatibility endpoint answers "not implemented" - so the script stops without deleting
anything and prints the `/api/v2/delete` request that does work, one per host, ready to run
once you substitute your organisation and token. It does not run that for you because it needs
your organisation, which the script cannot know and must not guess for a delete that cannot be
undone. Everything before this point, including the rewrite and the manifest, works on v2
exactly as on v1.

It drops only the series the manifest recorded, identified by the old broker `host` tag. The
migrated points carry a `device` tag instead, so they are in different series and are not
touched. It is never a blanket delete of the measurement - that would destroy the migration's
own output along with the history.

Afterwards, update any Grafana panel still referring to a prefixed field key such as
`Front_Door_Lock_stateValue`: use `stateValue` with a `device` tag filter, or group by `device`.

#### Why it asks for the credential

The script reads no configuration and no credentials of its own, on any install type, and this
is deliberate rather than a limitation. On a packaged install the credentials live in
systemd-creds and are only materialised for the service, so a hand-run script would not see
them anyway - but falling back to reading `settings.yaml` where it happens to be readable would
mean the safeguard existed on packaged installs and not in a source checkout. Supplying the
credential each time makes "someone consciously authorised this rewrite" a property of the tool
rather than of how you installed it.

For the same reason it is not on `$PATH` and is never run by the package or the service.
`postinst` runs unattended during unattended-upgrades, which is the worst possible trigger for
rewriting data; several collector hosts can share one database, so an upgrade-triggered run
would fire once per host and race on the same measurement; and the service may not own the
database at all.

### The MCP server's OAuth state moves to /var/lib

**Who this affects:** anyone running the MCP server on the packaged install. No action is
needed, and if you have been re-authorising the connector after upgrades, this is why.

The OAuth state file holds the client registration and a hashed refresh token, so a restart
should be invisible to a connected client: the access token expires after an hour, but the
refresh token lasts 90 days and the client renews silently.

That never worked on the packaged install. The service runs as `send-to-influx`, while
`/etc/send-to-influx` is owned by root - so creating the state file there, and the temporary
file its atomic write needs beside it, failed with a permission error on every save. The
failure was logged and the server carried on without persistence, so the only visible symptom
was having to re-authorise the client after every restart. In practice that means after every
upgrade, since that is when the service restarts.

The file now lives in `/var/lib/send-to-influx`, created by systemd from the unit's
`StateDirectory=`, owned by the service user and mode 0700 because it holds tokens. `/etc`
stays root-owned: the fix moves the state out rather than opening the configuration directory
up to the service.

If you already had a working state file - possible on an install old enough to predate a
change that stopped `postinst` taking ownership of the whole directory - `postinst` moves it
across for you, so you will not be asked to re-authorise.

Running the script by hand is unchanged. `STATE_DIRECTORY` is set only by systemd for a unit
that declares it, so a source checkout or a screen session still keeps the file next to
`settings.yaml`, where whoever started the process can write it. An explicit `mcp.state_file`
still overrides both.

**You will need to re-authorise the connector once**, on the upgrade that first gives the
server somewhere writable - there is no earlier state to carry across.

### Speedtest heartbeats gain a host tag

**Who this affects:** anyone running `speedtest` collectors on more than one machine into the
same database. No action is needed.

Before 5.3 every host wrote its heartbeat as `collector_status,source=speedtest` with nothing
to tell them apart, so two collectors overwrote each other and one dying looked exactly like a
healthy estate. From 5.3 the heartbeat carries the same `host` tag the speedtest data itself
has always carried.

Heartbeat points written before the upgrade sit in an untagged series and are not migrated.
That is deliberate: a heartbeat is a liveness signal with no analytical value, and the old
points were already wrong, since the hosts were overwriting one another. If you have a Grafana
alert on `collector_status` for speedtest, add `host` to its grouping so it alerts per machine.
