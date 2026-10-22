# HTPack

Error-bounded compression of holotomography (HT) refractive-index (RI) volumes
stored in Tomocube TCF (HDF5) files.

> **Status: 0.5.0b1 internal beta (BMOL, KAIST).** Native execution has been
> tested on macOS; Windows x64 validation is in progress. The public license is
> not yet decided — do not redistribute.

## What it does

- **Default mode** — 3D real FFT of each block (≤ 32×256×256), Fourier-coefficient
  quantization, a spatial integer residual and a final exception guard.
- **`--fast` mode** — float32 BitRound + Pcodec with the same final guard.
- Every decoded voxel satisfies `|RI_decoded − RI_original| ≤ ε`
  (default ε = 1e-4; `--ri-error-scale F` sets ε = F × 1e-4).
- Non-RI datasets (BF, FL, MIP, thumbnails, metadata) are copied unchanged, so a
  restored TCF keeps the original schema.
- Time-lapse TCFs are compressed per time point with RAM-aware concurrency.

Supported input: TCF FormatVersion 1.3.6 / 1.4.1 / 1.6.3 with uint16 RI codes
(`RI = code / 10000`). See [`docs/FORMAT.txt`](docs/FORMAT.txt) for the archive
format, error contract and limitations.

## Install and run (uv)

```bash
uv sync                       # creates .venv with the pinned dependencies (Python 3.12)
uv run htpack --help
uv run htpack encode sample.TCF -o sample.htfc
uv run htpack encode sample.TCF -o sample.fast.htfc --fast
uv run htpack decode sample.htfc -o restored.TCF
uv run htpack verify sample.htfc --reference sample.TCF --restored restored.TCF
```

From another directory: `uv run --project <path-to-this-repo> htpack ...`

## Tests

```bash
uv run pytest
```

Tests use small synthetic TCF fixtures only. Real-data acceptance scripts are in
[`validation/`](validation/) and need a separate validation-data set (not in git).

## Repository layout

| Path | Contents |
|---|---|
| `src/htpack/` | Package source (CLI: `htpack`) |
| `tests/` | pytest suite (synthetic fixtures) |
| `validation/` | Real-data acceptance harness (`validate_windows.py`, `audit_tcf.py`, `verify_return.py`) |
| `docs/FORMAT.txt` | Archive format and error contract |
| `packaging/windows/` | Launchers and installer for the portable Windows bundle |

The portable Windows bundle (embedded Python runtime + wheels) is distributed as
a release asset, not stored in this repository.

## License

To be decided. Bundled third-party libraries keep their own licenses.
