# 表格匹配 v2：复现、来源与部署

本轮全部评测使用当前工作区源码和现有只读离线包运行时，未替换历史包。主算法是 CPU 对应器，不加载 TableFormer 模型；N3 是独立研究脚本。

## 固定来源

| 对象 | revision / 许可 / 用法 |
|---|---|
| vendored TableFormer matching | `docling-ibm-models` commit `d1569fdffee1d09cb3111d6d5490991cb59d59fd`，MIT。`tf_cell_matcher.py`、`otsl.py`、`settings.py` 原文许可位于 `src/ocr_workbench/_vendor/tableformer/LICENSE`；每文件 upstream/vendored SHA 和 patch 同目录保存。修改仅为相对 import。 |
| Docling 组织方式 | commit `5ea6490ffdc57b2fd7de5cc436f2d0a22f2214d4`，只参考原生词输入、结构与文字范围分离方式，未引入完整 Docling 运行时。 |
| PaddleX | 3.7.0，Apache-2.0；主几何仍是现有分类、SLANeXt、RT-DETR cell 模型。模型 revision/文件哈希见原 `config/table-model-lock.json` 与本轮 `audit/table-matching-v2/runtime.json`。 |
| RapidTable | 3.0.2，固定源码 commit `22592283c1f9d7c5a014c96c4ecc57de0b3ebfce`，Apache-2.0；SLANet+ ONNX SHA256 `d57a942af6a2f57d6a4a0372573c696a2379bf5857c45e2ac69993f3b334514b`。仅作 CPU 研究对照。 |
| N3 TableFormer 模型 | `docling-models` revision `2199320848bb9a8a519d22e4b528185a4f9a6f64`，accurate 权重 SHA256 `2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9`。下载的 model card 声明 CDLA-Permissive-2.0 / Apache-2.0；代码 MIT。仅研究，不随应用分发权重；分发前须按具体模型文件核对许可归属。 |
| PubTables | 官方完整格标注镜像 `bsmock/pubtables-1m` revision `35b1c097807e0b07ec5313879b85956b7b3890db` 的数据卡声明 CDLA-Permissive-2.0；裁剪/OTSL 来自 `docling-project/PubTables-1M_OTSL` revision `77d8208f09ae52e5d8703c9fea5783ec37b8997c`，数据卡标为 `other`。两份卡的原文和 SHA 保存在 `audit/table-matching-v2/data-sources`；仅本地研究，不随包分发。官方 PDF bbox 映射到 crop，差异≤2px、边界舍入≤1px、跨度逐格核验。 |
| DocLayNet | 官方 1.0.0，CDLA-Permissive-1.0；官方 ZIP ETag、读取条目哈希、样本与划分锁见 pages 目录。按 HTTP Range 仅读所需条目，未下载整个约 30GB 压缩包。 |
| WTW | 既有 20 张已曝光非商业研究材料，仅引用历史 polygon 检测回执；缺转录和逻辑跨度，不能测逻辑错位，不分发图像/标注。 |

源码与数据许可分别记录。公共数据可能参与过上游模型训练，本轮只保证独立于 DeloitteOCR 的策略选择，不声称与预训练完全隔离。

## 可复算证据

- `audit/table-matching-v2/test-selection-lock.json`：最终开发/验证报告、全部对应及传递源码、评测器、策略、固定实际采用规则的封存锁。
- `build/table-matching-v2/dataset/`：开发/验证/测试输入，独立 annotations，sealed 测试标注，split-lock、protocol、qualification 和 canonical 核验记录。
- `build/table-matching-v2/inference/{development,validation,test}/`：`ppocr`、`geometry`、`rapidtable`，每引擎 immutable run-lock、逐样本成功/失败和 artifact SHA。
- `build/table-matching-v2/real-results/{development,test}/`：唯一采用规则为成功 PaddleOCR-VL 原始文字/表格、revision 0；不逐样本选最优引擎。
- `build/table-matching-v2/replay/`：B0 整表、B1 legacy Paddle、B2 legacy Rapid、N1 Paddle+v2、N2 Rapid+v2，开发/验证另有八种消融。
- `build/table-matching-v2/reports/`：完整逐目标结果、文档 bootstrap 1,000 次区间、严格输出质量、历史 bbox 指标和失败分母。audit 中仅保存去掉逐格 rows 的摘要和原文件 SHA。
- `build/table-matching-v2/pages/`：完整页检测独立划分、模型原始 boxes、评分和时延。不给检测器目标表框。
- `build/table-matching-v2/n3/probe-01/`：12 表 N3 的原始结构、后处理文字范围、压缩前后结构和模型锁。未接入产品匹配器的强制孤立文字归属。

真实结果的参考身份由独立 `geometry_reference_identity.py` 建立：唯一表文字锚点和全部最优 LCS 的无歧义 rank，只使用文字/结构，不使用待评测的定位框。参考目标无法关联计缺失；实际多出、重复或无法关联的格计额外输出，进入严格输出质量分母。这个保守关联规则本身也会损失可评目标，不能把其缺失全部归咎于几何模型。

## 重放命令

在源码根目录运行。命令不覆盖旧目录；如果代码、输入或策略改变，请使用新输出目录。以下复算测试已经曝光，不能因重跑而变成新的独立测试。

```powershell
python scripts/replay_geometry_v2.py --manifest build/table-matching-v2/dataset/inputs/test.json --candidates build/table-matching-v2/inference/test --output build/table-matching-v2/replay/test-reproduction --test-code-lock audit/table-matching-v2/test-tooling-amendment.json
python scripts/report_geometry_v2.py --annotations build/table-matching-v2/dataset/sealed/test.annotations.json --replay build/table-matching-v2/replay/test-reproduction --output build/table-matching-v2/reports/test-reproduction.json
```

真实结果轨在 replay 命令加 `--adopted-results build/table-matching-v2/real-results/test`，使用另一空输出目录，再由同一个独立 report 脚本评分。开发/验证替换 manifest 与 annotations 路径，replay 加 `--ablations`。

重新推理需要现有完整模型/运行时，GPU 任务串行执行：

```powershell
python scripts/evaluate_geometry_gpu.py --bundle E:/OCR-document-workflow-20260913/bundle --manifest build/table-matching-v2/dataset/inputs/test.json --output build/table-matching-v2/inference/test-reproduction --phase all --mapper none --test-code-lock audit/table-matching-v2/test-tooling-amendment.json
python scripts/evaluate_rapid_reference.py --manifest build/table-matching-v2/dataset/inputs/test.json --output build/table-matching-v2/inference/test-reproduction/rapidtable --ocr build/table-matching-v2/inference/test-reproduction/ppocr --mapper none --test-code-lock audit/table-matching-v2/test-tooling-amendment.json
python scripts/infer_geometry_real_results.py --manifest build/table-matching-v2/dataset/inputs/test.json --bundle E:/OCR-document-workflow-20260913/bundle --output build/table-matching-v2/real-results/test-reproduction --test-code-lock audit/table-matching-v2/test-tooling-amendment.json
```

当前复算命令使用补充锁 `test-tooling-amendment.json`：封存后只修复 replay 矩阵 tuple/list 序列化导致无法恢复相同运行的问题，匹配源码、策略、采用规则和 JSON 矩阵均未改变，原代码和主测试报告保留。该补充不产生新的完整代码独立测试结论。

无 seal 的 test 推理被 CLI 拒绝。冻结 PP-OCR 没有真实框时记录失败，禁止隐式补 OCR 或替入标注坐标。源 artifact 已有输出只在 run-lock 与所有 SHA 校验通过后复用。主测试推理在 seal 之后开始，评分阶段才读取封存目标框；本轮未按测试错误修改算法或阈值。

`freeze_geometry_v2.py` / `freeze_geometry_pages.py` 用于从合法原始材料重新准备数据，必须选新目录；它们不能保证外部下载长期可用。源材料路径、转换误差、资格和排重在原冻结目录中记录，以原 SHA 为复算依据。

## 部署与回归

未来构建时复制整个 `src/ocr_workbench/_vendor`（包括 LICENSE、lock、patch）及配置目录。现有 `sync_bundle.py` 使用目录复制，会保留这些文件；service 的 NumPy 已在 runtime lock 中，无需增加 Torch。本轮只验证现有本机运行时的导入与推理，不声称独立 Windows/A4000 验收或完成重新打包。

```powershell
& 'E:/OCR-document-workflow-20260913/bundle/runtimes/service/python.exe' -X utf8 -c "import sys,unittest;sys.path.insert(0,'C:/Users/A/Desktop/OCR/src');r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests'));sys.exit(not r.wasSuccessful())"
```

前端在 `frontend` 目录执行 `npm.cmd test`、`npm.cmd run build`。UI 回归 `scripts/audit_document_ui.py --geometry-v2` 使用外部无头 Edge，参数包含 `--disable-gpu --disable-gpu-compositing`，结束自动关闭。合成 OCR/geometry 仅用于确定性互动，不计入模型精度。缓存、策略回退及使用限制见[实施记录](table-cell-matching-v2-status.md)。
