# Windows portable bundle

Files here are copied to the **root** of the portable bundle
(`htpack-<version>-windows-x64-beta/`). The `.cmd` launchers call
`%~dp0runtime\python.exe`, so they only work inside an assembled bundle.

| File | Role in the bundle |
|---|---|
| `htpack.cmd` | CLI launcher (`python -m htpack`) |
| `Install.cmd`, `install_user.py` | Optional per-user copy under `%LOCALAPPDATA%\Programs\HTPack` (checks `bundle-manifest.json`) |
| `Run-Tests.cmd` | Runs pytest; expects tests under `validation\tests\` in the bundle |
| `Validate-Windows.cmd` | Runs `validation\validate_windows.py` on the validation-data set |
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
