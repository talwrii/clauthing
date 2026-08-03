# Developing

## Tests

- `./smoke-tests` — fast unit tests under `tests/`. Run on every change.
- `./run-tests` — full suite. Run **before commit / release** and **every
  ~5 iterations**.
- If you change or add functionality, run the specific test for it (e.g.
  `$(pipx-python clauthing --venv)/bin/python functional_tests/test_cd_command.py`).

All test scripts use the clauthing pipx venv via `pipx-python clauthing --venv`.

## Worktrees (parallel work)

Each parallel task gets its own git worktree under `worktrees/` on its own
branch, plus its own isolated pipx install — so branches don't share a venv and
one worktree's editable install never shadows another's.

```sh
# start a workdir for <name>
git worktree add worktrees/<name> -b <name>
pipx install --editable worktrees/<name> --suffix @<name>
```

The `--suffix @<name>` installs a *second*, independent copy of the package:

- it exposes the entry points suffixed, e.g. `clauthing@<name>`, so it doesn't
  collide with the main `clauthing` install;
- it gets its own venv, reachable as `pipx-python clauthing@<name> --venv` —
  use that to run this worktree's tests, exactly like `pipx-python clauthing
  --venv` for the main tree.

When the branch is merged/abandoned, tear both down:

```sh
pipx uninstall clauthing@<name>
git worktree remove worktrees/<name>
```

