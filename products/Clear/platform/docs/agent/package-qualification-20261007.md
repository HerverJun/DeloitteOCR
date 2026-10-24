# 专用 Agent 候选包的验收证据契约

`scripts/package_agent_candidate.py` 继续使用已有 F 盘路径检查、文件 SHA256、技能注册和数据读取兼容声明。本轮只增加只读验收索引，不创建第二套作业或状态服务，不切换正式 `current`。

`package(..., product_version='0.9.4', qualification_index=None)` 保留既有调用与版本默认值。本轮生成候选时明确传 `--product-version 0.9.5`。`--engineering` 允许缺少真实验收报告，但候选 README 和 `candidate.json` 明确记录工程状态。包含 `screen-batch-excel/SKILL.md` 的最终候选必须九项门槛全部通过，检查发生在创建候选目录之前。

`implemented_features` 表示代码提供的能力；`qualified_features` 只在完整验收通过后登记。旧 `readiness_features` 标签不再作为验收声明。CPU schema、接口和预检通过最多证明 `packaging_contract`，不能证明 OCR 识别质量、真实主控效率、浏览器可用性或整体可替代性。未包含新技能的历史来源仍保留旧打包调用方式，但没有本轮验收资格。

## 索引与来源绑定

`--qualification-index` 指向一个实际存在的 F 盘 JSON：

```json
{
  "format": "clear-agent-qualification-index-v1",
  "product_version": "0.9.5",
  "gates": {}
}
```

上例故意没有报告，结果为缺少九项门槛。实际 `gates` 的每个键应映射到 `{"path": "实际的 F 盘绝对 JSON 路径", "sha256": "该文件实际的完整 SHA256"}`，不能使用 `passed: true` 代替证据。索引不会执行评测，也不会生成或补造缺少的报告。

每项门槛报告的格式为 `clear-agent-qualification-gate-v1`，必须包含：

| 字段 | 内容 |
|---|---|
| `gate` | 下表中的确切门槛名 |
| `product_version` / `status` | 与索引一致的产品版本；验收成功为 `passed` |
| `tested_files` | 评测时保存的候选相对路径 → SHA256 完整清单 |
| `evidence` | 下表要求的原始 JSON 报告，按角色名登记绝对 F 路径和实际 SHA256 |
| `measurements` | 额外的测试范围声明；质量报告需要 `split: heldout`、`truth_used_by_controller: false` |

`qualification_inventory(source)` 返回 `tested_files` 的命名空间：`platform/...`、`ocr/...`、`classification/...`。它绑定各仓源码、运行配置、策略、技能、平台脚本以及三套前端的源码/构建/包和构建配置，忽略 Git、依赖目录、Python 缓存。新技能验收不能缺少运行源码、OCR 策略或三套前端源码。清单须在真实检查时保存；不得在检查结束后对当前内容重新做清单，以此冒充先前测试过的文件。

打包逐项核对完整清单与当前文件。实际 Agent 和恢复报告还会校验其原生源码及策略摘要；实际 Agent 报告的平台摘要沿用既有 `platform_inventory` 算法。候选封存有效索引、门槛报告与原始 JSON 的副本及来源 SHA；研究图像、真值、模型和缓存继续保留在评测目录。浏览器案例的证据文件也必须在 F 盘存在且匹配 SHA。

## 九项门槛

| 门槛 | `evidence` 角色 | 必须提供的事实 |
|---|---|---|
| `packaging_contract` | `verification` | `clear-agent-packaging-contract-v1`；`status: passed`、`cpu_only: true`、正数 `passed_checks`、`failed_checks: 0`，以及当前 `tested_inventory_sha256`。只证明 CPU 打包契约 |
| `agent_gpu_chain` | `run` | 既有 `screen-agent-actual-native-result-v1`；240 张真实主控/队列/EngineAdapter 完整链，未读真值、未注入回执/共享上下文，等待主控次数稳定，有首个实际结果/Excel及核实过的交付；源码/策略摘要与候选匹配 |
| `heldout_quality` | `dataset_qualification`、`quality`、`baseline_quality`、`challenge_quality`、`comparison`；按需 `human_timing` | 既有 `specialized-agent-eval-v1` 新保留数据资格：历史来源/中文模板及开发/验证/保留分组交集为零；主集和基线各 240、挑战 24 张的完整真实模型评分，绑定资格报告中的输入/真值 SHA。挑战与主集共享模板族的已知限制仍须披露 |
| `excel_readback` | `readback` | 既有读回报告：内容与当前版本均通过，240 个输入全部计入；工作簿和 `no_book` 合计为 240，逐项状态为 `passed` 或明确的 `no_book`，不能用 12 图读回冒充整批 |
| `native_recovery` | `recovery`、`prepared`、`run_policy` | 既有 `screen-native-eight-recovery-result-v1`；真实八图队列生命周期恢复、旧尝试保留、同批次及恢复请求幂等、不重复成果、所有自有 worker 与服务实际退出。准备回执绑定默认策略原字节 SHA；实际运行策略 SHA 与回执一致、JSON 内容与冻结默认相同，允许相同内容重新序列化。它不证明断电/OS 崩溃或冷备恢复 |
| `performance_comparison` | `comparison`、`baseline_run`、`candidate_run`；按需 `human_timing` | `clear-agent-performance-comparison-v1`；同 240 张、硬件、冷热态、主控配置及导出选项的实际配对报告，原始主控/模型回执保留。报告首 Excel、整批墙钟、轮数和 token，不把重叠成本相加 |
| `supported_cross_tool_tasks` | `tasks` | `clear-agent-cross-tool-qualification-v1`；真实主控调用生产工具，`published_model_classification` 与 `ocr_to_data_to_evidence` 两类任务通过，保留实际操作/成果 ID、结果读取及来源核实 |
| `cold_backup_restore` | `restore` | `clear-agent-screen-cold-restore-v1`；关闭源服务、真实屏拍业务数据冷备验证并恢复到新的 F 目录，完整交付版本向量和偏好相同、恢复后实际成果下载核实。消费者源码、实际 Python/导入模块和读取审计须绑定当前候选，旧数据范围已隔离。只有旧备份原语或队列恢复报告不合格 |
| `browser` | `browser` | `clear-agent-browser-qualification-v1`；真实浏览器交互通过，案例证据文件存在且 SHA 匹配。构建/组件测试不能替代 |

质量门槛独立计算自动覆盖：`automatic_images / images`，自动子集必须非空且无错误图片、关键残错、不确定图或自动/复核矛盾。配对净修复为正，并重新从逐图错误记录核对净修复计数；新增静默关键放行错误须为零。原来需要复核的未修复金额/编号错被移出复核也会独立拒绝，不能被其他格子的净修复抵消。必须实际缩小需核图片/格范围，或由独立人工计时证明减负；全送人审及仅覆盖率非零均不合格。主控完整链与主保留评分必须使用同一输入 SHA。

性能报告 `baseline` / `candidate` 需包含非负实际 `wall_seconds`、`first_excel_seconds`、`model_rounds`、`total_tokens`、`inputs_sha256`，以及相同的 `hardware`、`load_state`、`controller`、`export_options`。候选普通无追加批次需 `logical_controller_rounds <= 5`、`batch_tool_calls <= 8`，单列 `api_retries`；报告 `ordinary_batch: true`、`waiting_controller_count_stable: true`。默认模式改善首 Excel 或总时长即可；首产出改善但总成本上升时，需 `tradeoff.reason` 和 `tradeoff.adoption_scope` 给出具体理由与采用范围，不要求每项成本都下降。更准更慢的路线声明 `adoption_mode: accuracy`，提供原始 `quality_comparison` 证据，净修复为正且无新增静默关键放行错误，另保留具体取舍范围，不能暗中替换全批默认。`human_review_seconds` 没有独立计时就缺省/置空，不得从疑点数估算。提供时需对应 `clear-agent-human-review-timing-v1` 的 `independent_timing: true` 及实际 `baseline_seconds` / `candidate_seconds`。

冷恢复报告的 `source_version_vector` / `restored_version_vector` 要同时包含 `snapshot_id`、批次/项目、策略 SHA、完整逐项输入版本/结果 ID/revision/选定结果/人工复核 epoch 和导出选项；仅相同的一个 revision 数字不能通过。`source_preferences` / `restored_preferences` 需是实际非空的已保存偏好。

冷恢复还必须提供实际 `consumer_source_inventory`、`actual_invocation`、`actual_cold_source_isolation` 和两阶段 `actual_native_consumer_reads` 文件路径及 SHA。源码清单绑定实际执行前冻结的 platform/OCR 文件，逐文件核对当前候选与冻结副本；执行后补一份当前清单不能替代原始绑定。实际 argv 保留三种 Python 的路径/SHA，读取运行时中的已加载 native 模块必须来自同一冻结目录且匹配原清单，诊断版本为 `screen-diagnostics-v2`，队列和 Agent 关闭。

只隔离该次新建的 cold source，原推理与 S07 目录不移动；恢复消费时旧 cold source 根实际不存在，隔离后的目录仍存在。读取运行时须阻止原推理、S07、旧 cold source 和隔离目录。两阶段真实 JSONL open 审计要有当前冻结诊断模块的像素读取帧，所有读取均在本阶段的 native 数据根内，旧范围访问为零。打包封存这些 JSON/JSONL，不能凭几个布尔通过标记代替原文件与导入/读取绑定。历史 12 图冷恢复仅证明其工程范围；最终候选源码改动后须重新测试冻结候选，不能更新旧报告清单冒充复测。

浏览器 `cases` 必须覆盖 `bulk_import_progress`、`batch_pause_resume`、`grouped_review`、`precision_current_revision`、`per_image_excel_zip`、`preference_next_boundary`、`stale_delivery_warning`。每项为 `status: passed`，`evidence` 是至少一个实际 F 盘文件路径及 SHA 的列表。

报告生产者负责执行实际评测并保留失败与原始回执；只读打包检查不能重新证明图片真值、采样独立性或 JSON 中事实的真实性。不得把单元测试中的合成正例索引登记为真实验收。没有真实手机屏拍或人工计时证据时，只能报告模拟验收与所测采用范围，不能承诺业务准确率、免复核或旧 harness 倍速。
