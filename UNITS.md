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
| `vol` | V ×10 | Supply voltage, as returned by the API (divide by 10 for volts) |
| `gen` | W | Generated (solar) power |
| `grd` | W | Power from/to the grid |
| `che` | kWh | Energy transferred so far this session |
| `sta` | numeric status code | Not a physical unit |
| `wifiLink`, `ethernetLink` | N/A | Diagnostic/status fields, not documented as physical units |
| `newAppAvailable`, `newBootloaderAvailable` | boolean (0/1) | Update-available flags |
| `Charge`, `Import`, `Export`, `Genera` | kWh | Daily totals from the day/hour endpoint, always collected regardless of `fields` |

## MyEnergi Eddi (`eddi`)

| Field | Unit | Notes |
|---|---|---|
| `frq` | Hz | Supply frequency |
| `vol` | V ×10 | Supply voltage, as returned by the API (divide by 10 for volts) |
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

## Speedtest (`speedtest`)

| Field | Unit | Notes |
|---|---|---|
| `download`, `upload` | bits per second | From `speedtest-cli` |
| `ping` | ms | Round-trip time to the test server |

Other fields available from `speedtest-cli`'s results (e.g. `bytes_sent`, `bytes_received`, `server.*`) can also
be selected via `fields` in settings; see the [speedtest-cli](https://github.com/sivel/speedtest-cli) project for
their meaning and units.
