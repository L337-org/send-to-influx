# Privacy Policy

**send-to-influx** does not collect, store, or transmit any data back to the author, and has no
telemetry or analytics of any kind.

It runs entirely on your own machine (or wherever you choose to run it) as a local process. Its
network activity is limited strictly to the endpoints **you** configure it to talk to:

- **Your InfluxDB instance** — the metrics it collects are written only there, using the
  connection details and credentials you supply in `settings.yaml`.
- **The device/API sources you configure** — Hue Bridge (on your local network), MyEnergi,
  Octopus Energy, Open-Meteo, and the National Grid ESO Carbon Intensity API. Each is only
  contacted if you enable that source, using the credentials/host you supply.
- **Speedtest** — if enabled, this source contacts Ookla's public speedtest network to find a
  nearby test server and measure your connection, the same as running the standalone
  `speedtest-cli` tool. This is the one source that talks to a third-party network chosen
  automatically rather than one you name explicitly.

No credentials, device readings, or InfluxDB data pass through any author-operated service —
there is no author-operated service. Everything above is a direct connection between your
machine and the endpoint you configured.

Because the data collected (e.g. home energy usage, presence/occupancy from motion sensors) can
be sensitive, keep `settings.yaml` and your InfluxDB instance appropriately secured — see
[SECURITY.md](SECURITY.md).

## Contact

Questions about this policy: open an issue at
<https://github.com/GavinLucas/send-to-influx/issues>.
