# Public synthetic fixture set

All seven files in `tests/fixtures/data/` are newly generated synthetic material. The generator reads no business samples or network resources. The material is offered under CC0-1.0; the manifest records the license, file SHA256, exact expected values, source identity and revision. `real_public_sources` is deliberately empty. These files test protocol and parser behavior, not OCR/classification accuracy on real documents.

Use CPython 3.12 on Windows x86_64. Download the eight exact versions listed in `requirements-windows-py312.lock.txt` into a **run-owned wheelhouse** with `python -m pip download --only-binary=:all: --dest <wheelhouse> -r requirements-windows-py312.lock.txt`. The lock includes hashes; the isolated installation rejects a different wheel. A local index or previously verified cache can provide the wheels. Do not install them into the platform runtime or an existing application environment.

```powershell
python scripts/fixtures/setup_runtime.py --run-root <run-dir> --wheelhouse <wheelhouse>
$env:WORKBENCH_FIXTURE_PYTHON = '<run-dir>/workspaces/fixture-venv/Scripts/python.exe'
& $env:WORKBENCH_FIXTURE_PYTHON scripts/fixtures/build_fixtures.py --output-dir <scratch>/data --manifest <scratch>/manifest.json
python -m pytest tests/fixtures/test_manifest.py -q
```

The platform Python runs the frozen `fixtures` check, while `WORKBENCH_FIXTURE_PYTHON` explicitly selects the independent, hash-locked parser/generator interpreter. The test rebuilds in a temporary directory and uses `readback.py` in a separate process to parse every PNG, PDF, XLSX, CSV and Parquet file. It compares exact truth and bytes; the XLSX check also rebuilds across different clock seconds. The checked-in data directory is never a test output destination. `setup_runtime.py` writes only beneath the supplied run directory, and the builder writes only to its explicit output and manifest paths.

The CSV uses literal `\N` as a null marker while an empty field remains an empty string; CSV itself has no native null type. Parquet has a native null and decimal128(20,6). XLSX retains long identifiers as text and a formula whose cached result is absent. `same-source-a.png` and `same-source-b.png` have equal bytes but different source IDs. `票据-甲-v2.png` shares source ID with `same-source-a.png` but has a later revision and changed bytes. Row IDs and duplicate keys are intentional.
