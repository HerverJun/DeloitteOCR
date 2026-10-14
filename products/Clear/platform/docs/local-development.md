# 本机开发环境与集中依赖

当前开发根为 `F:\OCR\output\clear-automation-dev-20261002`。源码位于 `worktrees/platform`、`worktrees/ocr`、`worktrees/classification`；运行依赖及模型为 `program` 内的真实文件副本。

## 启动

双击开发根的 `启动平台.cmd`，或者运行：

```powershell
& F:\OCR\output\clear-automation-dev-20261002\worktrees\platform\start-development.ps1
```

入口默认使用既有业务工作区 `F:\OCR\output\audit-platform-dev-20261002\workspace\M2-business-preview`，平台地址 `http://127.0.0.1:8879/`。原生 OCR 和分类由平台启动，重启后从平台重新打开，避免复用旧端口。

`start-development.ps1` 可显式传入 `-DevelopmentRoot`、`-Data` 和 `-Port`。启动器及原生子进程将应用数据、缓存和临时目录显式限制在本机 F 盘。退出终端或按 Ctrl+C 会停止平台及其管理的原生服务。

## 实际依赖布局

| 路径 | 内容 |
|---|---|
| `program/runtimes/platform` | 平台 Python、FastAPI、Uvicorn 等 |
| `program/runtimes/data` | ClearData 的 Python、DuckDB、OpenPyXL 等 |
| `program/runtimes/forensics` | 经典图像取证的 Python、NumPy、OpenCV、Pillow 等 |
| `program/bundles/ocr/runtimes` | OCR 控制进程、四引擎运行环境、llama.cpp 与 CUDA 用户态库 |
| `program/bundles/ocr/models` | OCR、表格及视觉审校的离线模型 |
| `program/bundles/classification` | 分类运行环境、离线权重及启动文件 |
| `program/provenance` | 历史封存清单，仅记录复制来源 |
| `runs/local-development` | 当前服务进程和就绪登记 |
| `checks/physical-dependencies` | 隔离测试与实际运行证据 |
| `checks/dependency-cleanup-20261004` | 旧依赖删除清单与清理后验收 |
| `history/dependency-cleanup-20261004` | 旧包追溯资料和历史测试工作区存档 |

前端编译依赖已经位于各应用源码中的 `node_modules`。当前开发源码、前端和运行依赖均位于同一个开发根。

`physical_development.configure` 检查依赖树中没有目录链接，绑定当前源码，复制当前 OCR 前端，并生成本地原生服务登记。取证运行时由同目录配置提供，旧备份 venv 不再是当前运行来源。

## 复制、校验与追溯

`scripts/consolidate_dependencies.py` 按既有完整封存清单选择依赖文件，将实际字节复制到新目录，对每个目标校验大小和 SHA256。它拒绝盘外路径、目录链接和属于其他复制任务的非空目标；复制阶段保留原始包。完整计划与回执分别保存为 `dependency-manifest.json` 和 `copy-verification.json`。

源码开发需要覆盖启动文件、Python 搜索路径与前端，因此启动绑定后的哈希另写入原生 `bundle.manifest.json` 和 `development-binding.json`。原始复制回执仅证明复制完成时的封存字节，不表示后续开发覆盖文件仍与旧包相同。保留的关键模型和运行资产继续对照原声明校验，平台启动登记绑定当前入口的实际哈希。

`scripts/verify_physical_development.py` 在独立检查目录实际加载四个 OCR 运行环境的图像 DLL，执行一次新的混元识别，并验证取证报告、XLSX 读回和 Parquet SQL 聚合。业务工作区只用来只读取得既有输入，测试输出及缓存独立保存。

业务数据、识别结果、审计记录、凭据与业务缓存继续归原工作区。历史源码路径和旧包路径可以出现在追溯清单中，它们不参与这个启动入口的依赖加载。

## 旧依赖清理状态（2026-10-04）

用户确认集中后清理旧文件。本机再次核验当前 `program` 的全部 107,536 个实际文件（含开发覆盖哈希）通过，随后删除旧预览包 `program`、取证修复包 `program/candidate/candidate-final/candidate-ready`、业务工作区旧 `native-program` 启动层及停用缓存、旧启动入口。删除目录链接时只移除链接本身。

旧包的追溯清单、打包源码、日志及可读取的历史测试工作区已归档到当前开发根 `history/dependency-cleanup-20261004`。旧包已无法直接启动或回退；复制来源在清单中属于历史路径。原业务数据、原始源码、独立源码备份及历史审计报告保留。旧临时根 `F:\OCR\output\audit-platform-dev-20261002\temp` 仅剩 11 个因 Windows 权限拒绝访问的测试目录。

清理后平台、数据处理、取证列表及两个原生健康接口通过；原 OCR 结果的 TXT/JSON 导出与清理前 SHA256 一致。清理前后记录在 `checks/dependency-cleanup-20261004`。
