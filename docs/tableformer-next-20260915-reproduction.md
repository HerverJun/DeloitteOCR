# TableFormer raw 下一轮开发：复现与使用

本轮在现有工作区基础上接入可选几何提供方和 `local-v3` 邻居锚点实验。默认仍为 Paddle + `local-v2`；不新增文字识别票，不修改采用文字，不重新打包 E 盘历史交付目录。独立质量结果见 [质量报告](tableformer-next-20260915-quality.md)。

## 应用入口与来源

在「表格定位与人工绑定 · 实验性」选择定位模型和对应方式，再请求补充定位。API 为 `POST /api/results/{id}/geometry`，参数示例：

```json
{"revision": 0, "provider": "tableformer-raw", "algorithm": "local-v3"}
```

`provider` 可为 `paddle`、`tableformer-raw`、`rapidtable`；`algorithm` 可为 `local-v2`、`local-v3`。旧 `legacy` 仅支持 Paddle。默认参数保留原行为。人工绑定优先，切换模型不会删除历史产物。对应结果读取支持显式提供方/算法，PDF 导出使用最近成功请求的证据；失效的策略/代码证据不会恢复。

`config/geometry-providers.json` 固定 TableFormer accurate 权重、配置和完整 Python 来源文件哈希。沿用上一轮源码 commit `d1569fdffee1d09cb3111d6d5490991cb59d59fd`、模型 revision `2199320848bb9a8a519d22e4b528185a4f9a6f64`，权重 SHA256 `2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9`。关闭后处理与行列编号压缩，保留 `predict_details.table_cells`。原始 `upstream.json`、模型锁、原始编号和裁剪到图像的显式变换均随产物保存。

实验依赖使用本机已验证的离线目录；缺失或哈希不符时明确失败，不联网补下载。RapidTable 原目录部分文件仅允许先前沙箱账户读取，因此本轮将依赖复制到 `build/tableformer-next-20260915/runtime/rapid-dependencies` 并逐文件校验内容一致；未修改原目录权限、源码或权重。源码工作区的实验路径不等于可搬迁的发布包。

提供方通过独立进程运行。缺少表区域或 OCR 时，先串行执行共享页面检测/OCR，再卸载其进程并加载候选提供方。TableFormer 使用 GLM 运行时中的现有 Torch；RapidTable 使用 ONNX CPU。裁剪内推理结果还原到当前图像版本，跨表或跨裁剪 OCR 不会被拆分或复制。

候选缓存绑定图像、版本、区域、共享 OCR、模型锁、提供方和坐标代码；对应缓存另绑定采用文字、结构、修订、算法与策略。读取缓存时核验候选及上游原始文件哈希，修订后可以纯 CPU 重放。

## 边界契约与对应规则

`config/geometry-evaluation-v3.json` 在新测试准备前固定 `full_grid` 为完整格主口径，明确区分 `text_extent` 与 `pdf_cell_extent`。独立标签构造不接收 OCR 或模型预测。旧报告和原 PDF 框均保留，不以修改旧标签替换历史成绩。

官方结构图像标签来自固定 PubTables revision `35b1c097807e0b07ec5313879b85956b7b3890db` 的 `PubTables-1M-Structure_Annotations_Test.tar.gz`。交叉校验开发 80 表、8,488 格，最大边界差 0.9943 像素，全部在 2 像素标签舍入容差内。该容差用于官方标签转换检查，不修改评分的 polygon IoU≥0.5。

`local-v3` 以现有唯一精确文字锚点为起点。重复值必须有独立行列支持；没有同行/同列锚点时，只在两侧可靠邻居保留顺序和距离时使用夹定约束，不做单侧外推。每轮同时检查候选并列、复用和顺序冲突；依赖保留到初始独立根锚点，禁止循环自证。空格不能成为新文字锚点。合并格仍需要连续、无洞的真实候选并集。

token 包含率 0.8、margin 0.2、其他候选重合上限 0.2、表身份 margin 0.2、单表 200ms 预算及原分组上限均保留。没有通过宽松字符归一化、补 OCR、框缩放或按测试样本挑选提供方换取覆盖。

## 已执行流程

所有大型产物位于 `build/tableformer-next-20260915`，精简回执位于 `audit/tableformer-next-20260915`。新产物目录不可覆盖；重新实验须另选目录并重新封存。嵌入式 service Python 忽略环境 `PYTHONPATH`，调用旧脚本需显式插入 `scripts`；本轮新增脚本已自行设置来源路径。

```powershell
$servicePython = 'E:/OCR-document-workflow-20260913/bundle/runtimes/service/python.exe'
$experimentRoot = 'build/tableformer-next-20260915'

python -B -X utf8 scripts/prepare_public_pdf_geometry.py fetch --output "$experimentRoot/public-pdfs"
& $servicePython -B -X utf8 scripts/prepare_public_pdf_geometry.py render --output "$experimentRoot/public-pdfs" --bundle E:/OCR-document-workflow-20260913/bundle
& $servicePython -B -X utf8 scripts/evaluate_public_pdf_geometry.py infer --manifest "$experimentRoot/public-pdfs/inputs/development.json" --output "$experimentRoot/public-inference-02" --bundle E:/OCR-document-workflow-20260913/bundle --phase all
& $servicePython -B -X utf8 scripts/evaluate_public_pdf_geometry.py replay --manifest "$experimentRoot/public-pdfs/inputs/development.json" --output "$experimentRoot/public-inference-02"

python -B -X utf8 scripts/audit_geometry_label_contract.py --manifest build/table-matching-v2/dataset/inputs/development.json --annotations build/table-matching-v2/dataset/annotations/development.annotations.json --canonical build/table-matching-v2/dataset/canonical --output "$experimentRoot/label-audit"
python -B -X utf8 scripts/freeze_tableformer_v3.py --output "$experimentRoot/dataset" --candidate-count 140 --test-count 120

foreach ($splitName in @('development', 'validation')) {
    & $servicePython -B -X utf8 scripts/develop_tableformer_v3.py labels --manifest "build/table-matching-v2/dataset/inputs/$splitName.json" --annotations "build/table-matching-v2/dataset/annotations/$splitName.annotations.json" --canonical build/table-matching-v2/dataset/canonical --output "$experimentRoot/labels/$splitName"
    & $servicePython -B -X utf8 scripts/develop_tableformer_v3.py replay --manifest "build/table-matching-v2/dataset/inputs/$splitName.json" --candidates "build/table-matching-v2/inference/$splitName" --tableformer "build/tableformer-expanded-20260914/inference/$splitName/tableformer" --output "$experimentRoot/replay/$splitName-final"
    & $servicePython -B -X utf8 scripts/develop_tableformer_v3.py report --annotations "$experimentRoot/labels/$splitName/annotations/$splitName.annotations.json" --replay "$experimentRoot/replay/$splitName-final" --output "$experimentRoot/reports/$splitName-final.json"
}

& $servicePython -B -X utf8 scripts/develop_tableformer_v3.py seal --manifest "$experimentRoot/dataset/inputs/test.json" --test-inference "$experimentRoot/inference/test" --development-report "$experimentRoot/reports/development-final.json" --validation-report "$experimentRoot/reports/validation-final.json" --development-replay "$experimentRoot/replay/development-final" --validation-replay "$experimentRoot/replay/validation-final" --label-audit "$experimentRoot/label-audit/crosscheck.json" --output "$experimentRoot/selection-seal.json"
& $servicePython -B -X utf8 scripts/run_tableformer_acceptance.py --root $experimentRoot --bundle E:/OCR-document-workflow-20260913/bundle
& $servicePython -B -X utf8 scripts/summarize_tableformer_next.py --root $experimentRoot --output audit/tableformer-next-20260915
```

新测试准备排除原历史 100 表、v2 全部 320 个已准备文档、扩大实验全部 160 个已准备文档及既有排除记录，并继续使用 pHash≤6、同形状文字 Jaccard≥0.9 排除近重复。从 140 个新合格候选按固定 seed 选 120 个文档；输入与标签分开，标签在准备时即采用网格口径。新测试推理前封存代码、策略、开发/验证报告、输入和标签锁。控制轨固定 reference edit；真实结果轨固定采用 PaddleOCR-VL，识别失败和不可建立参考身份的目标仍保留在分母。

精简汇总读取四份完整评分，验证封存源码、各阶段运行锁和原始产物 SHA256，并附上工程、队列、UI、视觉抽查及公开来源回执。覆盖差区间使用固定 seed=20260915 的 2,000 次成对文档 bootstrap；单方法区间沿用独立评分器的 1,000 次文档 bootstrap。原始逐格评分保留在 build，汇总只复制统计和回执。

公开 PDF 共 7 份、14 个页面：Camelot 和 pdfplumber 的固定 GitHub commit 样本，以及香港政府 2024–25 预算案中文原文。下载来源、跳转、SHA256 与页码保留。原始 PDF 保持不变；图像型 PDF 不冒称已证明物理扫描来源。没有独立逐格标注的公开页面只用于工程开发，不能合并进精度主指标。

第一次公开输入运行 `public-inference-01` 在加载模型前被 GPU 锁文件权限拦截；保留全部回执。获得执行权限后在 `public-inference-02` 原配置运行。RapidTable 的目录访问修复发生在其模型加载前，已成功的 OCR/采用结构/Paddle/TableFormer 产物未重跑择优。首轮开发探索结果保留于 `development-01`；最终代码重新执行完整开发/验证，并在封存后运行新测试。

## 工程验证入口

```powershell
& $servicePython -B -X utf8 -c "import sys,unittest;sys.path[:0]=['src','scripts','tests'];r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests'));sys.exit(not r.wasSuccessful())"
& $servicePython -B -X utf8 scripts/audit_tableformer_queue.py --bundle E:/OCR-document-workflow-20260913/bundle --dataset "$experimentRoot/public-pdfs" --inference "$experimentRoot/public-inference-02" --output "$experimentRoot/queue-audit"
& $servicePython -B -X utf8 scripts/audit_document_ui.py --bundle E:/OCR-document-workflow-20260913/bundle --output "$experimentRoot/ui-audit" --geometry-v2
```

前端在 `frontend` 目录运行 `npm.cmd test` 和 `npm.cmd run build`。外部无头 Edge 使用 `--disable-gpu --disable-gpu-compositing`，验证后关闭。视觉抽查记录明确标注为 Codex 检查，未伪称人工签核。没有新增人工效率、A4000 或独立 Windows 发布验收结论。
