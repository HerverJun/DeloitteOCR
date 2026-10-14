# 复现、冻结与许可

开发前基线包含会话开始前的工作区修改：`audit/structure-workflow-20260916/baseline-source.json` 与 `build/structure-workflow-20260916/baseline-source.zip`，不只记录 Git HEAD。最终源码/文档为新交付目录的 `structure-workflow-source.zip`、`release-source-lock.json`。运行时/模型沿用旧完整包的固定字节，再加入配置锁定的 TableFormer 和 RapidTable 文件；新包单独生成，旧目录未覆盖。

下载、解析、各阶段推理、重放和每轮修复分别保留哈希与首轮失败。公开开发运行期间还进行了实现修复，因此不冒充冻结后的独立封存测试。最终安装副本验证以 `delivery-evidence` 为准，包括复制字节核验、离线模型来源和真实导出复验。

在解压的源码目录执行以下 PowerShell 命令，输出目录须为新目录。完整运行包自带已构建页面，使用者不需要 Node/npm。

```powershell
$servicePython = 'E:/OCR-structure-workflow-20260916/bundle/runtimes/service/python.exe'
$bundlePath = 'E:/OCR-structure-workflow-20260916/bundle'
$runPath = 'build/structure-workflow-reproduction'
& $servicePython -X utf8 -c "import sys,unittest;sys.path[:0]=['src','tests'];r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests'));sys.exit(not r.wasSuccessful())"
Push-Location frontend
npm.cmd ci --offline
npm.cmd test
npm.cmd run build
Pop-Location
# 下载仅在联网准备阶段进行，应用离线运行不下载。
python -X utf8 scripts/prepare_structure_public.py --output "$runPath/public-pdfs" --bundle $bundlePath --fetch-only
& $servicePython -X utf8 scripts/prepare_structure_public.py --output "$runPath/public-pdfs" --bundle $bundlePath
& $servicePython -X utf8 scripts/evaluate_public_pdf_geometry.py infer --manifest "$runPath/public-pdfs/inputs/development.json" --output "$runPath/inference" --bundle $bundlePath --phase all
& $servicePython -X utf8 scripts/evaluate_public_pdf_geometry.py replay --manifest "$runPath/public-pdfs/inputs/development.json" --output "$runPath/inference" --replay-name replay-01
& $servicePython -X utf8 scripts/audit_structure_public.py --data "$runPath/public-pdfs" --inference "$runPath/inference" --output "$runPath/workflow" --bundle $bundlePath
& $servicePython -X utf8 scripts/audit_structure_ui.py --output "$runPath/ui" --bundle $bundlePath
```

`npm ci --offline` 需要开发机事先具有锁定 npm 缓存。部分历史 PDF 运行时测试依赖原工作区 `build/document-workflow` 夹具，缺少时显式跳过，不能计为通过。安装副本真实导出另由 `audit_structure_export.py --installed` 验证。

官方 GriTS 固定 `microsoft/table-transformer` commit `ef21069f2a42254bbd049609f7d4bebc25abcf09`，MIT；`grits.py` SHA256 为 `75c13031657252c251f5d0b483aca2601e229a06d46e60d0aa3c739418f543d7`。上游源码未改，适配器只转换数组类型。`scripts/structure_eval.py` 要求双人原图核验、歧义清零的标注；输入基线/候选分开，不允许真实整页评分使用参考裁剪。无合格标注时不生成虚构成绩。

本机评分仅在 `build/structure-workflow-20260916/scoring-runtime` 安装 PyMuPDF 1.26.4，配合开发环境 numpy。PyMuPDF 为 AGPL/商业双许可，本次新增的 1.26.4 只作隔离评分，未复制到运行包。旧包 GLM 运行时已包含 PyMuPDF 1.28.2，本轮完整继承，并保留其 dist-info/COPYING 与原依赖许可；不能据此声称产品完全不含 PyMuPDF。在符合其许可的隔离环境安装 numpy、PyMuPDF==1.26.4，可运行 6 项 `test_structure_evaluation`。服务环境缺少该依赖的官方评分类会显式跳过。产品 PDF 导出继续使用已有独立 PDF 运行时及其许可清单。

| 上游 | 本轮使用与固定 |
|---|---|
| Docling / Docling Parse | 已有原生词/字符坐标及可靠性判断；延续 `licenses/document-workflow` 清单 |
| TableFormer / docling-ibm-models | 原始结构、OTSL、跨度和表头角色；源码 `d1569fdffee1d09cb3111d6d5490991cb59d59fd`，模型 `2199320848bb9a8a519d22e4b528185a4f9a6f64`；MIT 源码，模型卡列出 CDLA-Permissive-2.0/Apache-2.0，随包保存 LICENSE/模型卡 |
| RapidTable | 3.0.2 外部固定 OCR、SLANet+；Apache-2.0，保留依赖 dist-info 与原许可 |
| PaddleX | 已有 PP-DocLayout/表格默认路线，延续模型锁、Apache-2.0 及原 NOTICE/依赖清单 |
| Microsoft GriTS | MIT 官方评分源码与许可随源代码；不增加产品默认质量判断 |

TableFormer 后处理中的删列、强制孤立文字归属及编号压缩不直接写入采用结果；本轮未证明其额外收益，因此未启用。提供方全部文件哈希见 `config/geometry-providers.json`。其它 OCR 引擎/模型依赖延续原离线包许可清单。

公开 PDF 原文及生成的实验图像/表格留在本机实验目录，不随应用 ZIP 分发；来源 URL、commit、SHA256 和失败记录保留。打包入口为 `scripts/release_structure_bundle.py` 与 `scripts/package_bundle.py`，源码逐文件按冻结锁复制校验，实验资产路径与配置一致。ZIP64 附完整文件清单、SHA256 及回执；`ocr.cmd verify` 可复核解压内容。本轮没有另一台干净 Windows/A4000 的验收结论。
