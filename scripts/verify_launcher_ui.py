"""Render and exercise the real Qt launcher with a controlled service boundary."""

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
from unittest.mock import MagicMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase
from ocr_workbench.launcher import Launcher
from ocr_workbench.startup import FIRST_START_NOTICE, UPGRADE_NOTICE
from ocr_workbench.atomic_files import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    # The offscreen Qt platform has no Windows font discovery.
    QFontDatabase.addApplicationFont(str(Path(os.environ["SystemRoot"]) / "Fonts/msyh.ttc"))
    process = MagicMock(pid=1234)
    process.poll.return_value = None
    with patch("ocr_workbench.launcher.subprocess.Popen", return_value=process) as launched, patch(
        "ocr_workbench.launcher.ProcessJob"
    ), patch("ocr_workbench.launcher.QTimer"), patch("ocr_workbench.launcher.QSystemTrayIcon"):
        window = Launcher(args.bundle, args.output / "data", no_browser=True)
        assert launched.call_args.args[0][launched.call_args.args[0].index("--startup-check") + 1] == "auto"
        window.opener = MagicMock()
        window.opener.open.side_effect = urllib.error.URLError("service still starting")
        try:
            window.show()
            for name, notice in (("first-start", FIRST_START_NOTICE), ("upgrade", UPGRADE_NOTICE)):
                for message in ("准备完整校验…", "正在校验离线文件：12,500 / 65,209", "检查运行时依赖：glm…"):
                    write_json(window.startup_file, {"status": "checking", "notice": notice, "message": message})
                    window.poll()
                    app.processEvents()
                    assert window.notice.text() == notice and window.notice.isVisible()
                    assert window.status.text() == message
                    assert not window.open_button.isEnabled()
                    if "12,500" in message:
                        assert window.grab().save(str(args.output / (name + ".png")))
            write_json(window.startup_file, {"status": "passed", "notice": "", "message": "正在快速启动工作台…"})
            window.poll()
            assert window.notice.isHidden() and window.status.text() == "正在快速启动工作台…"
            window.no_browser = False
            window.open_browser = MagicMock()
            window.opener.open.side_effect = None
            window.opener.open.return_value.__enter__.return_value.status = 200
            window.poll()
            assert window.ready and window.open_button.isEnabled()
            assert json.loads((window.session / "launcher-state.json").read_text("utf-8"))["ready"]
            window.open_browser.assert_called_once()
            assert window.isHidden()
            window.ready = False
            process.poll.return_value = 2
            write_json(window.startup_file, {"status": "failed", "notice": FIRST_START_NOTICE, "message": "文件内容损坏：model.bin"})
            window.poll()
            assert "文件内容损坏" in window.status.text() and window.isVisible()
            (args.output / "ui-verification.json").write_text(json.dumps({
                "passed": True,
                "checks": ["default-auto-policy", "persistent-first-start-notice", "persistent-upgrade-notice",
                           "independent-progress", "cached-start-message", "automatic-open-when-ready", "failure-detail"],
                "scope": "Real Qt widget rendered offscreen; service boundary controlled. Packaged service tested separately.",
            }, ensure_ascii=False, indent=2), "utf-8")
        finally:
            window.cleanup()
            window.hide()
    print("Launcher UI checks passed")


if __name__ == "__main__":
    main()
