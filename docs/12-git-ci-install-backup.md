# Git、CI、安装与恢复

本仓库用 Git 记录源代码、锁文件、测试、文档和小型评测报告。运行数据、缓存、已训练模型、公开图片与教学视频留在工作目录或独立备份中，不进入 Git。`.gitignore` 隔离这些产物；克隆仓库不等于恢复已有项目。

## 本地 Git 与 CI

本地仓库的默认分支为 `main`。提交前运行：

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m pytest -q
Push-Location apps\web
pnpm.cmd install --frozen-lockfile
pnpm.cmd build
Pop-Location
git status --short
```

`.github/workflows/ci.yml` 在 GitHub 的 push / pull request 上使用全新 Ubuntu 运行器：安装 Python 3.12 的 API/数据/评估测试依赖，执行 lint 和单元测试；根据前端锁文件安装、构建、下载 Chromium，启动干净数据目录的本地 API，实际从浏览器创建项目、上传、标注并刷新验证。这个 CI 不下载 2.8 GB 的 CUDA PyTorch，也不承诺 GPU 训练验收。仅在推送到启用 GitHub Actions 的仓库后才会真正执行；本地 Git 初始化本身不会触发远端 CI。完整 GPU/Windows 验收仍在有 NVIDIA GPU 的机器上运行原平台流程。

## 干净 Windows 安装

前提：Windows x64、Python 3.12、Node.js 20.19+ 或 22.12+、pnpm 10.12.3；NVIDIA GPU 路径需要与 CUDA 12.8 轮子兼容的驱动。新机器上：

```powershell
git clone <你自己的仓库地址> image-classification-platform
cd image-classification-platform
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

安装脚本检查解释器、Node/pnpm 版本，创建 `.venv`，校验 PyTorch 和 torchvision 两个预编译 wheel 的 SHA256，再安装 `requirements-windows-cu128.lock.txt` 固定版本，执行 `pip check`，使用 `pnpm-lock.yaml --frozen-lockfile` 安装/构建前端，最后检查 GPU 与 ONNX Runtime。默认从官方 PyPI / PyTorch wheel 源下载，缓存放在 `%LOCALAPPDATA%\PrismImagePlatform\cache`，不依赖 E 盘。如果必须换源可显式传 `-IndexUrl`、`-TorchWheelBase`、`-CacheDir`。其余 Python 间接依赖锁版本，但还未做逐 wheel 哈希锁；首次下载预训练权重还需要网络。CI 使用轻量依赖集，不能取代第二台干净 Windows 主机的全量 GPU 安装演练。

## 完整工作区备份

**先停止 API 和 worker，避免复制期间修改模型或图片文件。**运行中的 SQLite WAL 由 SQLite backup API 读取，不会漏掉已提交事务；但数据库与模型/图片的跨文件一致性仍要求停止写入。备份会拒绝尚未结束的任务。示例：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
.\.venv\Scripts\python.exe -m image_platform.backup create `
  --data-dir .\.local-data `
  --output E:\image-platform-data\backups\prism-2026-09-23
.\.venv\Scripts\python.exe -m image_platform.backup verify `
  --backup E:\image-platform-data\backups\prism-2026-09-23
```

备份覆盖数据库、模型包、数据版本、导出、日志、上传图，以及所有已登记和历史数据版本引用的**外部原图**。每份文件记录字节数与 SHA256，并验证 SQLite 完整性；原图若已丢失或内容改变，备份会报错而不会留下“成功”的不完整副本。特征缓存、缩略图和下载权重缓存可以重建，因此不备份。备份是目录而非单独一个 SQLite 文件，需连同 `manifest.json`、`data/`、`external/` 一起保存。

## 在独立目录恢复与验收

```powershell
.\.venv\Scripts\python.exe -m image_platform.backup restore `
  --backup E:\image-platform-data\backups\prism-2026-09-23 `
  --target E:\image-platform-data\restored\prism-check
$env:ICP_DATA_DIR = 'E:\image-platform-data\restored\prism-check'
.\scripts\start.ps1 -Port 8877
```

恢复只允许**不存在的新目录**，不会覆盖当前 `.local-data`。它先完整校验 SHA256 和 SQLite，再复制到临时目录，重新定位数据库中的资产/模型路径及不可变数据清单中的图片路径，更新克隆后的清单校验和，验收成功才将目录提升为目标。旧目录和原图片始终保持不变。归档模型包内部的旧数据清单保留原来的训练来源路径，作为历史证据；克隆工作区的数据版本则指向恢复后的副本，因此清单哈希会不同，图片内容与划分不变。旧导入任务日志中的原始目录路径是历史记录，不代表可在新目录直接重试旧导入任务。

要从克隆工作区打开 API，应使用 `ICP_DATA_DIR` 并选未占用的端口；原数据和恢复副本不可在同一数据目录上并行写入。恢复后建议抽查项目数、原图缩略图、数据版本、模型包以及关键分类任务。备份文件还应另存到不同磁盘/设备，并定期演练恢复；同盘目录副本无法防护整盘故障。
