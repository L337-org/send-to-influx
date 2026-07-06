send-to-influx
===========

https://github.com/GavinLucas/send-to-influx
-----------------------------------------

Script to take data from various APIs and post it to InfluxDB in order to view the data in Grafana.

It currently supports Hue Bridges, MyEnergi Zappi/Eddi/Harvi devices, Open-Meteo weather, National Grid Carbon Intensity, Octopus Energy, and Speedtest network performance data sources.

For a full field-by-field reference of what each source collects and the units it's reported in, see [UNITS.md](UNITS.md).

Hue Bridge
----------

It will collect occupancy, temperature and light readings from Hue Motion Sensors, on/off state 
of Smart Plugs and the brightness of lights, but could be modified to collect other data from the bridge.

To create a username and password for the Hue Bridge, follow the instructions 
here: https://developers.meethue.com/develop/get-started-2/

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

Speedtest
---------

This uses the speedtest-cli python library and will run download and upload tests and store the results.

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

Alternatively, see [Running as a systemd service](#running-as-a-systemd-service) below for a
`.deb` package that installs and supervises it properly instead.

Log output goes to stdout with timestamps and log level, e.g.:

    2026-06-29 14:23:01 WARNING  Source 'hue' failed: connection timeout. Restarting in 5 seconds (attempt 1).

On startup, an INFO line logs the version and which source(s) will run, so restarts are visible in the logs:

    2026-06-29 14:23:00 INFO     Starting send-to-influx v1.0 (sources=hue, zappi, speedtest)

To also write logs to a file, add an optional `logfile` key to `settings.yaml`:

    logfile: "/var/log/send-to-influx.log"

By default the script looks for `settings.yaml` in the project root. To use a settings file at a
different location (e.g. `/etc/send-to-influx/settings.yaml` for a packaged install), pass
`--settings <path>`:

    sendtoinflux.py --settings /etc/send-to-influx/settings.yaml

`INFLUX_TOKEN` and `INFLUX_PASSWORD` environment variables, if set, override the corresponding
values in the `influx` settings block, so secrets need not be stored in the settings file itself.

By default, `sendtoinflux.py` starts one worker per source listed in the `sources` setting. Each source runs in its own loop using its own `interval`.

Worker start times are slightly staggered to avoid all collectors firing at exactly the same moment when intervals are equal.

If a source fails — whether running in single-source or multi-source mode — it is automatically restarted with exponential backoff (base 5 s, max 300 s) to avoid tight failure loops. In multi-source mode, only the failed source is retried; other sources keep running.

There are a few options that can be passed to the script and a couple of these can help you to debug and also to help you understand your data:

- To run only one data source, use the 'source' option, e.g. `sendtoinflux.py --source zappi`.
- To dump all the data from the Hue Bridge in order to see the names, etc., run `sendtoinflux.py --source hue --dump`
and it will output all the data returned as json. When `sources` is configured in `settings.yaml`, `--dump` must be
used together with `--source` so the script knows which source to dump.
- To print the data rather than send it to InfluxDB, run `sendtoinflux.py --source hue --print` and it will output the
parsed data structure as json.

Configuration options for multi-source mode:
- `sources`: list of source names to run in parallel when `--source` is not provided
- `stagger_seconds` (optional): delay between source starts (default `10`)
- failed source retries: exponential backoff with a 5 second base and 300 second maximum

Running as a systemd service
-----------------------------
Instead of a screen session, you can install send-to-influx as a systemd-managed service via a
`.deb` package, either built yourself or from the project's APT repo.

### Installing from the APT repo

    curl -fsSL https://gavinlucas.github.io/send-to-influx/send-to-influx.gpg | sudo tee /usr/share/keyrings/send-to-influx.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/send-to-influx.gpg] https://gavinlucas.github.io/send-to-influx/ ./" | sudo tee /etc/apt/sources.list.d/send-to-influx.list
    sudo apt update
    sudo apt install send-to-influx

The repo only keeps the last few releases' `.deb` files (older versions remain available on the
[Releases page](https://github.com/GavinLucas/send-to-influx/releases)), and is published by the
`apt-repo` job in `.github/workflows/release.yaml` on every tagged release.

### Building it yourself

    packaging/build-deb.sh
    sudo dpkg -i send-to-influx_*.deb

This must be built on the target architecture (it bundles the app and its Python dependencies
into a venv under `/opt/send-to-influx`, rather than pure source, so it's not architecture-agnostic).

### After installing

Either way, the package installs (but does not start) the service, since it needs configuration
first:

- Edit `/etc/send-to-influx/settings.yaml` (copied from `example_settings.yaml` on install; dpkg
  preserves your edits across upgrades).
- Optionally put `INFLUX_TOKEN`/`INFLUX_PASSWORD` in `/etc/send-to-influx/environment`
  (`KEY=VALUE`, one per line) instead of in the settings file - see the `--settings`/env var
  override notes above.
- Then enable and start it: `systemctl enable --now send-to-influx`

Logs go to the journal (`journalctl -u send-to-influx -f`) with the same timestamped format as
stdout above.

Usage
-----
>$ ./.venv/bin/python ./sendtoinflux.py --help  
>usage: sendtoinflux.py [-h] [--settings SETTINGS] [-d] [-p] [-s SOURCE]
>
>Send Hue Data to InfluxDB
>
>options:  
> &emsp; -h, --help            show this help message and exit  
> &emsp; --settings SETTINGS   path to the settings file (default: settings.yaml in the project root)  
> &emsp; -d, --dump            dump the data to the console one time and exit. This requires a source to be specified  
> &emsp; -p, --print           print the data rather than sending it to InfluxDB  
> &emsp; -s, --source SOURCE   the source of the data to send to InfluxDB (hue, zappi, etc.). If this parameter is omitted, all sources in the settings file
> &emsp;                       'sources' list are started. If no sources are specified in the settings file, the default source is used: hue

Running the unit tests
----------------------
The project uses pytest for unit tests. To run the tests:

- Create a virtual environment (recommended) and install development dependencies:
  `python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- Run the test suite: `pytest -v` (or `./.venv/bin/pytest -v` if using the venv above)

No real configuration or network access is required; tests use mocks for settings and HTTP. The same test suite runs in CI on every push and pull request.

Project Structure and How to Contribute
---------------------------------------

Most of the functionality is located in the 'toinflux' package.  This contains several modules each concerned with a different device type.

Pretty much all of the code is in a hierarchy of parent and child classes:

general.py - contains the function that returns an instance of the correct child class

influx.py - contains the top level parent class (DataHandler) which implements the common method for uploading to influx - send_data()

philipshue.py - contains a single child class (Hue) with all the functionality required to get data when calling the common method - get_data()

carbonintensity.py - contains a single child class (CarbonIntensity) that fetches national grid carbon intensity and optionally the generation fuel mix from the National Grid ESO API.

myenergi.py - contains an intermediate level child class (MyEnergi) with common functions for retrieving data from the myenergi APIs.  This class has three child classes: Zappi (EV charger), Eddi (hot water diverter), and Harvi (CT clamp energy monitor), each of which implements get_data().

openmeteo.py - contains a single child class (OpenMeteo) that fetches current weather observations from the Open-Meteo API (no API key required).

octopus.py - contains a single child class (Octopus) that fetches half-hourly electricity consumption and optionally the current unit rate from the Octopus Energy API.

speedtest.py - contains a single child class (Speedtest) with all the functionality required to get Speedtest data when calling the common method - get_data()

Unit tests for all the functions and classes are located in the 'tests' directory.

So to add a new device, if it's for an existing manufacturer, e.g. adding another MyEnergi device you can add a new sub-class to an existing file, otherwise add a new file with a class which is a child of DataHandler and exposes a get_data() method.

Don't forget to add imports for any new data collector classes to get_class() in general.py and \_\_init__.py, update the README.md and also add any required settings to example_settings.yaml

Also make sure you add unit tests for any functions or classes that you add.  Check that the existing tests still pass and check your linting before pushing changes to avoid CI failures.

Enjoy!