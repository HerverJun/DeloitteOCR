# TableFormer 原始结构框扩大实验：复现记录

本轮只扩大几何候选对照，不调整对应器、不增加文字识别票、不改变产品默认策略。使用 `compare_tableformer_geometry.py` 完成模型推理、统一 CPU 重放、开发/验证报告验收和新测试封存。

## 数据与顺序

- 开发：沿用 v2 的 80 个文档、8,488 格；验证：沿用 60 个文档、5,930 格。
- 测试：`freeze_tableformer_holdout.py` 从固定公开来源准备 160 个合格候选，按固定 seed 选择 120 个新文档、11,667 格。排除旧 100 表、旧 v2 全部 320 个已准备候选及历史排除文档，共 424 个文档；pHash 距离≤6，或同形状文字集合 Jaccard≥0.9 的近重复也排除。
- 每表 50–250 格；核对 canonical PDF 框与 crop 坐标转换、完整格数量和连续跨度。坐标转换差异≤2px，最多允许 1px 的图像边界越界并裁回。输入、几何标签分别存储。资格检查读取标签；推理与对应不读取几何标签。
- 顺序为：完成开发推理/重放/评分 → 完成验证推理/重放/评分 → 保存 selection seal → 新测试推理/重放 → 打开测试标签评分。开发/验证不做阈值搜索。
- 新测试 seal 时间及开发/验证报告哈希见 `build/tableformer-expanded-20260914/selection-seal.json`。测试首次启动因 LocalAppData 的 GPU 锁文件访问受限，尚未加载模型即退出；自动审批后原配置继续，没有修改代码或查看测试结果。

## 控制变量

三条路线均使用同一张图、同一份 PP-OCR 输出、同一 reference edit、同一 `result_id` / `image_version`、同一 `local_mapping` 和 `local-v2-development-1` 策略。Paddle worker 原有的空文字/无 polygon 过滤保持原样；CPU 对应时三条路线均传入完整的共享 OCR 块，禁止用候选内部文字替换或补识别。没有对测试逐表挑选引擎，也不按结果重跑择优。

Paddle / RapidTable 开发和验证候选复用已有哈希产物。旧 Paddle 锁中的 geometry provider/contract 文件早于后续适配扩展；直接推理 worker 未变，140 表的保存 lineage 全部能由当前 `paddle_lineage` 原样重建，0 个差异。最终对应统一使用当前源码。

TableFormer 使用 accurate safetensors；保持上次 12 表探针的权重、配置、源码、随机 seed 20260913 和 4 个线程。禁用后处理及行列编号压缩。提取 `predict_details.table_cells`，保留原始 `upstream.json`；不使用后处理生成的文字范围来冒充完整格。上游 `do_matching=True` 仅执行其生成结构坐标所需流程，最终跨候选对照的文字对应统一由本地对应器完成。模型不接收 reference edit 或 GT 框。

GPU 推理串行；CPU 对应独立使用 service Python。RapidTable 候选推理沿用既有 Anaconda Python；首次尝试 service Python 因缺少 tqdm 在模型加载前退出，随后使用已有完整运行时，没有安装依赖或改变模型。按样本轮换三种候选的重放顺序。保留既有 200ms 预算及所有超时/失败分母。模型推理、对应器和包含文件校验/读取的重放时间分别统计；TableFormer 的候选耗时包含 OCR 产物校验和图像读取，各引擎内部计时边界不是完全相同。

## 固定来源

| 对象 | 锁定内容 |
|---|---|
| TableFormer 源码 | `docling-project/docling-ibm-models`，commit `d1569fdffee1d09cb3111d6d5490991cb59d59fd`；本地完整 Python 文件 SHA |
| TableFormer 模型 | revision `2199320848bb9a8a519d22e4b528185a4f9a6f64`；权重 SHA256 `2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9` |
| RapidTable | 3.0.2 / SLANet+；ONNX SHA256 `d57a942af6a2f57d6a4a0372573c696a2379bf5857c45e2ac69993f3b334514b` |
| Paddle | 现有 PaddleX 3.7.0 分类、SLANeXt、RT-DETR 候选；离线包模型锁 |
| PubTables canonical | revision `35b1c097807e0b07ec5313879b85956b7b3890db`；原 OTSL parquet SHA 见 dataset/protocol.json |
| 对应/评分 | 原 `mapping_code_lock()` 全部文件 SHA；额外锁本轮 runner、freezer、TableFormer 源码/配置/权重和新测试输入 |

上游源码与数据许可、原始数据卡沿用 `docs/table-cell-matching-v2-reproduction.md` 的来源记录。本轮公开材料仅在本地实验使用，audit 只保留摘要与哈希。新测试独立于已知 DeloitteOCR 开发材料；不声称与上游模型预训练数据完全隔离。

## 实际命令

下列是已用路径和执行顺序。已有锁和产物只允许哈希一致的恢复；评分输出不可覆盖，重新评分须另选文件名。模型推理不需要网络，数据准备需要固定公开镜像。service 嵌入式 Python 调用旧脚本时显式加入 scripts 路径。

```powershell
$servicePython = 'E:/OCR-document-workflow-20260913/bundle/runtimes/service/python.exe'
$modelPython = 'E:/OCR-document-workflow-20260913/bundle/runtimes/glm/python.exe'
$experimentRoot = 'build/tableformer-expanded-20260914'
$oldDataset = 'build/table-matching-v2/dataset'

# 只用于首次准备；已冻结目录会拒绝覆盖。
python -B -X utf8 scripts/freeze_tableformer_holdout.py --output "$experimentRoot/dataset"

foreach ($splitName in @('development', 'validation')) {
    & $modelPython -B -X utf8 scripts/compare_tableformer_geometry.py infer --manifest "$oldDataset/inputs/$splitName.json" --ocr "build/table-matching-v2/inference/$splitName/ppocr" --output "$experimentRoot/inference/$splitName/tableformer"
    & $servicePython -B -X utf8 scripts/compare_tableformer_geometry.py replay --manifest "$oldDataset/inputs/$splitName.json" --candidates "build/table-matching-v2/inference/$splitName" --tableformer "$experimentRoot/inference/$splitName/tableformer" --output "$experimentRoot/replay/$splitName"
    & $servicePython -B -X utf8 -c "import sys,runpy; sys.path.insert(0,'scripts'); runpy.run_path('scripts/report_geometry_v2.py',run_name='__main__')" --annotations "$oldDataset/annotations/$splitName.annotations.json" --replay "$experimentRoot/replay/$splitName" --output "$experimentRoot/reports/$splitName.json"
}

& $servicePython -B -X utf8 scripts/compare_tableformer_geometry.py seal --manifest "$experimentRoot/dataset/inputs/test.json" --development-report "$experimentRoot/reports/development.json" --validation-report "$experimentRoot/reports/validation.json" --development-replay "$experimentRoot/replay/development" --validation-replay "$experimentRoot/replay/validation" --output "$experimentRoot/selection-seal.json"

& $servicePython -B -X utf8 scripts/compare_tableformer_geometry.py verify-seal --manifest "$experimentRoot/dataset/inputs/test.json" --test-code-lock "$experimentRoot/selection-seal.json"
& $servicePython -B -X utf8 -c "import sys,runpy; sys.path.insert(0,'scripts'); runpy.run_path('scripts/evaluate_geometry_gpu.py',run_name='__main__')" --bundle E:/OCR-document-workflow-20260913/bundle --manifest "$experimentRoot/dataset/inputs/test.json" --output "$experimentRoot/inference/test" --phase all --mapper none --test-code-lock "$experimentRoot/selection-seal.json"
& 'D:/anaconda3/python.exe' -B -X utf8 scripts/evaluate_rapid_reference.py --manifest "$experimentRoot/dataset/inputs/test.json" --output "$experimentRoot/inference/test/rapidtable" --ocr "$experimentRoot/inference/test/ppocr" --mapper none --test-code-lock "$experimentRoot/selection-seal.json"
& $modelPython -B -X utf8 scripts/compare_tableformer_geometry.py infer --manifest "$experimentRoot/dataset/inputs/test.json" --ocr "$experimentRoot/inference/test/ppocr" --output "$experimentRoot/inference/test/tableformer" --test-code-lock "$experimentRoot/selection-seal.json"
& $servicePython -B -X utf8 scripts/compare_tableformer_geometry.py replay --manifest "$experimentRoot/dataset/inputs/test.json" --candidates "$experimentRoot/inference/test" --tableformer "$experimentRoot/inference/test/tableformer" --output "$experimentRoot/replay/test" --test-code-lock "$experimentRoot/selection-seal.json"
# 本次在这里执行下节的网格协议开发/验证/封存，再打开下面的测试评分。
& $servicePython -B -X utf8 -c "import sys,runpy; sys.path.insert(0,'scripts'); runpy.run_path('scripts/report_geometry_v2.py',run_name='__main__')" --annotations "$experimentRoot/dataset/sealed/test.annotations.json" --replay "$experimentRoot/replay/test" --output "$experimentRoot/reports/test.json"
& $servicePython -B -X utf8 scripts/summarize_tableformer_experiment.py --report "$experimentRoot/reports/test.json" --replay "$experimentRoot/replay/test" --inference "$experimentRoot/inference/test" --tableformer "$experimentRoot/inference/test/tableformer" --annotations "$experimentRoot/dataset/sealed/test.annotations.json" --output "$experimentRoot/reports/test.diagnostics.json"
```

## 独立网格口径修订

验证集边界复核发现官方 PDF annotations 保存发生在结构标签 dilation 之前。`audit_tableformer_grid_labels.py` 按固定上游 commit `0bb3ab71c4ec2a8b6c656c67af40eeaf863f306e` 的 `process_pubmed.py` 重建非旋转网格：相邻行/列边缘的中点作为共同边界，跨格以行列矩形并集求交，最后应用原仿射变换。不参考模型输出或 OCR 框。

这是**测试推理完成后、测试评分前**的补充协议修订。原封存标签、预测和评分器保持不动；补充标签放入独立目录。原主指标与补充指标不能混成单一“从一开始预注册”的结论。

实际插入的执行顺序如下。两个新测试评分均在 `grid-selection-seal.json` 生成后执行；seal 拒绝在原测试或网格测试报告已存在时补签。

```powershell
foreach ($splitName in @('development', 'validation')) {
    & $servicePython -B -X utf8 scripts/audit_tableformer_grid_labels.py derive --manifest "$oldDataset/inputs/$splitName.json" --annotations "$oldDataset/annotations/$splitName.annotations.json" --canonical "$oldDataset/canonical" --output "$experimentRoot/grid-labels/$splitName"
    & $servicePython -B -X utf8 -c "import sys,runpy; sys.path.insert(0,'scripts'); runpy.run_path('scripts/report_geometry_v2.py',run_name='__main__')" --annotations "$experimentRoot/grid-labels/$splitName/annotations/$splitName.annotations.json" --replay "$experimentRoot/replay/$splitName" --output "$experimentRoot/reports/$splitName.grid.json"
}
& $servicePython -B -X utf8 scripts/audit_tableformer_grid_labels.py seal --development-report "$experimentRoot/reports/development.grid.json" --validation-report "$experimentRoot/reports/validation.grid.json" --output "$experimentRoot/grid-selection-seal.json"
& $servicePython -B -X utf8 scripts/audit_tableformer_grid_labels.py derive --manifest "$experimentRoot/dataset/inputs/test.json" --annotations "$experimentRoot/dataset/sealed/test.annotations.json" --canonical "$experimentRoot/dataset/canonical" --output "$experimentRoot/grid-labels/test" --test-seal "$experimentRoot/grid-selection-seal.json"
& $servicePython -B -X utf8 -c "import sys,runpy; sys.path.insert(0,'scripts'); runpy.run_path('scripts/report_geometry_v2.py',run_name='__main__')" --annotations "$experimentRoot/grid-labels/test/annotations/test.annotations.json" --replay "$experimentRoot/replay/test" --output "$experimentRoot/reports/test.grid.json"
& $servicePython -B -X utf8 scripts/summarize_tableformer_experiment.py --report "$experimentRoot/reports/test.grid.json" --replay "$experimentRoot/replay/test" --inference "$experimentRoot/inference/test" --tableformer "$experimentRoot/inference/test/tableformer" --annotations "$experimentRoot/grid-labels/test/annotations/test.annotations.json" --output "$experimentRoot/reports/test.grid.diagnostics.json"
```

全 260 表通过非旋转、有序行列、唯一跨度身份及图像边界校验；不支持的标签会拒绝派生，不静默退回另一种口径。开发/验证派生结果连同源码 SHA 纳入独立 grid seal，测试派生标签保留原框并另建 split-lock。补充重建使用浮点边界，没有通过整数舍入或阈值调整改善分数。

## 评分与证据边界

完整格成功须目标身份正确、非歧义且真实 polygon IoU≥0.5；覆盖率=C/全部目标N，错位率=W/提供完整格框A，提供框达标率=C/A。边界 IoU 不达标、身份歧义和错位分别统计；在线对应器接受不代表独立评分成功。遗漏、模型失败、超时不移出分母。原上线门槛保持覆盖≥90%、错位≤1%、提供框达标≥95%。

独立文档 bootstrap 区间沿用 1,000 次；同目标的 TableFormer 对基线差值另做固定 seed 的 2,000 次成对文档 bootstrap。raw oracle 仅诊断单个原始矩形与 GT 的一一分配能力，忽略文字、跨度和候选并集，不是实际精度或所有未来算法的绝对上限。

这是 PubTables 渲染裁剪表、共享 reference edit 的几何控制实验，不是整套 Docling 与 DeloitteOCR 的端到端比较。未新增整页多表、原生 PDF、物理扫描、中文、照片或跨页测试；正确定位覆盖率不能直接解释为文档识别准确率。
