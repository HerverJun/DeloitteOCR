import argparse
from pathlib import Path
import shutil

p = argparse.ArgumentParser()
p.add_argument("--bundle", type=Path, required=True)
a = p.parse_args()
source = Path(__file__).resolve().parents[1]
for origin, dest in [
    ("src", "app"),
    ("config", "config"),
    ("fixtures", "fixtures"),
    ("docs", "docs"),
    ("audit", "audit"),
]:
    shutil.copytree(
        source / origin,
        a.bundle / dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
shutil.copy2(source / "README.md", a.bundle / "README.md")
(a.bundle / "ocr.cmd").write_text(
    '@echo off\r\nchcp 65001 >nul\r\n"%~dp0runtimes\\control\\python.exe" -X utf8 -I -m ocr_workbench.cli %*\r\nexit /b %errorlevel%\r\n',
    encoding="ascii",
)
(a.bundle / "tools").mkdir(exist_ok=True)
shutil.copy2(source / "scripts/run_acceptance.py", a.bundle / "tools/run_acceptance.py")
shutil.copy2(
    source / "scripts/audit_application.py", a.bundle / "tools/audit_application.py"
)
(a.bundle / "运行兼容性验收.cmd").write_text(
    '@echo off\r\nchcp 65001 >nul\r\n"%~dp0runtimes\\control\\python.exe" -X utf8 -I -m ocr_workbench.acceptance_entry\r\nset "OCR_EXIT=%errorlevel%"\r\npause\r\nexit /b %OCR_EXIT%\r\n',
    encoding="utf-8",
)

for name in [
    "audit_load_cycles.py",
    "audit_batch_stress.py",
    "audit_failure_recovery.py",
    "audit_engine_update.py",
    "target_machine_acceptance.py",
    "evaluate_benchmark.py",
    "benchmark_metrics.py",
    "audit_fidelity.py",
    "official_reference.py",
    "audit_table_scenarios.py",
    "audit_dependencies.py",
]:
    shutil.copy2(source / "scripts" / name, a.bundle / "tools" / name)
