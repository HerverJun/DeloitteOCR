# 平台专用 Agent 评测接口

本目录包含模拟数据、独立评分／读回、CPU 契约检查，以及隔离的实际 Agent／模型／业务服务评测执行器。各命令的运行范围在下文分别说明；实际推理和受控恢复需要顺序安排 GPU，正式 current 不作为试验入口。所有落点必须在 F 盘，执行前 dot-source `F:/OCR/output/platform-specialized-agent-20261007/run-env.ps1`。

## 数据与独立性

最终数据根为 `F:/OCR/output/platform-specialized-agent-20261007/dataset/v3-002`。开发 60 图（36 公开、24 中文）；验证 40 图（24 公开、16 中文）；主保留 240 图（120 公开、120 中文，180 个源图、124 个文档/模板族）；挑战 24 图（6 个另外的中文源图、4 个保留模板族，遮挡/裁断/小字/无表各 6）。同一源图的变体与同族合成表相关，不能当作独立业务样本。

公开源来自本机 `build/table-matching-v2/dataset` 与 `build/tableformer-expanded-20260914/dataset` 的原 test，按源文档分组后重新分配。与旧 dataset-v2 全部 260 个公开文档及全部源图 SHA 无交集，开发/验证/主保留文档组无交集。新中文模板族为 4+4+4，源码保留完整文字、结构与业务含义定义，不只是给旧图片改名。

这些公开资料在历史其它 TableFormer 项目中已经存在，不声称整个模型历史首次盲测；同一屏幕 UI/相机退化渲染器刻意复用，也不声称新 UI 域独立。公开裁剪源无表外标题/表注真值，完整元信息专项主要覆盖合成表。只有开发/验证可用于选择策略，保留集须在源码/策略冻结后实际运行；一旦用于调参就失去独立验收资格。旧 240/24 仅登记为已知回归，不在本轮重复推理。

输入 `inputs.json` 仅有图片身份、哈希和尺寸，不含旧源 manifest 的 `fixed_edit`、格值或真值；`truth.json`、源注册表、遮挡/可见性掩码和冻结清单单独存放，不能传给主控或模型。每图真值保留原图坐标映射、合并跨度、可见/裁断/遮挡/小字不确定性，以及独立标题、单位和表注的完整原文、类别和顺序。

## F 盘 Python 与命令

只读复用 control runtime：`F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr/runtimes/control/python.exe`，Pillow 12.1.1、numpy 2.2.6、openpyxl 3.1.5。嵌入式 Python 不自动把脚本目录加到搜索路径，因此使用 bootstrap：

```powershell
. 'F:/OCR/output/platform-specialized-agent-20261007/run-env.ps1'
$evalPython = 'F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr/runtimes/control/python.exe'
$evalBootstrap = 'import runpy,sys;from pathlib import Path;p=Path(sys.argv[1]).resolve();sys.path.insert(0,str(p.parent));sys.argv=[str(p),*sys.argv[2:]];runpy.run_path(str(p),run_name="__main__")'
& $evalPython -B -c $evalBootstrap 'F:/OCR/scripts/specialized_agent_eval/verify.py' --dataset 'F:/OCR/output/platform-specialized-agent-20261007/dataset/v3-002' --output 'F:/OCR/output/platform-specialized-agent-20261007/evaluation/独立新记录.json'
```

命令和 API：

- `generate.py --output F:/.../新数据根`：按冻结源码确定性生成，已有不同字节拒绝覆盖。
- `verify.py --dataset F:/... --output F:/.../新记录.json`：逐核源图/标注/输入/真值/掩码/源码哈希、组隔离、格位与坐标；包括横向列标尺经缩放后的实际屏幕宽度。
- `test_contracts.py --output-root F:/.../隔离CPU样例 --receipt F:/.../新记录.json`：故意错误、漏报、旧 ZIP、数值授权及文本保护正反例。样例成绩只代表接口，不代表 OCR。
- `cpu_qualification.py --dataset F:/... --output-root F:/.../隔离CPU样例 --output F:/.../新记录.json`：验证全冻结集在无输出情况下仍保留全部分母；不产生任何 OCR 成绩。
- `native_bridge.py --native-run F:/实际原生运行 --inputs F:/.../inputs.json --output F:/.../observations.json`：调用已有 `screen_native_report.load_native_index` 校验真实任务/来源/结果/版本/策略/像素身份；收集阶段不读取真值。读取器需要既有原生运行回执布局；如新调度器布局变化，先补新的独立身份适配器，不能伪造旧回执或绕过校验。
- 当前原生 `detail.diagnostics` 由 `native_diagnostics.py` 另核真实 detail SHA、sample/原图/native input/result/revision、选定来源、当前表快照及 DB preparation/evidence，并加载冻结源码的纯 CPU 诊断函数重放静态疑点/定位/证据。只在重放得到已观测完整单元格几何时计 cell 提示；不读取自报 `reliable`，不提升自动交付或人工确认。旧源未提供诊断时保留明确缺失；当前源码 detail 本应生成却遗漏时拒绝。`test_native_diagnostics.py --output-root F:/.../隔离CPU样例 --receipt F:/.../新记录.json` 覆盖篡改、错来源/版本、伪格位、自报可靠与旧协议反例，成绩仅为工程门禁。
- `raw_bridge.py --run F:/实际原始引擎运行 --inputs F:/输入.json --worker-source F:/冻结worker源码 --output F:/.../observations.json`：核已有 `screen_benchmark/run.py` 的源码/配置/输入、记录/结果/准备图/context 身份。原始输出以实际文件 SHA 标识，明确不是数据库结果 ID；不把缺失的说明页元信息补填，不把分开 invocation 的时间相加冒作连续墙钟，自动交付覆盖为零。
- 原生运行被外部中止且没有终态 summary 时，显式加 `--partial-run --termination-receipt F:/真实owned停止回执.json`。必须匹配 `owned-benchmark-termination-v1` 与其绑定的 `screen-benchmark-external-termination-v1` 原现场 SHA、run/binding/input、PID/创建时刻/命令行/精确 UTC 停止时刻、所有权与退出观察、范围与已完成数量。桥不生成停止回执、不操作进程、不伪造原生 summary。已有 `failed` 原样保留错误/源记录，`running` 变为 `interrupted_unknown`，无原生记录为 `not_attempted`；成功项继续全链核结果 SHA。所有输入仍在分母，`complete_snapshot=false`，不能视为完整引擎对照、产品交付或默认路线选择依据。
- `analyze_probe.py --run-root F:/... --evaluation F:/... --worker-source F:/... --stem 唯一新stem [--partial-termination-receipt F:/真实停止回执.json]`：只对有完整原生 summary 的 Hunyuan/GLM 正常评分；明确的 Paddle partial 单列生命周期和完整分母输出覆盖，不参与配对精度或默认路线选择。
- `quality.py --truth F:/.../truth.json --observations F:/.../observations.json --output F:/.../新记录.json`：全量独立质量与分流统计；输入/真值冻值及实际输出图片身份必须匹配。
- `readback.py --zip F:/实际输出.zip --snapshot F:/权威当前结果快照.json --output F:/.../新记录.json`：按实际候选原文/跨度及十项来源核 Excel，不读取图片真值、不调用导出器。

输出均不可覆盖；修复后用新 stem，保留原失败。初版 `dataset/v3` 的列标尺注释曾用未缩放源宽度，已经保留，仅采用 `v3-002`；生成 001 的嵌入式导入失败、CPU 合约 001/002 的缺省布尔/零计数错误、005 的配对零计数字段遗漏均保留历史记录。CPU 合约 006 为 23 项通过，007 为新增 partial 协议后的 30 项通过，008 补充 source/input/config/failed/prepared 篡改反例后为 31 项通过，它们不是模型成绩。

## 质量接口

`quality.evaluate(truth_samples, records)` 与 `paired_report(before, after)` 为纯离线接口。`records` 每项包含：

```json
{
  "id": "与冻结输入完全一致",
  "image_sha256": "实际原图哈希",
  "status": "success",
  "result_id": "真实结果ID",
  "result_revision": 0,
  "candidate": {"original": {"engine": "实际引擎", "screen": {"business_metadata": []}}, "edited": {"tables": []}},
  "review_image": false,
  "review_regions": [{"scope": "cell", "table_index": 0, "row": 1, "column": 2, "reliable": true, "evidence_ref": "实际原图局部引用"}],
  "automatic_delivery": false,
  "human_confirmed": false
}
```

文件外层需有 `inputs_sha256`、`records` 和 `evidence_kind`（真实链为 `native_model_receipts`；工程样例必须另名）。未知/重复样本、错图哈希、成功结果缺 ID/revision 均拒绝；缺失/失败/取消/在途保留固定图片与格值分母。输入 `success` 指候选实际成功，不表示 OCR 正确或人工通过。

partial 新状态分别统计 `not_attempted_images` 与 `interrupted_unknown_images`，不混为技术识别失败，也不算无表识别正确。不可用输出在全量格分母中的零命中是可交付输出覆盖缺口，不是对未识别图片的准确率估计。泛化 `TimeoutError` 文案不能单独证明失败发生在冷加载或推理；明确阶段须原回执/日志另证，缺阶段保持空。

实际 raw 对照使用 `raw_engine_receipts`，只证明原始引擎实验；产品交付与人工状态仍须实际原生链核验。`select_probe.py` 已冻结 `evaluation/dev-probe-12-inputs.json` 与独立选择记录，离线 `dev-probe-12-scoring` 是相同 12 图的评分视图，不能当成全开发集或保留集成绩。模型只接收输入 JSON，不能接收评分目录。

报告给出表格/结构、可见关键格精确、全部参考诊断、元信息文字/类别/顺序、UI 标记与空间注入、需核图片/参考格、具体定位命中与误报、免人工覆盖和该子集残留错误。遮挡/裁断/小字不确定参考不强求猜值；非空输出记风险，免人工释放记错误。通用待复核只计图级复核，不能计具体格定位命中。局部问题必须显式 `reliable=true` 且有证据引用；坐标和引用的实际内容资格由 S03 原生定位核验负责，接口不会凭一个自报标记证明定位正确。

配对使用同一全量分母。既有关键错未经纠正从复核移出并免人工释放，也计 `new_silent_critical_release_errors`；净修复为正仍不能抵销该门禁，既有自动释放子集的残留错误也不能因没有新增错误而通过。缺失/失败项保留错误分母，但没有释放的文件不冒称静默自动交付错误。错误报告按原文 NFC、移除全部空白比较，不修数字/标点/负号字形；UI 标记/区域专项有已列边界，`annotated_content_exact` 只表示标注范围通过，包括合成元信息类别与顺序。

## Excel / 当前版本接口

`readback.check_bundle(zip_path, snapshot)` 的 snapshot 应从权威实际服务/数据库独立采集，不可从 ZIP manifest 反填。外层为 `batch_id`、`policy`、`items`；每项需要 `id`（样本映射）、`input_version_id`、`image_id`、`source_name`、原生版本的 `image_sha256`、`result_id`、`result_revision`、`status`、`review_state`、`human_confirmed`、实际 `candidate`。完整范围和逐输入唯一身份检查与图片真值评分分开。

默认所有非空格忠实文本，空格保空，前导零、16 位以上编号及公式样原文不会因导出转换。可选 `numeric_authorization` 包含明确确认和同一 input/result/revision，以及授权格及 `number`/`percent` 类别；独立 Decimal 解析和格式检查，不用 exporter 作 oracle。未确认、过期授权、编号/公式样转换拒绝。

忠实读回成功只代表候选未再次改变。新鲜度必须由 S05 在开始/结束采集逐输入版本向量（原图、选定候选、结果 revision、复核 epoch、导出选项）并写入成果 manifest。传 `snapshot.version_vector`、结束时 `snapshot.current_version_vector`，且两者及 manifest 向量一致才给 `freshness_status=passed/current_delivery_verified=true`；无向量时明确 `not_checked/false`。此接口不自行接入业务库，不能让旧快照验证成功冒充当前交付。

当前元信息工作表布局按平台已有导出协议核验。如果产品在 S03/S05 改动该协议，须显式版本化并同步独立 oracle 与正反例，不能静默删去元信息读回门禁。

## S07 现有工具实际样例

以下执行器均使用新 F 盘输出目录和既有 `run-env.ps1`；不调用主控，不替代真实 Agent 编排验收。

- `s07_classification.py prepare` 校验历史已发布分类模型包、实际数据库模型身份、固定测试输入和标签来源，生成无标签的模型输入；`predict-cpu` 由资源主代理顺序调用既有 CPU 推理原语。包运行时没有 distribution metadata 时记录实际已加载模块版本、路径与 SHA，不虚构缺库。历史 1 epoch 工程模型与两张输入仅用于模型加载/调用资格，不提供业务精度保证；预测与人工标注分开。
- `s07_native_followup.py` 使用平台 Python 执行真实已注册数据/审计工具；数据 worker 使用现有 data Python。显式 CSV/XLSX 输入按明确键、比较列、金额列与 Decimal 容差核对，读取真实结果，固定审计证据并读回五种导出文件 SHA。文件样例明确 `ocr_native_identity_verified=false`、`agent_orchestration_verified=false`。
- `s07_native_read.py` 核既有真实原生运行身份，完整稳定复制原生文件和 DB/WAL/SHM 到新的 F 盘目录，独立核 SQLite 完整性和原生表身份，再只在副本启动自有读取服务（`start_queue=False, agent_enabled=False`）。平台 Python 的原图像素身份检查只读加载同 ABI 的现有 F 盘 Pillow 包；不覆盖平台依赖。复制平台资料/项目/原生导入身份后调用实际 `results_describe/results_to_input`，合并或不完整结构继续按生产门禁拒绝，支持表再进入真实数据/证据工具。明确模拟参考文件必须绑定独立 truth/input 冻结和同图 SHA，只在既有推理结束后读取，不输入模型、不改 OCR 原文。

原生衔接样例仅用于 `Loci` 严格匹配、`Gene` 原文核对，不推断科学或金额单位。跟进耗时、工具数、模型与主控数单列，不能加回上游 Agent/模型耗时。成功还必须有自有服务退出、原运行全部文件 SHA/文件集未变的停止回执；不能直接在原证据目录调用 `create_app`。原生衔接支持不等于全平台结构支持，也不等于主控会自行规划跨工具任务。真实分类与 OCR→数据→证据 Agent 任务仍需在隔离范围内执行实际主控及原生服务，独立核工具选择、来源、业务规则、结果与轮数。

## 原批次受控恢复

`controlled_recovery.py` 只准备新 F 盘副本、绑定完整冻结源码及检查可启动条件。原 DB/WAL/SHM 先逐字节保存，只打开另建的可写副本并生成一致 SQLite 快照。目录树（包括原空 `blobs`）与文件分别登记；原任务/输入/模型身份、几何、attempt、已用成本与 deadline 保留，待续任务显式 paused，不启动服务或模型。

`controlled_recovery_followup.py` 封存已停止的恢复失败。它要求真实退出/Job 清理、原 batch 的 H26→H36 谱系、原 context60 和已关闭执行段证明；新准备从实际失败 runtime 继续，不能重新用 H26 现场构造任务。缺失的原空目录须有原现场证明，只在新 runtime 恢复；资产 ID、注册 revision 及固定集合不能重新生成。原/失败 SQLite 及其固定字节证据从不直接打开。

`controlled_recovery_restore_cpu.py` 在新的 SQLite/资料副本调用真实冻结平台的 `WorkspaceStore`、`AssetRegistry`、集合、handoff、批次服务和 Agent 构造器，读回原60资产，并核对缺目录/改 trusted root/缺源文件的拒绝。没有服务、主控或队列计算；CPU 通过仅代表恢复准备可初始化。`controlled_recovery_independent_readback.py` 再在新 SQLite 字节副本核任务、历史成本、图像/坐标、资产、原 pending Agent 与冻结源码。

`controlled_recovery_run.py` 的 `prepare` 仍为 CPU 阶段，`run` 必须取得 root 独占 GPU 槽授权。平台服务/原 Agent/资产在 native 服务和原 batch resume 前核实。真实 worker 采用冻结 OCR 源码，并保存实际 argv、阶段、HTTP延迟、自有 ProcessJob 清理、原任务连续性及当前 Excel/ZIP 读回。首个模型结果与首个实际可下载 Excel 分别计时；文件可读、候选忠实性、当前版本和内容准确性分开验证。恢复链含原失败与续作成本，不能作为同条件新路线性能对照。

已完成的 resume request ID 只会读回其旧回执。新执行副本的安全 pause 需要新的明确 resume 操作身份，同一操作未知结果则先 lookup 原身份，不能盲重发或重提 batch。`controlled_recovery_resume.py` 留存旧 action、当前 paused 状态、原 scope/deadline 和新的授权 intent；`test_controlled_recovery_resume_identity.py` 用真实冻结 Native SQLite 代码复现旧回执不再次执行、不同 intent 恢复与同 intent 幂等。标准整批 pause 会记录 `paused_at` 并在 resume 平移暂停时长；受控离线恢复保留原 `paused_at=None`，不给离线空档新增墙钟额度。
