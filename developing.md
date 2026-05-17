# Developing

## Tests

- `./smoke-tests` — fast unit tests under `tests/`. Run on every change.
- `./run-tests` — full suite. Run **before commit / release** and **every
  ~5 iterations**.
- If you change or add functionality, run the specific test for it (e.g.
  `$(pipx-python clauthing --venv)/bin/python functional_tests/test_cd_command.py`).

All test scripts use the clauthing pipx venv via `pipx-python clauthing --venv`.
