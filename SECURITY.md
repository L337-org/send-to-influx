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

- **Credentials on disk**: `settings.yaml` holds API keys/passwords in plain text by default. Restrict
  its permissions (`chmod 600 settings.yaml`) if you keep secrets in it - source-checkout installs
  aren't locked down for you automatically, since there's no `postinst` there to do it. An
  environment-variable override for InfluxDB's credentials was implemented and then deliberately
  removed - splitting secrets into a second file with the same effective permissions added no real
  security boundary, and risked the file being created less securely than `settings.yaml` itself. See
  `CLAUDE.md`'s "Rejected: environment-variable secrets" for the full reasoning.
- **`systemd-creds` for the packaged install**: on a systemd host (`systemd >= 250`),
  `send-to-influx-set-credential <name>` moves a credential out of `settings.yaml` and into
  `systemd-creds` - TPM-bound or host-derived encryption at rest, decrypted only into a restricted,
  in-memory location for the service's lifetime, a real security boundary rather than an
  organizational one. See the README's "Storing secrets in systemd-creds" section and CLAUDE.md's
  "Credential storage (`systemd-creds`)" for details. This is opt-in and per-field. The interactive
  `.deb` install prompt (debconf) can also collect these directly, using debconf's `Type: password`
  widget - contrary to an earlier version of this note, debconf *does* write password-type answers to
  disk (a dedicated `passwords.dat` store, kept separate from its general-purpose, more widely-readable
  answer database, and restricted to `chmod 600`). Debian's own developers' guide
  (`debconf-devel(7)`) advises clearing a password value out of that store "as soon as is possible"
  once consumed, so `postinst` does exactly that: right after each `db_get` on a password-type
  template, it sets the stored answer back to the empty string (`db_set <question> ""`), removing
  the leftover copy in `passwords.dat` (a final sweep also clears every password-type answer whether
  or not its source was selected, so an inconsistent preseed can't leave one behind; the InfluxDB
  user/organisation answer is cleared too, since for a v1 install it's the InfluxDB username). This doesn't change the already-separate UI convention that
  password answers are never redisplayed on a later `dpkg-reconfigure` (secret prompts always come
  back blank regardless); it removes the actual stored value, not just the redisplay behaviour.
  (An earlier version used `db_unregister` instead - that cleared the value equally well, but
  deleting the question deletes its `seen` flag too, and the recreated, unseen question was then
  re-asked, blank and contextless, on every subsequent package upgrade.)
- **`enforce_permissions` (settings.yaml key)**: if `settings.yaml` is readable by group/other *and*
  actually contains a real credential (not just placeholder or systemd-creds-sentinel text),
  `send-to-influx` always logs a warning. Setting `enforce_permissions: true` additionally refuses to
  start until the permissions are fixed. This key defaults to `false` when absent, so any
  `settings.yaml` from before this feature existed keeps working with just the warning; new installs
  (packaged or a fresh copy of `example_settings.yaml`) ship it as `true`. The packaged install's
  fresh-install default file mode is `644`, not `600` - safe specifically because a freshly-packaged
  `settings.yaml` never contains a real secret (only placeholder text, or once systemd-creds is used, a
  cosmetic sentinel) unless you hand-edit one in, which is exactly the case this check catches
  regardless of file mode. An upgrade never resets permissions/ownership you've since changed
  yourself - they're only set when `postinst` creates the file in the first place (a genuinely
  fresh install, or a `settings.yaml` that's been deleted since).
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
