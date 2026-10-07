# 屏拍表格模拟回归

本工具生成、冻结和离线计分完整屏拍输入。公开英文表与中文合成业务表分层报告；当前集合是已有开发暴露史的模拟回归，不代表真实手机拍摄或首次盲测。

文件只保存到 `F:/OCR`。所有命令使用现有 F 盘 control runtime；不安装软件，不修改共享运行时的 `._pth`。Windows 字体仅从 `C:/Windows/Fonts` 只读读取。

## 建集

```powershell
$env:TEMP='F:\OCR\output\ocr-screen-benchmark-20261007\temp'
$env:TMP=$env:TEMP
$env:PYTHONDONTWRITEBYTECODE='1'
$py='F:\OCR\output\clear-automation-dev-20261002\program\bundles\ocr\runtimes\control\python.exe'
& $py -B -X utf8 F:/OCR/scripts/screen_benchmark/generate.py --output F:/OCR/output/ocr-screen-benchmark-20261007/dataset-v2
& $py -B -X utf8 F:/OCR/scripts/screen_benchmark/verify.py --dataset F:/OCR/output/ocr-screen-benchmark-20261007/dataset-v2 --output F:/OCR/output/ocr-screen-benchmark-20261007/evidence/dataset-v2-verification.json
```

`--pilot-only` 可先冻结 72 张筛选集。完整建集还生成 60 张公开验证、8 张来自未进入 pilot 的中文开发源表验证、240 张主验收、24 张挑战及重复/同名/损坏文件工程用例。源文件和历史清单只读；重复运行校验原冻结数据，内容不一致立即拒绝。更改生成规则时采用新输出目录。

中文模板为 8 开发族、4 保留验收族，不把换数字当新模板。合成业务语义使用现存 FX03；整屏 UI 添加后再施加屏幕采样/亮度/摩尔纹、整屏透视、模糊和 JPEG。每图记录来源哈希、随机种子、参数、矩阵、单元格位置、UI 区域及遮挡/可见掩码。遮挡、裁断、极小字保留原真值，并明确标记不进入可读字段分母；它们仍进入挑战风险统计。

`inputs.json` 只包含完整图片与哈希，不包含答案/目标框。`truth.json` 仅供离线计分。`freeze.json` 绑定清单、真值、源数、组数、生成器哈希。`gallery.jpg` 用于人工检查，不能以生成器知道答案推断所有退化像素均可读。

几何可见掩码标记屏幕投影覆盖与裁断边界；遮挡掩码独立标记弹窗，两者联合确定可見区域。逐格 `visibility` 已对两者及极小字进行保守分类。

## 实际识别

先整批采集真实 PP-DocLayoutV3 与 PP-OCRv6 上下文。使用已冻结源码，避免并行实现导致运行身份发生变化：

```powershell
& $py -B -X utf8 F:/OCR/scripts/screen_benchmark/context.py `
  --inputs F:/OCR/output/ocr-screen-benchmark-20261007/dataset-v2/pilot/inputs.json `
  --bundle F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr `
  --worker-source F:/OCR/output/ocr-screen-benchmark-20261007/source-snapshots/pilot-v2/src `
  --output F:/OCR/output/ocr-screen-benchmark-20261007/runs/pilot-v2-context
```

该阶段复用一组常驻上下文模型；先全部采集，再卸载，随后逐个运行主识别模型。输入只包含原图，不传人工/真值区域。保存每图原始版面和文字框、标准化证据、模型加载/推理/墙钟计时及哈希；失败、恢复和重试采用保留历史 attempt 的规则。

```powershell
& $py -B -X utf8 F:/OCR/scripts/screen_benchmark/run.py `
  --inputs F:/OCR/output/ocr-screen-benchmark-20261007/dataset-v2/pilot/inputs.json `
  --bundle F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr `
  --worker-source F:/OCR/output/ocr-screen-benchmark-20261007/source-snapshots/pilot-v2/src `
  --engine hunyuan --preprocess-module ocr_workbench.screen_imaging `
  --evidence-run F:/OCR/output/ocr-screen-benchmark-20261007/runs/pilot-v2-context `
  --output F:/OCR/output/ocr-screen-benchmark-20261007/runs/pilot-v2-hunyuan-context
```

Hunyuan、PaddleOCR-VL、GLM 分开顺序执行。runner 首位加载指定新源码，并将缓存、临时、应用数据及子进程位置设置为本次 F 盘目录。不要并发抢 GPU。省略 `--preprocess-module` 时是完整原图基线，单独报告；产品主成绩使用同一自动处理函数。

`--evidence-run` 验证上下文的输入/源码/配置身份、每图版本/尺寸、原始输出和标准化证据哈希，再调用 `prepare_screen_image(image, output, evidence=actual_context, perspective=True)`。上下文缺失或失败明确记为该图失败，不悄悄跳过定位。省略它则是仅像素预处理基线，不能冒作真实上下文路线。

普通输入使用 `primary_id`，检测到多个不重叠 `table_crop` 时逐区域识别并聚合全部真实表。每区域原始输出、输入变换和每图结果均保留；实际存在的表/单格/文字框映射回 EXIF 标准化原图，不按均匀网格制造不存在的框，不对文字作真值修补。

预处理开启时，首轮真实结果调用同一 `assess_screen_result`；启用上下文时传实际 PP-OCR 独立文字框，不传真值或参考文字。自动交付覆盖、其中错误与关键格错误单列；质量信号缺失明确记录 unavailable，不当作检查通过。

原模型进程常驻；每图保存原始结果、状态、计时与错误。重新运行同一命令跳过身份/哈希一致的成功项；默认失败也保留，`--retry-failed` 创建新 attempt 才重试。输入、源码、引擎配置或预处理版本变化要求新的输出目录，避免缓存混用。`--offset`/`--limit` 可作诊断；需要完整分母时使用完整输入清单计分。GPU 采样是设备总占用，包含其它应用，不能冒作模型进程峰值。

## 离线计分

```powershell
& $py -B -X utf8 F:/OCR/scripts/screen_benchmark/score.py `
  --truth F:/OCR/output/ocr-screen-benchmark-20261007/dataset-v2/pilot/truth.json `
  --runs F:/OCR/output/ocr-screen-benchmark-20261007/runs/pilot-hunyuan-prepared `
  --output F:/OCR/output/ocr-screen-benchmark-20261007/evidence/pilot-hunyuan-score.json
```

按整表匹配保留行列身份；禁止逐格重排来掩盖错列。仅 NFC/空白规范化，不修正负号、小数点、百分号、前导零或数字。文字、关键数字、跨度、漏表/漏格、额外格、已知 UI 入侵、标题/单位/表注保留和整图全部表正确分别计数。失败、未执行和截断输出全部留在整图分母。挑战集保留可见字段分母与风险识别统计。

报告按公开/中文、常规/困难、模板、挑战分层，给出原始文档/模板组汇总与组级 bootstrap 区间；同源变体不冒作独立业务样本。输出仅与冻结参考对照，标注争议不得在看到错误后删除。没有人工计时不报告节省人工时间。

真实输出对照与 Excel 读回：

```powershell
& $py -B -X utf8 F:/OCR/scripts/screen_benchmark/acceptance.py `
  --truth F:/OCR/output/ocr-screen-benchmark-20261007/dataset-v2/pilot/truth.json `
  --run hunyuan=F:/OCR/output/ocr-screen-benchmark-20261007/runs/pilot-hunyuan-prepared `
  --run paddlevl=F:/OCR/output/ocr-screen-benchmark-20261007/runs/pilot-paddlevl-prepared `
  --run glm=F:/OCR/output/ocr-screen-benchmark-20261007/runs/pilot-glm-prepared `
  --output F:/OCR/output/ocr-screen-benchmark-20261007/evidence/pilot-model-comparison.json
```

该报告用真实 `result.json` 计分，并独立打开真实引擎生成的 XLSX，与其原始 OCR 结果逐格/类型/合并关系比较；不把真值注入识别或导出来制造正确工作簿。正确读回只能证明导出忠实，OCR 准确率仍由独立真值评分决定。主批次 ZIP 文件名与输入映射属于产品导出验收，另列检查。尚在运行时报告明确列出 missing/running 与已完成快照，不把已完成样本冒作全量成绩。

回归检查：

```powershell
& $py -B -X utf8 -m pytest F:/OCR/tests/test_screen_benchmark.py -q -p no:cacheprovider --basetemp F:/OCR/output/ocr-screen-benchmark-20261007/temp/pytest-benchmark
```
