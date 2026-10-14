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

平台健康接口 `GET /api/platform/v1/health` 的 `service=ready` 表示本服务处理了请求，`ui/cpu/gpu/models=unknown`，不会以文件存在推断旧模块或 GPU 状态。应用目录及本机工作空间接口另有各自路由。无凭据、业务样本和真实外部 API 配置被写入工程。

工作空间接口使用受信进程环境 `WORKBENCH_DATA_DIR`（指向预先建立的本机数据目录），启动时在该目录打开或创建 `workspaces.sqlite3`。例如在启动 uvicorn 前设置 `$env:WORKBENCH_DATA_DIR = 'E:/platform-data'`；此值不能由 HTTP 请求指定。未配置时 GET/POST 返回 503，而不是伪造空列表。数据目录无效会使服务启动失败，以免误用其它位置。工作空间 GET 仅接受 loopback Host；创建 POST 还要求严格 JSON 与匹配该 Host 的 HTTP Origin。通过 Vite 本机代理开发时，浏览器同源 Origin 与转发 Host 必须保持一致。

门户现在用 React Router、TanStack Query 和 Ant Design 6 单一主题；`pnpm run test` 是一次性完成的 Vitest/jsdom 行为测试。七个主导航入口可直接访问；资料、任务、成果、设置在服务能力接入前显示真实未开放状态。工作空间以 URL 查询参数 `workspace` 传递，应用状态查询按工作空间和应用身份隔离。工具的实际打开与旧应用浏览器联动属于后续接入任务。
