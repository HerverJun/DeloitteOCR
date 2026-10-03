# 多功能平台自动化开发契约规范

版本：1。该文档约束首版实现；T004 负责提供严格 schema、OpenAPI、正反例和生成的前端类型。此处的接口是待实现协议，不是现有两个项目已经具备的 API。

## 1. 通用约定

- 新平台 API 使用 `/api/platform/v1`；模块原 API 保留，适配器处理差异。
- JSON 默认拒绝未知的控制字段；整数不接受布尔值冒充；分页、字符串和批次数量有明确上限。
- 金额通过十进制字符串传输，schema 指明精度和单位；不经过 JavaScript Number 再保存。
- 时间使用 UTC ISO8601；ID 为不透明字符串，不编码绝对路径、用户名称或模型 Key。
- 列表返回 items、next_cursor、snapshot_revision。翻页固定同一快照，快照失效返回明确错误。
- 错误返回 code、message、retryable、request_id、details；details 不包含凭据或项目原文片段。
- 原始值、模型预测、人工确认和最终采用分别表示，字段命名不得互相混用。

## 2. 应用目录与启动

AppManifest 必填：schema_version、app_id、display_name、app_version、adapter_version、min_platform_version、data_schema_version、entry_mode、capabilities、input_types、output_types、resources、license_refs。

entry_mode 首版为 standalone_page。后续可加入 routed_page 或 embedded，但平台不得推断旧版已经支持。健康状态至少拆为 service、ui、cpu、gpu、models，每项取 ready/unavailable/disabled/unknown，并带原因。

manifest 仅声明能力；受信安装记录才保存 executable、working_directory、bundle_hash 和 allowed_arguments。上传的应用清单不能注册任意命令、端口代理或文件根目录。

启动回执包含 launch_id、app_id、instance_id、PID 与 creation_time、实际 base_url、健康握手 nonce、ownership、version。ownership 区分 owned 和 discovered：发现的用户服务只能读取状态，不能自动停止。

同 launch_id 重试查询原状态。若端口被抢占或握手应用身份错误，失败并保留诊断，不把任意同端口服务作为成功实例。

## 3. 工作空间与打开上下文

Workspace 字段：workspace_id、name、revision、created_at、updated_at。ModuleProjectLink 字段：workspace_id、app_id、module_project_id、link_id、role、revision。

一个工作空间可关联多个同应用项目；分类项目的类别集仍由分类应用拥有。同名不是同一项目，跨模块不同 ID 不能直接互用。

OpenContext 包含 app_id、instance_id、link_id、可选 asset_set_id、可选 artifact_id、nonce、expires_at 和 return_route。后端校验当前工作空间可访问这些对象。return_route 仅允许平台内部路径，不能是任意外部 URL。

凭据不放 query string、localStorage 导出、日志或截图。独立模块通过受控启动/握手取得上下文；既有认证不被一个不受信请求头替代。

## 4. 资产与资料集

Asset 表示资料来源身份：asset_id、workspace_id、source_id、original_name、media_type、storage_mode、blob_hash、size_bytes、revision、created_at。Blob 表示字节内容，哈希相同可以复用文件，来源登记不能合并。

storage_mode 为 managed 或 referenced。referenced 文件在登记与读取处理前核验内容身份，已变化返回 INPUT_CHANGED；消失返回 INPUT_MISSING。大小/mtime 只可用于筛选优化，不能替代内容哈希判断。

AssetSet 是不可变清单：asset_set_id、revision、manifest_hash、items。每个 item 有 item_id、asset_id、asset_revision、blob_hash、source_locator；同 asset 可在有明确目的时重复出现不同 item_id，覆盖统计按 item_id 计算。

文件访问由后端按资产 ID 解析，不接受前端提供任意磁盘路径。登记目录导入需在允许根下解析并检查链接/重解析点，避免通过相对路径逃逸。

## 5. 来源定位与成果

SourceLocator.kind 可为 file/page/table/cell/row/region。所有定位先绑定 asset_id、asset_revision 和 blob_hash；页码统一从 1 起，表/行/列索引约定由 schema 固定，不让前后端各自猜测。

坐标定位必须有 image_version_id、coordinate_space、transform_revision、precision。precision 为 exact/approximate/page_only/unknown；缺可靠证据时降级。旋转/裁剪后的框不能直接套回原图。

表格数据行有稳定 row_id，同时保存 source_sheet、source_row_number；匹配结果包含 left_row_ids、right_row_ids。合并/筛选后的行号不能覆盖原始来源行号。

Artifact 包含 artifact_id、workspace_id、job_ref、revision、media_type、blob_hash、size_bytes、input_manifest_hash、application_version、rule_or_model_version、created_at、availability、source_refs。

availability 为 available/expired/missing/corrupt。下载先核实身份；不可用返回明确错误，不能自动计算新文件冒充原版本。临时文件完成并验证后才发布可见的 Artifact。

## 6. 幂等交接

导入请求包含 request_id、target_link_id、asset_set_id、asset_set_revision、manifest_hash 和 import_options。接收方对规范化语义 payload 计算 hash，不把时间戳/随机 transport metadata 算作业务差异。

| 情况 | 必须行为 |
|---|---|
| 新请求 ID | 持久化接收意图与处理身份，再执行导入 |
| 相同 ID、相同 payload | 返回原 operation_id 和已有逐项状态 |
| 相同 ID、不同 payload | 409 IDEMPOTENCY_CONFLICT，不覆盖原操作 |
| 接收后响应丢失 | 发送方查询该请求状态，不能立即生成新 ID 重投 |
| 已导入部分条目后中断 | 保留映射，只继续未完成项 |
| 输入版本变化 | INPUT_CHANGED/REVISION_CONFLICT，要求创建新操作 |

平台 outbox 状态：prepared → submitting → accepted → reconciling → completed/partial/failed；submitting 时失联进入 unknown，随后查询目标，不把 unknown 直接转 failed 再新建。

请求状态查询在不存在时必须返回可区分的 NOT_FOUND；网络失败、服务未就绪和权限失败不能解释为目标未接收。跨 SQLite 不承诺原子提交或计算恰好一次。

## 7. 作业与控制

JobRef 字段：platform_job_id、app_id、instance_id、native_job_id、operation_id、input_manifest_hash、native_status、status、stage、counts、capabilities、last_event_seq、updated_at。

平台状态：queued、waiting_resource、running、pausing、paused、cancelling、cancelled、succeeded、partial、failed、unknown。原生状态保留用于诊断，映射规则逐模块有测试。

counts 至少包含 total、succeeded、failed、cancelled、pending。固定输入项总数不因失败项被删而改变；尚未完成时不得只用 succeeded+failed 解释所有 total。total 未知使用 null，不能伪造百分比。

能力按任务类型声明 cancel/pause/resume/retry_failed。当前分类训练不具备的断点恢复不能在平台出现可执行的 resume。cancel 表示请求停止；确认进程/队列终止后才进入终态。

事件具有 app instance、job ID、generation 和单调 sequence。重复事件去重，旧 generation 不能覆盖新恢复状态。断流后查询账本恢复；AI 对话文字不参与完成判定。

## 8. GPU 所有权

ResourceGrant 包含 resource_id、owner_instance_id、job_id、generation、granted_at、heartbeat_at、lease_until 和 managed_process_identity。

取得顺序：持久排队 → 确认无存活旧 owner → 发放 generation → 模块确认 → 加载模型。释放顺序：停止新执行 → 模块清理/退出 GPU 会话或进程 → 验证受管所有者已释放 → 撤销 grant。

心跳超时触发核查，不能直接发放第二份 grant。旧 owner 的提交和释放请求需核对 generation；协调器重启后必须核实登记进程，不以空内存状态认定 GPU 空闲。

引擎维护、实际训练、ORT session 缓存、导出验证和允许的 CLI 都列入入口清单。只控制 UI 按钮不符合契约。GPU 兼容模式必须明示仅允许一个旧应用独占，CPU 操作保持可用。

## 9. 数据工作台语义

输入包含原字面值与解析值，两者分别保存。DECIMAL 的 precision/scale 由明确列规则决定；溢出返回错误，不能转成浮点继续。日期规则显式保存格式与时区，歧义日期进入待确认。

第一版算子：trim、rename_column、map_values、parse_decimal、parse_date、filter、detect_duplicates。每步有 step_id、operator_version、parameters、input_revision、output_revision、input_count、output_count、error_count。未来新增算子通过版本化注册，不允许任意 SQL/脚本从前端下发。

精确比对保存匹配键、类型规范化、null 策略和金额容差。默认 null 不等于空字符串或零；重复键不能自动变成多对多笛卡尔积。结果分类为 equal、different、left_only、right_only、ambiguous，附每类明细和输入覆盖计数。

公式处理区分 literal、formula、cached_value、cache_status。未取得可信缓存时不计算任意 Excel 公式，不把公式字符串或零当作数值结果。导出时原始文本与可执行公式分开选择，避免公式注入。

逻辑确定性依据固定输入、算子版本、参数和排序规则；XLSX ZIP 时间戳变化不直接代表逻辑结果不同，导出读回比较字段/类型/合并及来源，而字节哈希用于固定单次产物身份。

## 10. 错误代码和重试矩阵

| code | 是否自动重试 | 动作 |
|---|---|---|
| APP_UNAVAILABLE | 只可有界重试健康查询 | 给出缺包/进程退出原因 |
| AUTH_EXPIRED / CONTEXT_EXPIRED | 否 | 重新取得受控上下文 |
| REVISION_CONFLICT / INPUT_CHANGED | 否 | 保留草稿，重新确认输入并创建新操作 |
| INPUT_MISSING / ARTIFACT_MISSING | 否 | 修复引用或明确重新生成 |
| IDEMPOTENCY_CONFLICT | 否 | 查原请求，不覆盖 payload |
| RESOURCE_BUSY | 进入队列 | 等资源事件，避免轮询风暴 |
| SUBMISSION_UNKNOWN | 只重试状态查询 | 不盲目重发生成式请求 |
| CAPABILITY_UNSUPPORTED | 否 | 按能力调整流程，不显示假成功 |
| CANCEL_REQUESTED | 等实际终态 | 不提前释放 GPU |
| OUTPUT_INCOMPLETE | 否 | 展示覆盖清单，明确部分输出 |
| INTEGRITY_MISMATCH | 否 | 隔离产物并保存诊断 |
| VALIDATION_FAILED | 否 | 展示具体字段/规则失败 |

## 11. 契约验收与变更

T004 必须建立 manifest、context、asset_set、source_locator、job、artifact、handoff、resource_grant 和 error 的正反例。后端校验、OpenAPI 和前端类型使用同一版本来源。

新增可选展示字段可作为兼容增量；改变状态含义、金额类型、定位基准、幂等行为或权限语义属于破坏性变更。更新 schema_version 和适配器兼容矩阵，旧版本拒绝无法理解的写入请求。

合约变化先增加失败反例并记录 ADR，再修改提供者和消费者，最后跑契约与受影响集成。不要先让两个工作台临时各实现不同字段，再依赖前端猜测修补。
