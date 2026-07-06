## Summary

<!-- What does this change do, and why? -->

## Test plan

<!-- How did you verify this? e.g. `.venv/bin/pytest -v`, `.venv/bin/flake8`, `.venv/bin/mypy toinflux sendtoinflux.py`, manual steps. -->

## Checklist

- [ ] `.venv/bin/pytest -v`, `.venv/bin/flake8`, and `.venv/bin/mypy toinflux sendtoinflux.py` all pass locally
- [ ] If this adds a new data source: the ["Checklist when adding a new data source"](https://github.com/GavinLucas/send-to-influx/blob/main/CONTRIBUTING.md#checklist-when-adding-a-new-data-source) in `CONTRIBUTING.md` has been followed (factory registration, tests, `example_settings.yaml`, README/UNITS.md)
- [ ] If this changes CLI flags, settings keys, or retry/exit-code behaviour: `README.md`, `CLAUDE.md`, and `.github/copilot-instructions.md` are updated
