# Data Units Reference

This document lists the fields collected by each source and the units they are reported in.
It reflects what the API returns; `send-to-influx` does not convert or rescale any values
unless explicitly noted below.

## Hue Bridge (`hue`)

| Field | Unit | Notes |
|---|---|---|
| Temperature sensors | °C, °F or K | Set via `temperature_units` in settings (default `C`), rounded to 2 decimal places |
| Light level sensors | lux | Converted from the Hue `lightlevel` raw value |
| Motion/presence sensors | boolean (0/1) | 1 = movement detected |
| Smart plugs | boolean (0/1) | 1 = on |
| Dimmable lights | % (0-100) | Brightness percentage |

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

Field keys are prefixed with the lock's own name from the Nuki app (spaces replaced with
underscores), e.g. `Front_Door_stateName`; every lock provisioned to the broker is reported.

| Field | Unit | Notes |
|---|---|---|
| `stateName` | - | Lock state label (`locked`, `unlocked`, `unlatched`, ...); an unrecognised numeric code is written unchanged as `state` instead |
| `doorsensorStateName` | - | Door sensor label (`door closed`, `door opened`, ...); unrecognised codes written unchanged as `doorsensorState` |
| `batteryChargeState` | % | Battery charge level |
| `batteryCritical`, `batteryCharging`, `keypadBatteryCritical`, `doorsensorBatteryCritical` | bool | Battery status flags (keypad/door-sensor flags only present when those accessories are paired) |
| `mode`, `deviceType`, `firmware` | - | Device metadata |
| `connected` | bool | Broker-maintained liveness flag (MQTT Last Will) - `false` when the lock has dropped off the network, making stale state detectable |
| `serverConnected` | bool | Whether the lock currently has a connection to Nuki's cloud |
| `timestamp` | - | ISO8601 time of the lock's last state update (string) |

## Speedtest (`speedtest`)

| Field | Unit | Notes |
|---|---|---|
| `download`, `upload` | bits per second | From `speedtest-cli` |
| `ping` | ms | Round-trip time to the test server |

Other fields available from `speedtest-cli`'s results (e.g. `bytes_sent`, `bytes_received`, `server.*`) can also
be selected via `fields` in settings; see the [speedtest-cli](https://github.com/sivel/speedtest-cli) project for
their meaning and units.
