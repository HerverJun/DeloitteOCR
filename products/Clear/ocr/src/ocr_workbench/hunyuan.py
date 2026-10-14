"""Resident CUDA llama.cpp server; authenticated loopback, fixed F16 settings."""

import base64
import json
import secrets
import socket
import subprocess
import time
import urllib.request
import urllib.error


class Session:
    def __init__(self, bundle, output):
        binary = bundle / "runtimes/llama/llama-server.exe"
        model = bundle / "models/HunyuanOCR-GGUF/hyocr-f16.gguf"
        projector = model.with_name("mmproj-hyocr-f16.gguf")
        for path in [binary, model, projector]:
            if not path.is_file():
                raise FileNotFoundError(f"Missing offline asset: {path}")
        self.prompts = json.loads(
            (bundle / "config/hunyuan-prompts.json").read_text(encoding="utf-8")
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.token = secrets.token_hex(24)
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.proc = None
        self.log = (output / "llama-server.log").open("w", encoding="utf-8")
        self.loaded = 0
        command = [
            str(binary),
            "--model",
            str(model),
            "--mmproj",
            str(projector),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--alias",
            "HYVL",
            "--ctx-size",
            "10240",
            "--n-predict",
            "4096",
            "--n-gpu-layers",
            "99",
            "--parallel",
            "1",
            "--api-key",
            self.token,
            "--offline",
            "--log-verbosity",
            "4",
            "--fit",
            "off",
            "--device",
            "CUDA0",
            "--mmproj-device",
            "CUDA0",
        ]
        started = time.perf_counter()
        try:
            self.proc = subprocess.Popen(
                command,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError("llama-server 启动失败，请查看引擎日志")
                try:
                    with self.opener.open(
                        urllib.request.Request(
                            self.base + "/health", headers=self.headers()
                        ),
                        timeout=2,
                    ) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.2)
            else:
                raise TimeoutError("llama-server 启动超时")
            self.loaded = time.perf_counter() - started
        except BaseException:
            self.close()
            raise

    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def recognize(self, image, output=None):
        request = {
            "model": "HYVL",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image.read_bytes()).decode()
                            },
                        },
                        {"type": "text", "text": self.prompts["doc_parse"]},
                    ],
                }
            ],
            "max_tokens": 4096,
            "temperature": 0.0,
            "top_p": 1.0,
            "top_k": -1,
            "repeat_penalty": 1.08,
            "seed": 0,
        }
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(request).encode(),
            headers=self.headers(),
        )
        with self.opener.open(req, timeout=600) as response:
            raw = json.load(response)
        if raw["choices"][0]["finish_reason"] != "stop":
            raise RuntimeError("混元输出未正常结束，拒绝保存截断结果")
        text = raw["choices"][0]["message"]["content"] or ""
        return (
            raw,
            [{"kind": "document", "text": text, "confidence": None, "polygon": None}],
            self.loaded,
        )

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.log.close()


def recognize(bundle, image, output):
    session = Session(bundle, output)
    try:
        return session.recognize(image, output)
    finally:
        session.close()
