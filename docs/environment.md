# 独立平台工程环境

此仓库只包含新平台工程。OCR 与分类应用保持各自的仓库、Python 环境和数据目录，平台通过未来的 HTTP 适配器接入；本阶段不复用其包或数据库。

本次验证环境为 Windows x64、Python 3.12.7 (`E:/project/platform-dev-runs/dev-20260925-001/workspaces/platform-venv/Scripts/python.exe`)、Node 22.17.0 (`D:/nodejs/node.exe`)、pnpm 10.12.3 (`C:/Users/A/AppData/Roaming/npm/pnpm.cmd`)。新虚拟环境、依赖缓存、测试数据均在运行目录，不入此仓库。`docs/requirements-windows-py312.lock.txt` 由 `pyproject.toml` 的 dev extra 解析，锁定直接与传递 Python 包及发行文件 SHA256；`apps/portal/pnpm-lock.yaml` 锁定门户传递依赖和完整性值。

在新的 Windows/Python 3.12 环境中，创建独立 venv 后执行：

```powershell
$env:PIP_NO_CACHE_DIR = '1'
python -m pip install --require-hashes -r docs/requirements-windows-py312.lock.txt
python -m pip install --no-build-isolation --no-deps -e .
python -m pytest tests/unit -q
python -m uvicorn workbench_platform.app:app --app-dir src --host 127.0.0.1 --port 8766
```

`setuptools==84.0.0` 与 `wheel==0.48.0` 也包含在同一哈希锁中。安装本地包使用已锁定的构建工具和 `--no-build-isolation`，不再建立临时联网构建环境。首次解析锁使用单独的工具环境；运行环境不安装锁生成工具。

门户在 `apps/portal` 内运行，缓存可指向该运行目录的专用路径：

```powershell
$env:npm_config_cache = 'E:/project/platform-dev-runs/dev-20260925-001/cache/npm'
pnpm install --frozen-lockfile --store-dir E:/project/platform-dev-runs/dev-20260925-001/cache/pnpm-store
pnpm run build
```

开发预览使用 `pnpm run dev`，只绑定本机 `127.0.0.1`，将 `/api` 转发到已由本次运行明确启动的 8766 服务。不要连接或停止未知同端口进程。生产构建仅证明静态文件能生成；真实浏览器导航和旧应用接入属于后续任务。

平台当前仅提供 `GET /api/platform/v1/health`。其中 `service=ready` 表示本服务处理了请求，`ui/cpu/gpu/models=unknown`，不会以文件存在推断旧模块或 GPU 状态。无凭据、业务样本和真实外部 API 配置被写入工程。
