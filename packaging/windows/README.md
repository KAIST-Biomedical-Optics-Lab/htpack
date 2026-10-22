# Windows portable bundle

The `.cmd` launchers run the repository's uv environment
(`<repo>\.venv\Scripts\python.exe`, resolved as two levels above this folder).
Run `uv sync` in the repository root first; otherwise they exit with a message.
Tests are taken from `<repo>\tests` and validation scripts from `<repo>\validation`.

Note: the 0.5.0b1 bundle shipped versions of these launchers that called
`%~dp0runtime\python.exe` from the bundle root. A future bundle build script must
generate bundle-specific launchers. `Install.cmd` still copies a *bundle* and
checks `bundle-manifest.json`, so it is only meaningful inside an assembled bundle.

| File | Role |
|---|---|
| `htpack.cmd` | CLI launcher (`python -m htpack`) |
| `Install.cmd`, `install_user.py` | Optional per-user copy of an assembled bundle under `%LOCALAPPDATA%\Programs\HTPack` (checks `bundle-manifest.json`) |
| `Run-Tests.cmd` | Runs pytest on `<repo>\tests`; JUnit XML to `<repo>\validation\pytest-windows.xml` |
| `Validate-Windows.cmd` | Runs `<repo>\validation\validate_windows.py` on the validation-data set |
| `README-WINDOWS.txt` | End-user instructions shipped in the 0.5.0b1 bundle (Korean) |
| `build-provenance.json` | Runtime and wheel URLs/SHA-256 used for 0.5.0b1 |

Bundle layout at 0.5.0b1:

```
runtime/                 python-build-standalone CPython 3.12 + wheels (see build-provenance.json)
support/install_user.py
validation/{audit_tcf,validate_windows,verify_return}.py, validation/tests/
*.cmd, FORMAT.txt, README-WINDOWS.txt, build-provenance.json, bundle-manifest.json
```

TODO: the 0.5.0b1 bundle was cross-assembled on macOS; the assembly script is
not yet in this repository. Add it here (e.g. `build_bundle.py`) so the bundle
can be rebuilt from `build-provenance.json` and `uv build`.
