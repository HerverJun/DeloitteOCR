# 自动化 v2 冻结约定

共同传输 envelope 仍是 schema_version 1.0；自动化关系/写语义使用 automation_version 2.0。
旧 v1 只读批次可迁移为 revision 1；旧未携带 expected_revision、request_id 和本机 actor 的处置请求拒绝，不自动补最新版本。
actor.source=local 表示本机操作身份及手填显示名，不表示组织认证。

- 行级索引固定 result_identity 与 ordinal。原结果数、左右行数、应处置数、已索引数及扫描数分开记录；pending + confirmed + deferred = indexed_items。采用修订属于 confirmed，并保留独立采用关系和事件。
- 处置携带用户所见 batch expected_revision。相同 request_id/规范化 payload 返回原回执，异内容返回 409。检查请求重放先于 revision CAS；首次写入不能静默读最新 revision 重试。
- adopt 每次指定一个 item 和明确 native_revision。OCR 还核对 result/version、图像原件、项目及实例；分类核对原 job/item/project/instance，采用原生人工类别，禁止旧预测覆盖新类别。
- 原生桥接口支持带 operation_id 的条件操作和 operation(operation_id) 核对。平台先保存 intent，再调用原生，最后保存平台 committed 回执。原生成功但平台未记账的恢复查询同一 operation_id；不可核对时保留 needs_verification，禁止无身份重发。
- 派发前持久化每个分段的操作 ID、完整目标与请求摘要；标记已开始后恢复只查询原操作，不重新 POST、不更换 ID。已经核对的回执和固定请求不能替换。
- 唯有首次原生条件 POST 明确返回 409、且没有任何分段成功时，允许记账为 committed/outcome=conflict 的无副作用终结回执。保留请求去重，不新增有效处置事件，不推进 batch revision，并释放操作保留位；重放仍返回原冲突。读请求冲突、响应丢失、部分成功及无法核实的执行不能据此终结。
- 分类后续处置的原生 CAS 使用该事项最近已提交平台操作回执中的版本，不静默刷新到原生最新版本。接受原建议 A 与采用人工修订 B 分开；已有 B 采用关系时不能把旧 A 当作本次确认。
- OCR 接受原建议与采用修订使用有稳定操作身份的条件固定版本回执。平台确认不等同原生独立 review/fusion 已完成。OCR 暂缓和重开仅写平台处置账本，保留原生 review 状态，不调用有版本选择副作用的 pin，也不伪造原生写入回执。
- 原生重启或冷备恢复后，当前受管运输实例 B 必须重新授权；原操作实例 A 从平台持久意图读取。历史回执仍核对 A、项目及完整输入身份，不改写为 B。OCR 查询使用 operation(operation_id, expected_instance_id=A)，首次采用仍要求当前实例与当前修订。
- OCR 修订版本为单调 revision，不等同撤销游标；旧版本快照不可变。迁移无法还原的旧中间版本明确不可用，不能伪造。
- 批次 revision 只在有语义的复核变化时推进；执行过程更新不会自动认可结果。final 要求执行 succeeded、完整索引、pending/deferred=0 且没有未完成操作。
- review-artifacts 固定 request_id、expected_revision、actor、kind。先在私有 staging 完成验证，再发布新 bundle/artifact ID；原包不可改。中间成果标 intermediate 并列出未解决事项。重开后旧成果标 outdated。
- 唯一复核发布入口是 POST /batches/{batch_id}/review-artifacts，201 返回固定原 ProcessingBatch 响应；相同请求重放不会切换到当前版本。GET /workspaces/{workspace_id}/artifacts/{bundle_id} 对 ready 复核包返回 ReviewBundle：完整覆盖计数、未解决总数及最多 30 条预览、后继总数及最多 100 个后继 ID。全量事项、采用快照、处置历史和来源进入不可变材料。current 是读取时相对当前完整批次状态的资格，既要求当前 delivery 指向本包，也要求固定业务快照仍一致；同 revision 的执行/覆盖继续变化也使旧中间成果 outdated。落后状态不改旧包字节。
- unresolved_count 包含全部未解决结果事项及技术事项，所以至少覆盖 coverage.pending + coverage.deferred；coverage 只守恒结果索引，不将归集失败等技术事项混入业务结果数。存在任何技术事项时也不能发布 final。

硬反例：100002 不交行异常全覆盖；混合重复/空键守恒；索引页提交后中断恢复；分类 A→B、OCR 1→2；两个窗口冲突；同请求重放/异内容拒绝；原生成功平台记账前中断；不可核实保持待核实；生成中断不发布；旧包保存身份不变；冷备保留新表及未完成操作。
