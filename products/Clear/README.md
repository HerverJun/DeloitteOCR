# Clear 三应用源码与版本对应

本目录把当前 Clear 的平台、分类和原生 OCR 自有工程文件集中到同一 GitHub 仓库，便于审阅、检索和后续开发。整理日期为 2026-10-14；**这是带来源身份的源码快照，不是完整离线安装目录，也不是三个原开发仓库的 Git 历史迁移。** 原目录、运行服务和业务数据均保留。

| 本仓目录 | 原开发目录 | 本轮文件数 | 作用 |
|---|---|---:|---|
| [platform](platform/) | `F:\OCR\output\forensics-stability-20261014\worktrees\platform` | 见来源清单 | 门户、统一契约、Data、取证、多模态、审计与 Agent；本轮取证稳定性隔离源 |
| [classification](classification/) | 同根 `classification` | 190 | 图像标注、训练、模型、推理与人工复核 |
| [ocr](ocr/) | 同根 `ocr` | 926 | Clear 对应的原生 OCR 源码、前端、配置、测试和许可 |

每仓 HEAD、分支、工作区状态以及全部快照文件的 SHA256/大小见 [source-manifest.json](source-manifest.json)。快照读取当前工作区字节。平台以 `eeea058fe4ac01716a89c0617e0815141888f426` 为基线，在 F 盘隔离分支 `codex/forensics-stability-r1` 完成本轮热图定位、EXIF 坐标、CAT-Net 上下文分块和复核查看器。原平台开发目录保留在基线，后续本轮续作应使用清单登记的隔离源；分类、OCR 仍读取原登记目录。来源仓库远端保持原配置。

本轮功能、工程验收范围与回退见[取证稳定性候选说明](platform/docs/forensics-stability-20261014.md)。独立完整候选在本机 `F:\OCR\output\forensics-stability-20261014\delivery`，正式入口保持 0.10.0；合成评测通过不代表业务准确率已验收。

## 与根仓和发行版本的区别

- 根仓 `src/`、`frontend/` 是独立 OCR 研发线，当前 schema 18，含较新的屏拍等功能。
- 本目录 `ocr/` 是 Clear 原生 OCR 开发线，schema 17。它与正式发行根 `release/Clear/versions/0.10.0/ocr` 的运行源码对应；开发快照仍带未提交改动，不能直接声称全部字节与发行包一致。
- 正式 Clear 指针在 [current.json](../../release/Clear/current.json)。运行时 `launch.py` 从发行根的 `platform`、`ocr`、`classification` 加载代码，OCR 网页由发行根 `ocr/frontend/dist` 提供；`program/bundles/ocr` 中旧 `src/web` 是底层资产，不是实际界面基线。
- `release/Clear/versions/` 里的完整离线安装目录留在本机并忽略。Python、模型、GPU DLL、依赖、编译产物、业务工作区及大型 ZIP 不进入 Git。其身份、兼容要求和完整包交付证据仍由登记与交付文档说明。

## 捕获与校验

入口脚本为 [sync_clear_sources.py](../../scripts/sync_clear_sources.py)。它从原仓 `git ls-files --cached --others --exclude-standard` 取得文件清单，读取当前工作区字节，排除运行数据、模型、依赖、缓存、构建、历史 audit/research 以及可执行大包，记录来源 HEAD 和 dirty 状态。许可证、前端品牌字体、配置、测试和必要静态源资产随源保留；例如 OCR 的 NotoSansSC 字体及其许可属于前端构建源资产。

```powershell
# 先按项目存储规范设置 F 盘 TEMP/TMP/LOCALAPPDATA/APPDATA。
# 从 F:\OCR 运行；原开发仓须存在。
python -B scripts/sync_clear_sources.py
python -B scripts/sync_clear_sources.py --verify
# 捕获本轮取证稳定性隔离源：
python -B scripts/sync_clear_sources.py --platform-source F:\OCR\output\forensics-stability-20261014\worktrees\platform
```

`--verify` 只读检查清单成员的字节和自有文件清单，忽略规则排除的运行缓存；不运行模型，不读取业务工作区，也不证明发行包与来源 HEAD 完全一致。重新捕获会覆盖同名快照源文件，不移动或删除原仓，也不会自动清除旧快照中的多余文件；变更源文件范围时应审阅 Git diff 与清单差异。不要把捕获动作当作发布切换。

根仓 `.gitattributes` 对本目录关闭换行转换，保证 GitHub 克隆后能够重新核对清单哈希。源仓自身的 `.gitattributes` 原字节保存为 `.gitattributes.source`，避免其中的换行规则覆盖快照策略；每个清单成员的 `source_path` 记录原相对路径。编辑快照文件后，其原始来源哈希自然会过期，必须说明实际改动并重新登记，不能直接沿用旧清单宣称来源一致。

## 开发与运行思路

平台与分类各自保留 `pyproject.toml`、前端 `package.json`/锁文件和测试；OCR 保留前端锁文件与离线运行时/模型锁。开发时使用 F 盘独立环境与 F 盘隔离工作区，按各工程配置构建，不复用真实业务数据库。原工程中的固定历史路径仅作来源追溯，运行前统一到 F 盘实际目录。

本次没有改写既有启动器对原目录的绑定，没有把生产入口切换到这个快照，也没有新增下载或安装模型。完整启动依赖仍需已校验的离线运行资产及原生登记；仅 `pip install` 平台基础依赖不能得到四引擎 OCR 或新增多模态模型环境。

面向使用者请从[平台使用说明书](../../docs/Clear审计平台使用说明书.md)开始；面向开发者见[架构设计与实现思路](../../docs/Clear架构设计与实现思路.md)。
