# Data Units Reference

This document lists the fields collected by each source and the units they are reported in.
It reflects what the API returns; `send-to-influx` does not convert or rescale any values
unless explicitly noted below.

The optional MCP server's read tools surface these units and coded-value meanings to Claude
(see each source class's `MCP_FIELD_METADATA` in `toinflux/`); keep the two in step when adding
a source or field. The server's `get_documentation` tool / `docs://reference` resource generate
a machine-readable version of this from `MCP_FIELD_METADATA` plus each source's `MCP_DESCRIPTION`,
so those class attributes - not this file - are what the model actually reads.

## Hue Bridge (`hue`)

| Field | Unit | Notes |
|---|---|---|
| Temperature sensors | °C, °F or K | Set via `temperature_units` in settings (default `C`), rounded to 2 decimal places |
| Light level sensors | lux | Converted from the Hue `lightlevel` raw value |
| Motion/presence sensors | boolean (0/1) | 1 = movement detected |
| Smart plugs | boolean (0/1) | 1 = on |
| Dimmable lights | % (0-100) | Brightness percentage |

Every **Hue** point carries a `host` tag holding the bridge it came from, exactly as written
in `settings.yaml` (this tag is specific to Hue - other sources are unaffected). With more than one bridge configured, that tag is what separates them:
field names are unchanged and unprefixed, so two bridges with a light of the same name write
the same field key under different `host` tags. Filter or group by `host` to separate them,
and note that replacing a bridge with one at a different address starts a new series - see
the README's "Multiple Hue bridges" section.

The `collector_status` heartbeat for `hue` also carries a `host` tag, so each bridge's
`ok`/`consecutive_failures` are recorded separately. Grouping by `source` alone aggregates
across bridges, which can hide a failing one behind a healthy one - group by `source, host`
(or `*`) for a series per bridge.

Every **MyEnergi** point carries a `device` tag. With a single device configured per type it
holds the type name (`zappi`, `eddi`, `harvi`), exactly as it always has. Configure more than
one device of a type and the tag holds the `label` you gave each one, so the tag identifies
the device rather than the type - filter or group by `device` to separate them. Labels must be
unique across the three blocks, since all three types write to the one `myenergi` measurement.
Note that renaming a label starts a new series, and that a decommissioned device's history
stays under its old label.

The `collector_status` heartbeat for a MyEnergi source also carries the `device` tag, so each
device's `ok`/`consecutive_failures` are recorded separately rather than several devices
overwriting one another at second precision. Heartbeat points written before this existed sit
in an untagged series.

## MyEnergi Zappi (`zappi`)

| Field | Unit | Notes |
|---|---|---|
| `frq` | Hz | Supply frequency |
| `vol` | raw MyEnergi API value | Supply voltage, passed through unconverted; some MyEnergi docs describe this field as deciVolts (divide by 10 for volts), but this project does not rescale it - verify against your device's actual voltage |
| `gen` | W | Generated (solar) power |
| `grd` | W | Power from/to the grid |
| `che` | kWh | Energy transferred so far this session |
| `sta` | numeric status code | Not a physical unit |
| `wifiLink`, `ethernetLink` | N/A | Diagnostic/status fields, not documented as physical units |
| `newAppAvailable`, `newBootloaderAvailable` | boolean (0/1) | Update-available flags |
| `Charge`, `Import`, `Export`, `Genera` | kWh | Daily totals computed by this project from the day/hour endpoint's raw values (divided by 3,600,000 and rounded to 4 dp); always collected regardless of `fields` |

## MyEnergi Eddi (`eddi`)

| Field | Unit | Notes |
|---|---|---|
| `frq` | Hz | Supply frequency |
| `vol` | raw MyEnergi API value | Supply voltage, passed through unconverted; some MyEnergi docs describe this field as deciVolts (divide by 10 for volts), but this project does not rescale it - verify against your device's actual voltage |
| `div` | W | Diversion (heating) power |
| `sta` | numeric status code | Not a physical unit |
| `hno` | 1 or 2 | Currently active heater number |
| `che` | kWh | Energy diverted so far today |
| `tp1`, `tp2` | °C | Tank temperature probes |

## MyEnergi Harvi (`harvi`)

| Field | Unit | Notes |
|---|---|---|
| `ectp1`, `ectp2`, `ectp3` | W | CT clamp power readings |
| `ectt1`, `ectt2`, `ectt3` | text label | CT clamp channel names (e.g. `"Grid"`), not numeric |

## UK National Grid Carbon Intensity (`carbonintensity`)

| Field | Unit | Notes |
|---|---|---|
| `intensity_actual`, `intensity_forecast` | gCO2/kWh | From the `/intensity` endpoint |
| `gen_<fuel>` (e.g. `gen_wind`, `gen_gas`) | % (0-100) | Generation fuel mix share; only collected if `include_generation: true` |

## Open-Meteo (`openmeteo`)

No unit-override parameters (`temperature_unit`, `wind_speed_unit`, `precipitation_unit`, etc.) are sent to
the API, so every field uses Open-Meteo's own default unit. For the fields in the example settings:

| Field | Unit |
|---|---|
| `temperature_2m` | °C |
| `relative_humidity_2m` | % |
| `precipitation` | mm |
| `cloud_cover` | % |
| `wind_speed_10m` | km/h |
| `direct_radiation` | W/m² |

If you configure other `fields` from the [Open-Meteo variable list](https://open-meteo.com/en/docs), check that
page for the default unit of each one.

## Octopus Energy (`octopus`)

| Field | Unit | Notes |
|---|---|---|
| `consumption_kwh` | kWh | Latest half-hourly electricity reading |
| `gas_consumption` | kWh or m³ | Unit depends on meter type (kWh for SMETS1 Secure, m³ for SMETS2); sent unconverted |
| `unit_rate_p_per_kwh` | pence/kWh (inc. VAT) | Only collected if `product_code`/`tariff_code` are configured |

## Nuki Smart Lock (`nuki`)

Every **Nuki** point carries a `device` tag holding the lock's own name from the Nuki app (give
each lock a distinct name), and every lock provisioned to the broker is reported as its own
point. Field keys are the bare names below, the same for every lock, so filter or group by
`device` to separate them. A lock that has not published its name to the broker is reported
under its Nuki device ID instead.

The broker is deliberately not recorded: every lock arrives through one broker, so it
identifies nothing, and moving to a different broker should not start a new series.

> **Breaking change in 5.3:** before 5.3 each lock's fields were prefixed with its name into a
> single shared point - `Front_Door_stateValue` rather than `stateValue` tagged
> `device=Front_Door` - and the point carried a `host` tag naming the broker. The lock could
> not be queried as a dimension that way. Existing history stays in the old shape and keeps
> working; `UPGRADING.md` describes the supplied migration that converts it, in two phases with
> the irreversible one separately invoked.

| Field | Unit | Notes |
|---|---|---|
| `stateValue` | - | Lock state, numeric (see table below); a code not in the table is still written through as its raw number - Nuki firmware may add new ones |
| `doorsensorStateValue` | - | Door sensor state, numeric (see table below); same raw-passthrough rule for undocumented codes |
| `batteryChargeState` | % | Battery charge level |
| `batteryCritical`, `batteryCharging`, `keypadBatteryCritical`, `doorsensorBatteryCritical` | bool | Battery status flags (keypad/door-sensor flags only present when those accessories are paired) |
| `mode`, `deviceType`, `firmware` | - | Device metadata |
| `connected` | bool | Broker-maintained liveness flag (MQTT Last Will) - `false` when the lock has dropped off the network, making stale state detectable |
| `serverConnected` | bool | Whether the lock currently has a connection to Nuki's cloud |
| `ringactionTimestamp` | - | ISO8601 time of the last ring action (string; Nuki Opener only - not published by locks) |
| `timestamp` | - | ISO8601 time of the lock's last state update (string) |

`stateValue` codes (Nuki MQTT API spec v1.6):

| Value | Meaning |
|---|---|
| 0 | uncalibrated |
| 1 | locked |
| 2 | unlocking |
| 3 | unlocked |
| 4 | locking |
| 5 | unlatched |
| 6 | unlocked (lock 'n' go) |
| 7 | unlatching |
| 254 | motor blocked |
| 255 | undefined |

`doorsensorStateValue` codes:

| Value | Meaning |
|---|---|
| 1 | deactivated |
| 2 | door closed |
| 3 | door opened |
| 4 | door state unknown |
| 5 | calibrating |
| 16 | uncalibrated |
| 240 | tampered |
| 255 | unknown |

## Speedtest (`speedtest`)

| Field | Unit | Notes |
|---|---|---|
| `download`, `upload` | bits per second | From `speedtest-cli` |
| `ping` | ms | Round-trip time to the test server |

Other fields available from `speedtest-cli`'s results (e.g. `bytes_sent`, `bytes_received`, `server.*`) can also
be selected via `fields` in settings; see the [speedtest-cli](https://github.com/sivel/speedtest-cli) project for
their meaning and units.

Every **Speedtest** point carries a `host` tag holding the short hostname of the machine that ran
the test. That is the point of it: running a collector on several hosts into one database is how
their connections are compared, so the tag is what separates them. Filter or group by `host`, and
note that renaming a machine starts a new series.

The `collector_status` heartbeat for `speedtest` carries the same `host` tag, so each collector's
`ok`/`consecutive_failures` are recorded separately. Before this existed every host wrote the same
`collector_status,source=speedtest` series and overwrote the others at second precision, so one
collector dying looked exactly like a healthy estate - if you have heartbeat history from before
that change, it sits in an untagged series and cannot be attributed to a host.
