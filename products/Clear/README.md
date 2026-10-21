> 当前登记（2026-10-22）：正式 8879 为 Clear 0.16.0 SAFIRE；文档/Pro 候选 0.17.0 在 8985；本轮真实 Debug 候选 0.17.1 在 8986。本目录同步 2036 个登记源码文件，实际运行版本以各候选清单及正式登记为准。详见[项目总览与真实 Debug](../../docs/platform-automation/Clear项目总览与真实Debug-20261022.md)。

# Clear 三应用源码与版本对应

本目录把当前 Clear 的平台、分类和原生 OCR 自有工程文件集中到同一 GitHub 仓库，便于审阅、检索和后续开发。最新捕获日期为 2026-10-22；**这是带来源身份的源码快照，不是完整离线安装目录，也不是三个原开发仓库的 Git 历史迁移。** 原目录、正式运行服务和业务数据均保留。

| 本仓目录 | 原开发目录 | 本轮文件数 | 作用 |
|---|---|---:|---|
| [platform](platform/) | `F:\OCR\output\frontend-workbench-20261015\worktrees\platform` | 906 | 门户、Agent 工作台、文档结构、Harness 适配、业务服务、任务中心与工具目录 |
| [classification](classification/) | `F:\OCR\output\clear-automation-dev-20261002\worktrees\classification` | 195 | 图像标注、训练、模型、推理与人工复核 |
| [ocr](ocr/) | 同登记开发根 `worktrees/ocr` | 935 | Clear 对应的原生 OCR 源码、前端、配置、测试和许可，含 MinerU Pro 接入 |

每仓 HEAD、分支、工作区状态以及全部快照文件的 SHA256/大小见 [source-manifest.json](source-manifest.json)。快照读取当前工作区字节。本轮平台 HEAD 为 `e5ca875ed822643219b22c688d23be72c87b06e7`，OCR 为 `adab40a77ad294a5011f1212491dd4277ed8d772`，分类为 `1be916be64fcbe425cae356f28c106c586da3d7a`，捕获时三仓均干净。来源仓库和远端配置保留。

当前正式入口为 **0.16.0 SAFIRE**、<http://127.0.0.1:8879/>。本轮审阅源码和独立 Debug 候选包含文档/Pro 接入与金额、取消恢复、结构阅读修复，没有切换正式登记。此前 0.14.0 的通用 Agent 能力、旧会话/成果下载、冷备和回退历史证据见[通用能力实施与验收](../../docs/platform-automation/Agent通用能力恢复实施与验收-20261019.md)。Agent 的开放分析仍使用官方 DeepSeek Harness 内核。下述较早候选与预览记录保留作历史来源，当前登记以 `release/Clear/current.json` 为准。

新版前端的参考来源、改动与本机预览见[改版记录](../../docs/platform-automation/前端工作台改版与预览-20261015.md)。最新[门户首页补齐](../../docs/platform-automation/门户首页评估与补齐-20261015.md)将默认入口改为导航门户，AI 工作区独立为 `/agent`，保留旧会话链接；本轮 29 项定向回归、23 项首页和 21 项工具目录浏览器检查通过。预览首页为 <http://127.0.0.1:8910/>，正式入口继续使用 8879。

此前取证稳定性功能、工程验收范围与回退见[取证稳定性候选说明](platform/docs/forensics-stability-20261014.md)。该阶段独立完整候选在本机 `F:\OCR\output\forensics-stability-20261014\delivery`，当时正式入口为 0.10.0；合成评测通过不代表业务准确率已验收。

最新多人任务中心见[实现记录](platform/docs/task-center-implementation.md)、[运维说明](platform/docs/task-center-operations.md)与[验收报告](../../docs/platform-automation/Clear多人任务中心实施与验收-20261015.md)。候选在 `F:\OCR\output\task-history-multiuser-20261015\delivery-v2`，共享模型/运行时，不切换正式入口。

## 与根仓和发行版本的区别

- 根仓 `src/`、`frontend/` 是独立 OCR 研发线，当前 schema 18，含较新的屏拍等功能。
- 本目录 `ocr/` 是 Clear 原生 OCR 开发线。正式运行源码取自 `current.json` 选择的 0.16.0 候选根 `ocr/`，本轮 0.17.1 使用其独立候选源码；0.10.0 目录继续提供已登记的共享运行时/模型。运行身份须以各候选逐文件清单核对。
- 正式 Clear 指针在 [current.json](../../release/Clear/current.json)。运行时 `launch.py` 从发行根的 `platform`、`ocr`、`classification` 加载代码，OCR 网页由发行根 `ocr/frontend/dist` 提供；`program/bundles/ocr` 中旧 `src/web` 是底层资产，不是实际界面基线。
- `release/Clear/versions/` 里的完整离线安装目录留在本机并忽略。Python、模型、GPU DLL、依赖、编译产物、业务工作区及大型 ZIP 不进入 Git。其身份、兼容要求和完整包交付证据仍由登记与交付文档说明。

## 捕获与校验

入口脚本为 [sync_clear_sources.py](../../scripts/sync_clear_sources.py)。它从原仓 `git ls-files --cached --others --exclude-standard` 取得文件清单，读取当前工作区字节，排除运行数据、模型、依赖、缓存、构建、历史 audit/research 以及可执行大包，记录来源 HEAD 和 dirty 状态。许可证、前端品牌字体、配置、测试和必要静态源资产随源保留；例如 OCR 的 NotoSansSC 字体及其许可属于前端构建源资产。

```powershell
# 先按项目存储规范设置 F 盘 TEMP/TMP/LOCALAPPDATA/APPDATA。
# 从 F:\OCR 运行；原开发仓须存在。
python -B scripts/sync_clear_sources.py --platform-source F:\OCR\output\frontend-workbench-20261015\worktrees\platform
python -B scripts/sync_clear_sources.py --verify
# 如需单独捕获此前取证稳定性隔离源：
python -B scripts/sync_clear_sources.py --platform-source F:\OCR\output\forensics-stability-20261014\worktrees\platform
```

`--verify` 只读检查清单成员的字节和自有文件清单，忽略规则排除的运行缓存；不运行模型，不读取业务工作区，也不证明发行包与来源 HEAD 完全一致。重新捕获会覆盖同名快照源文件，不移动或删除原仓，也不会自动清除旧快照中的多余文件；变更源文件范围时应审阅 Git diff 与清单差异。不要把捕获动作当作发布切换。

根仓 `.gitattributes` 对本目录关闭换行转换，保证 GitHub 克隆后能够重新核对清单哈希。源仓自身的 `.gitattributes` 原字节保存为 `.gitattributes.source`，避免其中的换行规则覆盖快照策略；每个清单成员的 `source_path` 记录原相对路径。编辑快照文件后，其原始来源哈希自然会过期，必须说明实际改动并重新登记，不能直接沿用旧清单宣称来源一致。

## 开发与运行思路

平台与分类各自保留 `pyproject.toml`、前端 `package.json`/锁文件和测试；OCR 保留前端锁文件与离线运行时/模型锁。开发时使用 F 盘独立环境与 F 盘隔离工作区，按各工程配置构建，不复用真实业务数据库。原工程中的固定历史路径仅作来源追溯，运行前统一到 F 盘实际目录。

本次没有改写既有启动器对原目录的绑定，没有把生产入口切换到这个快照，也没有新增下载或安装模型。完整启动依赖仍需已校验的离线运行资产及原生登记；仅 `pip install` 平台基础依赖不能得到四引擎 OCR 或新增多模态模型环境。

面向使用者请从[平台使用说明书](../../docs/Clear审计平台使用说明书.md)开始；面向开发者见[架构设计与实现思路](../../docs/Clear架构设计与实现思路.md)。
