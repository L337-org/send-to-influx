send-to-influx
===========

https://github.com/L337-org/send-to-influx
-----------------------------------------

Collect metrics from your smart-home and energy devices into InfluxDB, and then really take control!

The built-in [MCP server](#remote-mcp-server) means that you can use your AI of choice as one place
to **control** your devices, query their **live and historical** state, and ask **complex questions**
about **relationships between different measurements**, and **spot trends** in your data.

Because the data is stored in InfluxDB it can also be **displayed in Grafana** dashboards and other
visualization tools that support it.

It currently supports Hue Bridges, MyEnergi Zappi/Eddi/Harvi devices, Open-Meteo weather, National Grid
Carbon Intensity, Octopus Energy, Nuki smart locks, and Speedtest network performance data sources.
Most are read-only; Hue lights/plugs and on-demand Speedtest runs can be controlled through the MCP server
when you opt in.

It can be installed as a .deb package on supported platforms, or run directly from the source code in a
Python virtual environment.

For a full field-by-field reference of what each source collects and the units it's reported in, see
[UNITS.md](UNITS.md).

Contents
--------

- Data sources
  - [Hue Bridge](#hue-bridge)
  - [MyEnergi Zappi / Eddi / Harvi](#myenergi-zappi--eddi--harvi)
  - [UK National Grid Carbon Intensity](#uk-national-grid-carbon-intensity)
  - [Open-Meteo](#open-meteo)
  - [Octopus Energy](#octopus-energy)
  - [Nuki Smart Lock (MQTT)](#nuki-smart-lock-mqtt)
  - [Speedtest](#speedtest)
- [InfluxDB setup](#influxdb)
- [Running the script](#running-the-script)
- [Using the .deb package](#using-the-deb-package)
- [Remote MCP server (query and control your devices via an MCP client like Claude)](#remote-mcp-server)
- [Usage / CLI reference](#usage)
- [Contributing](#contributing)
- [Privacy and Security](#privacy-and-security)

Hue Bridge
----------

It will collect occupancy, temperature and light readings from Hue Motion Sensors, on/off state 
of Smart Plugs and the brightness of lights, but could be modified to collect other data from the bridge.

To create a username and password for the Hue Bridge, follow the instructions 
here: https://developers.meethue.com/develop/get-started-2/

Hue bridges are commonly reached over `https` with a self-signed local certificate, so TLS
verification is skipped by default (`insecure: true`). If your bridge has a valid certificate,
set `insecure: false` in the `hue` settings block to enable verification.

MyEnergi Zappi / Eddi / Harvi
-----------------------------

Information on how to obtain your API key is available here:
https://support.myenergi.com/hc/en-gb/articles/5069627351185-How-do-I-get-an-API-key

Some information on the API fields is available here:
https://github.com/twonk/MyEnergi-App-Api

- **Zappi**: EV charger. Collects real-time status fields plus daily energy totals (Charge, Import, Export, Genera).
- **Harvi**: CT clamp energy monitor. Collects CT clamp power readings (ectp1/ectp2/ectp3) and channel names.
- **Eddi**: Hot water diverter. Collects real-time status fields (frequency, voltage, diversion amount, temperatures, etc.).

UK National Grid Carbon Intensity
------------------------------

Real-time national grid carbon intensity (gCO2/kWh) and generation fuel mix (wind, solar, gas, nuclear, etc.)
from the National Grid ESO Carbon Intensity API. No API key required. Data updates every 30 minutes.

API documentation: https://carbon-intensity.github.io/api-definitions/

Set `include_generation: true` in settings to also collect the fuel mix percentages.

Open-Meteo
----------

Free weather data with no API key required. Configure latitude/longitude and choose which fields to collect
from the `current` weather variables (see https://open-meteo.com/en/docs for the full list).
Recommended interval is 15 minutes (900 seconds) or longer.

Octopus Energy
--------------

Collects half-hourly electricity consumption from your smart meter via the Octopus Energy API.
Your API key is available from https://octopus.energy/dashboard/developer/.

If you also configure `gas_mprn` and `gas_meter_serial`, gas consumption is collected too, as
`gas_consumption` (see [UNITS.md](UNITS.md) for its unit, which depends on your meter type).

If you are on a time-of-use tariff (e.g. Agile Octopus), you can also configure `product_code` and
`tariff_code` to collect the current electricity unit rate alongside consumption.

Note: smart meter readings are typically delayed by up to 24 hours, so consumption data always
represents a recent-past reading rather than real-time usage.

Nuki Smart Lock (MQTT)
----------------------

Collects lock state and door-sensor state (plus battery and connectivity fields) from WiFi-connected
Nuki smart locks via Nuki's local MQTT API (supported by the Smart Lock 3.0 Pro on firmware 3.5.12+
and all newer WiFi models). Nuki devices publish every state topic with the MQTT retain flag set, so
each collection cycle simply subscribes briefly and receives the last-known state of every lock
provisioned to the broker - locks need no per-device configuration in `settings.yaml`, and each
lock's fields are prefixed with its own name from the Nuki app (give each lock a distinct name).

This needs an MQTT broker on your LAN, e.g. Mosquitto. Note that Nuki devices only support plain,
unencrypted MQTT (port 1883) to a private-range LAN address - only use this on a network you trust.

Setting up Mosquitto on Debian/Ubuntu:

1. `sudo apt install mosquitto mosquitto-clients` - the client tools (used below to verify the
   setup) are a separate package from the broker, not bundled with it.
2. Create `/etc/mosquitto/conf.d/local.conf` (the filename must end in `.conf` or it is silently
   ignored):

       listener 1883 0.0.0.0
       allow_anonymous false
       password_file /etc/mosquitto/passwd

3. Create two separate broker credentials - one for the lock (publisher) and one for
   send-to-influx (subscriber). Don't reuse one login for both; separate credentials mean either
   can be rotated or revoked independently:

       sudo mosquitto_passwd -c /etc/mosquitto/passwd nuki-lock
       sudo mosquitto_passwd /etc/mosquitto/passwd sendtoinflux

   Create the password file *before* restarting Mosquitto - it refuses to start if the configured
   `password_file` doesn't exist yet.
4. The file is created owned `root:root` mode 600, which the `mosquitto` service user can't read,
   so fix the group and mode:

       sudo chgrp mosquitto /etc/mosquitto/passwd
       sudo chmod 640 /etc/mosquitto/passwd

   `mosquitto_passwd` will warn about this ownership on later runs ("group is not root ...") -
   that's a known upstream rough edge; do *not* follow its suggested `chown root`/`chmod 0700`
   fix, which stops the broker being able to read the file at all.
5. Restart Mosquitto (`sudo systemctl restart mosquitto`).
6. In the Nuki app: Device Administration > MQTT - enter the broker's address, the `nuki-lock`
   credentials, and turn **"Allow locking" off** so the lock only publishes state and never
   subscribes to command topics (this integration is read-only regardless).
7. Verify end-to-end with the `sendtoinflux` credential (the one that goes in `settings.yaml`):

       mosquitto_sub -h <broker-host> -u sendtoinflux -P <password> -t 'nuki/#' -v

   This should immediately print every retained state topic for the lock.

Then fill in the shared `mqtt:` block in `settings.yaml` with the `sendtoinflux` credential and add
`nuki` to `sources:`. On the packaged install the broker password can be moved into `systemd-creds`
with `send-to-influx-set-credential mqtt-password`, like any other credential.

If your broker permits anonymous access, you can instead leave `username` and `password` blank -
the two-credential setup above is the recommended default, not a requirement. To switch an
already-authenticated install to anonymous later, blank `mqtt.username` in `settings.yaml` and run
`sudo send-to-influx-set-credential mqtt-password --remove`. The install prompts can't do it, since
a blank answer there means "keep the current value".

Speedtest
---------

This uses the speedtest-cli python library and will run download and upload tests and store the results.

If every latency probe to a candidate server fails, speedtest-cli reports a nonsensical `ping`
(hardcoded penalty values averaged in place of real samples - can come out as high as 1,800,000 ms)
rather than raising an error. speedtest-cli times each latency probe with a hardcoded 10-second
connection timeout, so no genuine measurement can exceed 5000 ms; any `ping` >= 5000 ms is treated
as an implausible/failed measurement and raises a connection error (retried with backoff) instead
of being written to InfluxDB.

InfluxDB
--------

Both InfluxDB v1 and v2 are supported.

For v1, configure `user` and `password` in the `influx` settings block and use `db` per source:

    influx:
      url: "http://influx.example.com:8086"
      user: "your_user"
      password: "your_password"

For v2, use `token` and `org` instead and optionally use `bucket` per source (falls back to `db` if `bucket` is not set):

    influx:
      url: "http://influx.example.com:8086"
      token: "your_token"
      org: "your_org"

If your InfluxDB URL uses `https` with a self-signed or internally-issued certificate, TLS certificate
verification will fail by default. Set `insecure: true` in the `influx` settings block to skip verification:

    influx:
      url: "https://influx.example.com:8086"
      user: "your_user"
      password: "your_password"
      insecure: true

Speedtest-cli is installed as a requirement of this project so no additional download is required.

Information about speedtest-cli is available on their project page:
https://github.com/sivel/speedtest-cli

Running the script
------------------
- copy example_settings.yaml to settings.yaml
  - Change the permissions of the file, e.g. `chmod 600 settings.yaml`, so that it's not readable 
  by other users
  - Fill in the values for your devices and InfluxDB
- Install runtime requirements with `pip install -r requirements.txt`
- Leave the script running in a screen session and sit back and watch the data roll in.

Alternatively, see [Using the .deb package](#using-the-deb-package) below for a
`.deb` package that installs and runs it under systemd instead.

Log output goes to stdout with timestamps and log level, e.g.:

    2026-06-29 14:23:01 WARNING  Source 'hue' failed: connection timeout. Restarting in 5 seconds (attempt 1).

On startup, an INFO line logs the version and which source(s) will run, so restarts are visible in the logs:

    2026-06-29 14:23:00 INFO     Starting send-to-influx v1.0 (sources=hue, zappi, speedtest)

To also write logs to a file, add an optional `logfile` key to `settings.yaml`. The file is rotated automatically
once it reaches `log_max_bytes` (default 10 MiB), keeping `log_backup_count` old copies (default 3):

    logfile: "/var/log/send-to-influx.log"
    # log_max_bytes: 10485760
    # log_backup_count: 3

If you're [using the .deb package](#using-the-deb-package), you shouldn't need this at all: the
package ships its own separate mechanism for a dedicated logfile, described below, which takes the
service's messages out of the shared `daemon.log`/`syslog` entirely rather than adding a second copy
alongside it. Setting `logfile` yourself on a packaged install is possible (the packaged service's
`ProtectSystem=strict` sandboxing means `/var/log/...` isn't writable by it directly, so point it at
a path under `/etc/send-to-influx/`, the one directory the service can write to, or add your own path
to `ReadWritePaths=` in `packaging/send-to-influx.service` and rebuild) - but don't point it at
`/var/log/send-to-influx.log` itself, since that's the same file the packaging-level mechanism below
already writes to, and you'd end up with two independent writers to one file.

The log level defaults to `INFO`. Set an optional `loglevel` key in `settings.yaml` (e.g. `DEBUG`, `WARNING`), or
pass `-v`/`--verbose` on the command line to force `DEBUG` regardless of what's configured:

    loglevel: "INFO"

By default the script looks for `settings.yaml` in the project root. To use a settings file at a
different location (e.g. `/etc/send-to-influx/settings.yaml` for a packaged install), pass
`--settings <path>`:

    sendtoinflux.py --settings /etc/send-to-influx/settings.yaml

By default, `sendtoinflux.py` starts one worker per source listed in the `sources` setting. Each source runs in its own loop using its own `interval`.

Worker start times are slightly staggered to avoid all collectors firing at exactly the same moment when intervals are equal.

If a source hits a transient failure (e.g. a network error talking to its API or to InfluxDB) — whether running in single-source or multi-source mode — it is automatically restarted with exponential backoff (base 5 s, max 300 s) to avoid tight failure loops. In multi-source mode, only the failed source is retried; other sources keep running. A configuration problem (e.g. a source missing its settings section) is not retried: in single-source mode the process exits with code 1; in multi-source mode that source's worker stops permanently and a critical line is logged, while other sources keep running.

If InfluxDB itself is briefly unreachable, a point that fails to write is buffered in memory (per source, up to a few hundred points) rather than dropped, and sent automatically — oldest first, batched into a handful of requests — once InfluxDB is reachable again, so a short outage delays data rather than losing it. A point the server itself repeatedly rejects (for example, one that has aged past the bucket's retention window) is given up on after a few attempts rather than blocking the points queued behind it. The buffer is in-memory only: it does not survive a process restart, and if the settings file's InfluxDB destination is changed while a backlog exists, the backlog is delivered to the new destination. Heartbeat status points are never buffered — they are a live signal, so a failed one is simply dropped.

After every collection cycle (success or failure), a `collector_status,source=<name>` heartbeat point is written to InfluxDB alongside the source's own data, with fields `ok` (`1`/`0`) and `consecutive_failures`. A dead collector would otherwise only show up as a silent gap in Grafana; this gives you a positive signal to alert on (e.g. `ok == 0` or a stale `collector_status` point). Heartbeats are not written in `--print` mode, since that mode never sends anything to InfluxDB.

There are a few options that can be passed to the script and a couple of these can help you to debug and also to help you understand your data:

- To run only one data source, use the 'source' option, e.g. `sendtoinflux.py --source zappi`.
- To dump all the data from the Hue Bridge in order to see the names, etc., run `sendtoinflux.py --source hue --dump`
and it will output all the data returned as json. When `sources` is configured in `settings.yaml`, `--dump` must be
used together with `--source` so the script knows which source to dump.
- To print the data rather than send it to InfluxDB, run `sendtoinflux.py --source hue --print` and it will output the
parsed data structure as json.
- To check `settings.yaml` is valid without starting any collectors, run `sendtoinflux.py --check-config`. It prints
`Configuration OK` and exits 0, or exits 1 with details of what's wrong.
- To print the installed version, run `sendtoinflux.py --version`.

Configuration options for multi-source mode:
- `sources`: list of source names to run in parallel when `--source` is not provided
- `stagger_seconds` (optional): delay between source starts (default `10`)
- failed source retries: exponential backoff with a 5 second base and 300 second maximum

Using the .deb package
----------------------
Instead of a screen session, you can install send-to-influx as a systemd-managed service via a
`.deb` package.

### Installing from the APT repo

    curl -fsSL https://apt.l337.org/send-to-influx.gpg | sudo tee /usr/share/keyrings/send-to-influx.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/send-to-influx.gpg] https://apt.l337.org/ ./" | sudo tee /etc/apt/sources.list.d/send-to-influx.list
    sudo apt update
    sudo apt install send-to-influx

The repo only keeps the last few releases' `.deb` files (older versions remain available on the
[Releases page](https://github.com/L337-org/send-to-influx/releases)), and is maintained by
[L337-org/apt](https://github.com/L337-org/apt), which aggregates `.deb` release assets from
L337-org projects' GitHub Releases (new releases appear there within the hour).

### Building it yourself

    packaging/deb/build-deb.sh
    sudo dpkg -i send-to-influx_*.deb

The package is architecture-independent (`all`) - the app and its dependencies are pure Python,
and any optional compiled accelerators (e.g. PyYAML's) are stripped from the bundled venv at build
time in favour of their pure-Python fallbacks - so it can be built on any Debian/Ubuntu machine
(the script requires `/usr/bin/python3` and `dpkg-deb`) and installed on any Debian/Ubuntu
architecture, including arm64 (e.g. Raspberry Pi). CI builds the package on an arm64 runner on
every push/PR (a required status check) and runs `packaging/deb/test-packaging.sh` against it - a
scenario suite covering upgrade over the latest published release, a fresh debconf-seeded install,
plain-upgrade silence over a hand-edited config, restart-on-upgrade of a running service,
`dpkg-reconfigure` semantics, question visibility at debconf's default priority, and purge - as
well as catching a future dependency change that would make a compiled extension load-bearing
rather than optional before it can merge. The same suite also runs in a Debian 12 container
(systemd 252, matching Raspberry Pi OS bookworm), so behaviour differences in older systemd-creds
tooling are covered too - the runners' own newer systemd once masked exactly such a difference.

A venv's `site-packages` normally lives under `lib/pythonX.Y/`, named after the exact major.minor
of the interpreter that created it - which would otherwise tie an installable target to whatever
Python version happened to be on the build host (e.g. GitHub's CI runner image) rather than
whatever the *install target* actually has, and those two drift out of sync over time. Since
everything in the venv is pure Python (see above), `build-deb.sh` symlinks every minor from 3.10
through 3.30's `lib/pythonX.Y` to the one that was actually populated, and declares a matching
`python3 (>= 3.10), python3 (<< 3.31)` dependency, so the package installs correctly regardless of
which minor in that range the target's `/usr/bin/python3` happens to be (the upper bound keeps the
dependency and the symlink range from silently drifting apart - both are bumped together if Python
ever gets close to 3.30).

### After installing

Either way, the package installs (but does not start) the service, since it needs configuration
first:

- Edit `/etc/send-to-influx/settings.yaml` (created from `example_settings.yaml` the first time the
  package is installed, and never touched by upgrades - only a fresh install or your own edits, or
  the debconf/`send-to-influx-set-credential` flows described below, ever write to it).
- Then enable and start it: `systemctl enable --now send-to-influx`

On upgrades, a *running* service is automatically restarted so it actually picks up the new
version - upgrades are often unattended (cron/apt timers), where a "please restart" hint in the
package output would never be seen, and the old code would otherwise keep running until the next
reboot. A stopped service is left stopped; an upgrade never starts anything.

Logs go to the journal (`journalctl -u send-to-influx -f`) with the same timestamped format as
stdout above. Journald also forwards to syslog as usual, and the package ships rsyslog and
logrotate config (`/etc/rsyslog.d/49-send-to-influx.conf`, `/etc/logrotate.d/send-to-influx`,
`Recommends:`, not `Depends:` - the service works via the journal alone either way) that redirects
this service's messages out of the shared `daemon.log`/`syslog` into their own
`/var/log/send-to-influx.log`. Nothing needs configuring for this; the redirect is on by default
whenever rsyslog is installed, and daily rotation (kept for 7 days) - the same pattern used by
packages like haproxy - additionally needs logrotate, which runs it automatically once installed;
without logrotate the file is still written correctly, just not rotated. Edit either file directly
(both are real, editable config files, preserved across upgrades) to change the retention or
filter.

### Configuring during install (debconf)

A fresh interactive install of the `.deb` presents a debconf prompt: your InfluxDB
connection details first (asked unconditionally, regardless of what else you answer - useful on
its own if you just want to move an existing InfluxDB
credential into `systemd-creds` without touching anything else), then a checklist of which data
sources you want to configure now, then - only for the ones you pick - the fields needed to
actually reach that source's API (credentials plus things like a Hue bridge hostname or an Octopus
meter number; tuning settings like intervals keep their shipped defaults and can be adjusted in
`settings.yaml` afterwards). You're also asked, with a default of "no", whether to enable the
[remote MCP server](#remote-mcp-server) - answer yes and it collects the public URL, login username
and password for it (the password goes into `systemd-creds` like any other). A plain package
*upgrade* never prompts and never applies debconf
answers: your `settings.yaml` and credentials are only ever written by a fresh install's prompts or
by an explicit `sudo dpkg-reconfigure send-to-influx`, which re-runs the same flow at any time.
Secrets you enter are moved into `systemd-creds` (see below)
and never written into `settings.yaml` in plaintext. Debconf itself briefly holds what you type in
its own separate, `chmod 600` password store while `postinst` runs, then clears each
password-type answer back to the empty string immediately after reading it - regardless of whether
the subsequent migration
into `systemd-creds` goes on to succeed - see SECURITY.md if you want the detail. Non-secret answers
(a Hue bridge hostname, an Octopus meter number, and so on) aren't password-type and stay in
debconf's regular database as normal, same as any other package's debconf answers - with one
exception: the InfluxDB user/organisation answer is cleared like the secrets are, since for a v1
install it's the InfluxDB username, which is treated as a credential everywhere else. If every required
field for a source was answered *and* your InfluxDB connection details resolved successfully, it's
automatically added to `sources:` in `settings.yaml` and the InfluxDB database/bucket it needs is
created for you where possible - a source with everything else filled in still won't be enabled if
InfluxDB itself couldn't be reached or authenticated. When you revisit the prompts with
`dpkg-reconfigure`, a secret left blank counts as provided if it's already stored in
`systemd-creds`, so adding a new source never requires re-entering credentials that are already
migrated.

You can leave every question blank and configure `settings.yaml` by hand instead - nothing here is
required. Re-run `sudo dpkg-reconfigure send-to-influx` at any time to change your answers; secret
prompts always come back blank on a reconfigure (debconf's own UI convention for password-type
questions - it doesn't redisplay a previous answer), so leaving one blank keeps whatever's already
stored rather than clearing it.

### Storing secrets in systemd-creds

By default, `settings.yaml` holds credentials (InfluxDB token/user/password, Hue bridge user,
MyEnergi API key, Octopus API key) in plaintext, same as the source-checkout path. On the packaged
install (requires `systemd >= 250`), you can instead move them into
[systemd-creds](https://systemd.io/CREDENTIALS/) - encrypted at rest with a TPM-bound or host-derived
key, decrypted only into a restricted, in-memory location for the service's lifetime:

    sudo send-to-influx-set-credential influx-token
    sudo send-to-influx-set-credential --list

Run `send-to-influx-set-credential --list` to see the full set of available credential names. Each
`set` call prompts for the value (or reads it from stdin if piped, e.g.
`echo -n "$TOKEN" | sudo send-to-influx-set-credential influx-token`) and replaces the plaintext value
in `settings.yaml` with a note that it's now managed elsewhere - the file stays readable for the rest
of its content, but that field is never used from it again. Use
`send-to-influx-set-credential <name> --remove` to remove a credential from systemd-creds again -
note this resets the `settings.yaml` field to its placeholder rather than restoring the secret (the
encrypted value is destroyed, not decrypted back into the file), so re-enter the value by hand
afterwards if you still need it.

This is entirely optional and per-field - you can mix systemd-creds and plaintext values freely, and
the source-checkout/screen-session path is unaffected either way, since systemd-creds only applies
under the packaged systemd service.

Remote MCP server
-----------------

send-to-influx can optionally run a remote [MCP](https://modelcontextprotocol.io/) server inside the
same process, so an MCP client — Claude Desktop or Claude Mobile, or another — can ask
natural-language questions about your devices via a custom connector. It is disabled unless **both**
`user` and `password` are set in the `mcp:` settings block - credentials-present is the primary
enablement mechanism. Set `mcp.disabled: true` to force it off regardless of user/password state -
useful if the password was migrated to systemd-creds (blanking the YAML fields alone won't disable
it there, since the stored credential still gets substituted in at load time) or simply to rule the
server in or out while troubleshooting something else.

```yaml
mcp:
  bind_address: "127.0.0.1:8420"   # loopback/private only - any-interface (0.0.0.0/::) and public IPs are refused
  public_url: "https://mcp.example.org"  # the external HTTPS address your reverse proxy serves
  user: "your-login-name"
  password: "your-login-password"
```

Key points:

- **You provide the TLS side.** The server itself speaks plain HTTP on a private/loopback address;
  put your own TLS-terminating reverse proxy (nginx, Caddy, ...) in front of it and set
  `public_url` to the address the proxy serves. Binding a public interface is refused outright: the
  server speaks plain HTTP — OAuth login and tokens included — so it must sit behind your
  TLS-terminating proxy, never face the internet directly.
- **Authentication is OAuth 2.1** (what Claude's connector UI uses, and the remote-MCP standard):
  add the connector in your MCP client (Claude, say) with URL `https://mcp.example.org/mcp`, and it
  registers itself and sends you to a login page gated on `mcp.user`/`mcp.password`. Leave the
  client's OAuth **Client ID blank** - the server uses dynamic client registration, and filling one
  in makes the client skip registration so `/authorize` rejects it. Failed logins are throttled and
  logged. On the packaged install the password can be stored in systemd-creds:
  `sudo send-to-influx-set-credential mcp-password`.
- **Connections survive restarts.** OAuth client registrations and refresh tokens are persisted
  (hashed) in `mcp-oauth-state.json` next to the settings file (override with `mcp.state_file`),
  so package upgrades and reboots don't make the client re-authenticate.
- **Read-only by default.** Device-control tools are opt-in per collector and aren't registered at
  all unless enabled in that collector's own settings block - when none is enabled, the write tools
  don't exist on the server.

**Example nginx config.** Give the server its own subdomain and serve it at the root - its OAuth
flow uses several root-level routes (`/authorize`, `/token`, `/.well-known/oauth-*`, `/login`), so a
sub-path of a shared site won't work:

```nginx
server {
    listen 443 ssl http2;                      # 'listen ... http2' works on old and new nginx;
    listen [::]:443 ssl http2;                 # newer nginx (1.25.1+) prefers a separate 'http2 on;'
    server_name mcp.example.org;               # must match the host in mcp.public_url

    ssl_certificate     /etc/letsencrypt/live/mcp.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.example.org/privkey.pem;

    location / {
        # Literal 127.0.0.1, not 'localhost' - 'localhost' can resolve to ::1 while the
        # server listens on IPv4, which gives a 502. No trailing slash on proxy_pass.
        proxy_pass http://127.0.0.1:8420;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto https;

        # Streamable-HTTP / SSE: don't buffer, keep the upstream connection alive,
        # and allow long-lived streams.
        proxy_http_version 1.1;
        proxy_set_header   Connection "";
        proxy_buffering    off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Two things that catch people out:

- **`public_url` must be a bare origin** (`https://host[:port]`, no path). The server refuses to
  start with a path in it, since the OAuth metadata is built from it - so use a subdomain
  (`https://mcp.example.org`), not a path prefix (`https://example.org/mcp`).
- **Leave the OAuth Client ID blank** in the connector (see the OAuth point above) - the server
  registers clients dynamically, and a manually-set client id makes `/authorize` reject it.

Each controllable collector adds its own tools when you opt it in - writes differ too much between
device types to share one tool.

To allow controlling Hue lights and plugs, set `hue.mcp_read_write: true`. That adds:

- **`hue_list_devices`** - the controllable lights/plugs, each with its id, name, and which controls
  it supports (on/off, brightness, colour temperature, colour).
- **`hue_set_light`** - a real physical action: turn a light/plug on or off, set brightness
  (0-100 %), white colour temperature (in kelvin), or colour (an `#rrggbb` hex or a name like "warm
  white"). It's capability-aware - asking a plain white bulb for a colour is refused rather than
  ignored - and setting brightness/temperature/colour turns the light on unless you turn it off.

To allow triggering a speed test on demand, set `speedtest.mcp_read_write: true`. That adds:

- **`speedtest_run`** - runs a speed test now, on the host running the server, and returns the
  result (also recording it like a scheduled run). Only one runs at a time per host.

Once connected, the client has these read tools:

- **`list_sources`** - the collectors that can be queried, each with a short description.
- **`list_fields`** - the fields available for a source, with their units and (for coded fields
  like Nuki lock state) what each value means.
- **`get_current_state`** - a source's state *right now* ("is the door locked?", "which lights are
  on?"). Most sources are read live from the device; Speedtest and Octopus report their latest
  recorded reading instead.
- **`query_history`** - a field's history over a time range, either as individual points or
  aggregated (mean/max/min/sum/count/…) into buckets ("how much electricity this month vs last?",
  "when did the light go off?").
- **`get_documentation`** - a one-call reference of what every source reports and what its coded
  values mean.

Results are domain-aware throughout: values carry their unit, and coded fields come back with a
decoded label alongside the raw number. The same data is also exposed as MCP **resources** (a
documentation reference, and a live-state and schema resource per source) for clients that use them.

The server also offers a few **prompts** - ready-made tasks you can pick in the client: *home status*
(summarise everything now), *usage trends* (historical analysis, e.g. "electricity this month vs
last"), and, when device control is enabled, *control device*.

History queries run against InfluxDB through a fixed, parameterised query builder - never a raw
query from the model - with field names checked against the measurement's live field list, time
ranges normalised in the app, and a capped result size.

Usage
-----
>$ ./.venv/bin/python ./sendtoinflux.py --help  
>usage: sendtoinflux.py [-h] [--version] [--settings SETTINGS] [--check-config] [-v] [-d] [-p] [-s SOURCE]
>
>Send Hue Data to InfluxDB
>
>options:  
> &emsp; -h, --help            show this help message and exit  
> &emsp; --version             show program's version number and exit  
> &emsp; --settings SETTINGS   path to the settings file (default: settings.yaml in the project root)  
> &emsp; --check-config        validate settings.yaml and exit (0 if valid, 1 if invalid)  
> &emsp; -v, --verbose         enable DEBUG-level logging (overrides the 'loglevel' settings.yaml key)  
> &emsp; -d, --dump            dump the data to the console one time and exit. This requires a source to be specified  
> &emsp; -p, --print           print the raw data rather than sending it to InfluxDB  
> &emsp; -s, --source SOURCE   the source of the data to send to InfluxDB (hue, zappi, etc.). If this parameter is omitted, all sources in the settings file
> &emsp;                       'sources' list are started. If no sources are specified in the settings file, the 'default_source' settings key is used.

Contributing
------------

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the project layout, code
conventions, the checklist for adding a new data source, and local development setup.

Privacy and Security
---------------------
`send-to-influx` sends no telemetry and has no author-operated backend - see
[PRIVACY.md](PRIVACY.md) for what data it does handle (yours, sent only to the InfluxDB and
device/API endpoints you configure) and [SECURITY.md](SECURITY.md) for reporting a vulnerability
and operational security notes (credential storage, TLS verification defaults).