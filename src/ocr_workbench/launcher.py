"""PySide6 tray launcher; all application/GPU processes belong to a Windows job."""

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from PySide6.QtCore import QTimer, Qt, QLockFile
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QAction
from PySide6.QtWidgets import (
    QApplication,
    QSystemTrayIcon,
    QMenu,
    QWidget,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QMessageBox,
)
from ocr_workbench.processes import ProcessJob
from ocr_workbench.atomic_files import read_json


def icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#000000"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 12, 12)
    painter.setPen(QPen(QColor("white"), 3))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(18, 12, 27, 40)
    painter.drawLine(25, 26, 38, 26)
    painter.drawLine(25, 34, 38, 34)
    painter.drawLine(25, 42, 34, 42)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#86BC25"))
    painter.drawEllipse(40, 41, 17, 17)
    painter.end()
    return QIcon(pixmap)


class Launcher(QWidget):
    def __init__(self, bundle, data, no_browser=False):
        super().__init__()
        self.bundle, self.data = bundle, data
        self.no_browser = no_browser
        self.ready = False
        self.quitting = False
        self.process = None
        self.job = None
        self.log = None
        self.token = secrets.token_urlsafe(36)
        self.session = data / "launcher"
        self.session.mkdir(parents=True, exist_ok=True)
        self.startup_file = self.session / "startup-state.json"
        self.startup_file.unlink(missing_ok=True)
        self.token_file = self.session / "session-token.txt"
        self.token_file.write_text(self.token, encoding="utf-8")
        self.setWindowTitle("Deloitte ｜ OCR 工作台")
        self.setWindowIcon(icon())
        self.resize(440, 290)
        self.setStyleSheet(
            'QWidget {background:#151515;color:#f2f4ee;font-family:"Microsoft YaHei";font-size:13px;} QLabel {padding:8px;} QPushButton {padding:9px;background:#86bc25;color:#152008;border-radius:4px;font-weight:600;} QPushButton:hover {background:#95c840;} QPushButton:disabled {background:#353b2d;color:#b6bbb1;} QPushButton:focus {border:2px solid white;}'
        )
        layout = QVBoxLayout(self)
        brand = QLabel()
        logo = QPixmap(str(bundle / "web/brand/deloitte.svg"))
        if not logo.isNull():
            brand.setPixmap(logo.scaledToWidth(152, Qt.TransformationMode.SmoothTransformation))
        else:
            brand.setText("Deloitte")
        layout.addWidget(brand)
        title = QLabel("OCR 工作台 · 本机运行")
        title.setStyleSheet("font-size:16px;font-weight:600;")
        layout.addWidget(title)
        self.status = QLabel("正在启动本机服务…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.open_button = QPushButton("打开工作台")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_browser)
        layout.addWidget(self.open_button)
        self.exit_button = QPushButton("退出工作台")
        self.exit_button.clicked.connect(self.quit)
        layout.addWidget(self.exit_button)
        self.tray = QSystemTrayIcon(icon(), self)
        self.tray.setToolTip("Deloitte ｜ OCR 工作台")
        menu = QMenu()
        menu.addAction("打开工作台", self.open_browser)
        menu.addAction("查看运行状态", self.show)
        menu.addAction("打开项目目录", lambda: os.startfile(str(data)))
        menu.addAction(
            "查看服务日志", lambda: os.startfile(str(self.session / "service.log"))
        )
        menu.addSeparator()
        menu.addAction("退出工作台", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self.open_browser()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick
                else None
            )
        )
        self.tray.show()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.log = (self.session / "service.log").open("w", encoding="utf-8")
        self.job = ProcessJob()
        runtime = bundle / "runtimes/service/python.exe"
        if not runtime.is_file() or not (bundle / "web/index.html").is_file():
            raise RuntimeError("应用包不完整，请重新解压完整交付 ZIP")
        environment = os.environ.copy()
        environment["PATH"] = (
            str(runtime.parent)
            + os.pathsep
            + str(Path(os.environ["SystemRoot"]) / "System32")
        )
        for key in [
            "PYTHONHOME",
            "PYTHONPATH",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        ]:
            environment.pop(key, None)
        self.process = subprocess.Popen(
            [
                str(runtime),
                "-X",
                "utf8",
                "-I",
                "-m",
                "ocr_workbench.service",
                "--verify-startup",
                "--bundle",
                str(bundle),
                "--data",
                str(data),
                "--port",
                str(self.port),
                "--token-file",
                str(self.token_file),
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.job.assign(self.process)
        self.started = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(300)
        (self.session / "launcher-state.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "service_pid": self.process.pid,
                    "port": self.port,
                    "bundle": str(bundle),
                    "data": str(data),
                }
            ),
            encoding="utf-8",
        )

    def poll(self):
        if self.process.poll() is not None:
            self.cleanup()
            if self.quitting or self.ready:
                QApplication.quit()
                return
            detail = "服务启动失败。请查看项目目录中的 launcher/service.log。"
            if self.startup_file.exists():
                try:
                    detail = read_json(self.startup_file).get("message", detail)
                except (OSError, ValueError):
                    pass
            self.status.setText(detail)
            self.exit_button.setEnabled(True)
            self.show()
            return
        if self.quitting:
            if time.monotonic() - self.quit_started > 40:
                self.job.close()
            return
        if not self.ready:
            checking = False
            if self.startup_file.exists():
                try:
                    state = read_json(self.startup_file)
                    checking = state.get("status") == "checking"
                    self.status.setText(state.get("message", "正在启动检查…"))
                except (OSError, ValueError):
                    pass
            try:
                with self.opener.open(
                    self.base + "/api/health", timeout=0.2
                ) as response:
                    self.ready = response.status == 200
            except Exception:
                pass
            if self.ready:
                self.status.setText("服务正在运行。关闭浏览器后可从托盘重新打开。")
                self.open_button.setEnabled(True)
                if not self.no_browser:
                    self.open_browser()
                    self.hide()
            elif not checking and time.monotonic() - self.started > 45:
                self.status.setText("启动时间较长，请查看服务日志。")
                self.show()

    def open_browser(self):
        if not self.ready:
            self.show()
            return
        url = self.base + "/#token=" + self.token
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Google/Chrome/Application/chrome.exe",
        ]
        for browser in candidates:
            if browser.is_file():
                subprocess.Popen(
                    [str(browser), url], creationflags=subprocess.CREATE_NO_WINDOW
                )
                return
        webbrowser.open(url)

    def quit(self):
        if self.process is None or self.process.poll() is not None:
            QApplication.quit()
            return
        if self.quitting:
            return
        self.quitting = True
        self.quit_started = time.monotonic()
        self.status.setText("正在保存队列状态并退出引擎…")
        self.open_button.setEnabled(False)
        self.exit_button.setEnabled(False)
        self.show()
        try:
            req = urllib.request.Request(
                self.base + "/api/shutdown",
                data=b"{}",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                },
            )
            with self.opener.open(req, timeout=2):
                pass
        except Exception:
            if self.job:
                self.job.close()

    def cleanup(self):
        self.timer.stop()
        if self.job:
            self.job.close()
            self.job = None
        if self.log:
            self.log.close()
            self.log = None
        self.token_file.unlink(missing_ok=True)
        self.tray.hide()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path)
    p.add_argument(
        "--data",
        type=Path,
        default=Path(os.environ["LOCALAPPDATA"]) / "OfflineOCR/Workspace",
    )
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    bundle = args.bundle or (
        Path(sys.executable).resolve().parent.parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2]
    )
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Deloitte ｜ OCR 工作台")
    args.data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(args.data / "launcher.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "工作台已运行", "请从系统托盘打开现有工作台。")
        return 0
    window = None
    try:
        window = Launcher(bundle, args.data, args.no_browser)
        window.show()
        return app.exec()
    except Exception as error:
        if window and window.job:
            window.job.close()
        QMessageBox.critical(None, "工作台启动失败", str(error))
        return 1
    finally:
        if window:
            window.cleanup()
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
