# Security Policy

## Reporting a vulnerability

If you believe you have found a security issue in `send-to-influx`, please open a
private vulnerability report via GitHub's
[security advisory flow](https://github.com/GavinLucas/send-to-influx/security/advisories/new)
rather than filing a public issue. That keeps the discussion private until a
fix is available.

## Operational security notes

`send-to-influx` is a script that reads credentials for your devices/APIs and your InfluxDB
instance from `settings.yaml`, and polls those APIs to write data to InfluxDB. There's no
network listener and no handling of untrusted input from the network - the main things worth
being deliberate about are credential storage and TLS verification:

- **Credentials on disk**: `settings.yaml` holds API keys/passwords in plain text. Restrict its
  permissions (`chmod 600 settings.yaml`), as the README recommends (the packaged install does this
  automatically). An environment-variable override for InfluxDB's credentials was implemented and
  then deliberately removed - splitting secrets into a second file with the same effective
  permissions added no real security boundary, and risked the file being created less securely than
  `settings.yaml` itself. See `CLAUDE.md`'s "Rejected: environment-variable secrets" for the full
  reasoning.
- **TLS verification defaults differ by source, deliberately**: the `influx` block defaults to
  verifying TLS certificates (`insecure: true` is an explicit opt-out); the `hue` block defaults
  the other way (`insecure: true`, i.e. verification skipped), since Hue bridges are commonly
  reached over a self-signed local certificate. Set `hue.insecure: false` if your bridge has a
  valid certificate. Don't set `insecure: true` for InfluxDB unless you understand the exposure
  (typically only reasonable for a same-host or same-LAN instance).
- **Packaged (`.deb`/systemd) deployments** run as a dedicated unprivileged system user with
  `NoNewPrivileges=true` and `ProtectSystem=strict` (mounts the filesystem read-only except for
  paths explicitly listed). `/etc/send-to-influx` is the only path granted read-write access via
  `ReadWritePaths=` - see `packaging/send-to-influx.service`.

If you find a gap in any of the above (e.g. a credential that ends up somewhere it shouldn't,
such as logs), please report it via the private flow above rather than a public issue.
