# 开发计划逐项验收矩阵

当前审计对象：0.8.0rc2、策略 v5。依据 2026-09-12 删除人工验收条款后的 `DeloitteOCR-多引擎融合与快速校对开发计划.md`。当前范围的开发、验证和候选交付已完成；状态「已验证」仅限该行证据覆盖范围，后续外部范围和「未证」项仍按事实记录。

## 前置、数据和实验（计划 §1–3）

| 要求 | 状态 | 权威证据与范围 |
|---|---|---|
| 等现有收尾结束、整理提交、干净后开发 | 已验证 | e3ae6c7 及实施记录；功能提交 9d4211e 在其后 |
| 单机离线、单 GPU 顺序 OCR、CPU 融合 | 已验证 | task_queue.py 的独立 FusionQueue；无 Adapter 构造回归；包内两模式启动证据 |
| 表格、印刷体、手写；默认保守、两档入口 | 已验证 | FusionLauncher.tsx、config/fusion-policy.json；前端/浏览器证据 |
| 准确率优先、完整所选引擎、不提前停止/动态追加 | 已验证 | attach_batch_fusion/claim 依赖终态及失败取消测试；现有 OCR 串行队列 |
| 票率非概率、不直接用原始 confidence 跨引擎平均 | 已验证 | choose/candidates；验证权重；原始分数与位置独立存证，canonical_edit 清理融合元数据 |
| 开源思路独立实现，复用代码需许可核查 | 已验证 | fusion.py 独立实现；既有 research 调研与许可证记录，本轮未导入所调研项目代码 |
| 200 样本、800 记录、原始输出与文件哈希 | 已验证 | data/manifest.json，796 成功原始输出核验、4 失败保留 |
| 同文档变体分组、种子、分层比例与偏差 | 已验证 | data/split.json；88 组、种子 20260912；114/60/26，不拆组凑比例 |
| 来源追溯、重复检查、既往暴露 | 已验证，有限范围 | manifest/split/near-duplicates；确切哈希与保守来源分组，dHash 无候选仅属启发式 |
| 验证/原留出表格结构、合并、空值、数字标注 | 已复核，歧义保留 | annotation-review.json 的 16 张原图核对；原标注另存、修订台账可重放；没有独立第二读者 |
| 开发固定可部署基准，不用逐样本事后最优 | 已验证 | evaluation-v5/baselines.json；印刷体/表格 Hunyuan，手写 GLM |
| 固定基准、等权、加权、基准替换及单引擎同口径比较 | 已验证 | 每类 36 配置的 *-validation.json，holdout.json 的 single_engines；同版本原始输出和参考 |
| 冻结代码/策略/数据哈希，再回放 | 已验证 | v1–v5 各自 freeze.json、策略副本、精确 frozen-code；Git 属性保护字节 |
| 新独立留出结论 | 未证，按计划明确回顾性 | 所有数据有既往暴露，继续分析原留出后未重新声称独立；需后续新数据 |

## 算法与策略（计划 §4–5）

| 要求 | 状态 | 权威证据与范围 |
|---|---|---|
| 同图/处理版本/明确批次、任务/模型包/参数/指纹 | 已验证 | validate_sources、enqueue_fusion、冻结原始 snapshot；版本/批次拒绝及预处理绑定回归 |
| 同源仅一票、重试不加票、修订/融合不能回投 | 已验证 | 来源去重/不同原始输出拒绝测试，save 后来源不变测试 |
| 失败/取消/不支持/未匹配与空串区分 | 已验证 | 每单位 source_states、coverage、candidates；依赖失败与空值回归 |
| 预期覆盖、有效支持、分母与最低条件显式 | 已验证 | choose 输出分母、coverage；部署策略内 denominator/minimum_*；不剔除失败造满票 |
| 共同错误和非独立来源解释 | 已验证，计分范围有限 | fusion_metrics.py 和回放共同错误指标；仅可比参考单位计分，未匹配不推定正确 |
| PP-OCRv6 不投结构票、不造均匀单元格坐标 | 已验证 | STRUCTURAL_ENGINES、source_states=unsupported、对应回归 |
| 多表身份：坐标/表头/上下文；顺序或同形不单独证明 | 已验证 | match_tables 双向唯一和 margin；多表重排、重复表头、同形不同表、唯一长正文锚点回归 |
| 完整合法单一结构骨架，不拼合并关系 | 已验证 | complete_structure、initial skeleton fallback；稀疏结构和完整替代回归 |
| 可靠单元格对应；缺行列/偏移/合并冲突整表复核 | 已验证 | reliable_cells、拓扑和行列锚点；相关结构回归 |
| 字符串保真、数字整字段、平票保留、证据可追溯 | 已验证 | choose/field_kind、长编号四格式导出与空值/负号/日期测试；无语言模型补写 |
| 先真实文字区域关联，再有界行/片段 | 已验证（rc2 补齐） | text_regions/text_source_alignment；唯一字面跨度、真实 IoU 双向唯一、区域内偏移回归 |
| 区域/行换序、独有插删、未匹配及资源上限不静默丢失 | 已验证 | region_order_changed/region_unmatched/resource_limit 及文字插删/超限回归；无几何时有界阅读顺序降级 |
| 普通文字可组合有来源片段，数字边界不清则保留 | 已验证 | 片段来源偏移和完整数字候选测试；整条数字行保留字符串 |
| 文本与表格阅读顺序一致、混排不猜测 | 已验证，保守范围 | canonical_edit 维护原文表格 source offsets；复杂混排整文待核，未实现推测性多栏重排 |
| 单元格/真实区域/全图三级定位和上下文缩放 | 已验证 | valid_polygon/location、版本不匹配/越界回归；ImageCanvas 与浏览器放大场景 |
| 定位注明实际来源，非原引擎分数冒充融合置信度 | 已验证（rc2 修复） | 跨来源 cell/region 的 result ID/engine；完整候选与手工采用后 confidence/polygon 清理测试 |
| 三候选算法、两模式词典序选参、各类别分别决定开放 | 已验证 | evaluate_fusion.experiments/freeze，参数与逐类报告；所有类别当前仅建议 |
| 策略版本/类别/基准/算法/权重/条件/限制/哈希 | 已验证 | config/fusion-policy.json 和 freeze.json；对象指纹与文件字节哈希口径在使用说明区分 |

## 工作台、持久化与交付（计划 §6–9）

| 要求 | 状态 | 权威证据与范围 |
|---|---|---|
| 融合入口复用兼容同批次或完整识别后融合 | 已验证 | FusionLauncher、create_fusion、attach_batch_fusion；CPU 与批次回归 |
| 新结果预览、显式采用、再次融合不盖人工修订 | 已验证 | selection/revision 检查；浏览器预览禁止提交、显式采用、冻结来源测试 |
| 候选/来源/理由/定位级别、分页与状态/类别筛选 | 已验证 | review_issues API、QuickReview、长候选表分页；前后端回归 |
| 采用/保留/手改/疑问/上一下一；保存成功才继续 | 已验证 | decide_issue 原子事务；跳过、保存失败/丢响应/疑问浏览器证据 |
| 重开恢复位置、全部处理后仍需人工确认 | 已验证 | fusion_progress、SQLite 重开、确认绑定结果/版本/revision |
| CPU 任务、终态依赖、取消/失败/幂等恢复、零模型加载 | 已验证 | FusionQueue、取消/重试/预处理回归；500 图突然退出73后显式恢复 |
| 仅校对模式可纯融合，需要新 OCR 则受限制 | 已验证 | service 路由限制、包内两模式 CPU 融合 smoke |
| 输入/策略/父结果指纹冻结，原始/草稿/修订/决策分别保留 | 已验证 | fusion_inputs/results/edits/fusion_decisions 和原始修订不改 snapshot 测试 |
| 表格/文字/Markdown 同规范结构，更新 offsets/hash | 已验证 | canonical_edit、保存和再导出回归，整表候选与插入保真 |
| TXT/MD/JSON/XLSX 内容与来源来自同一保存快照 | 已验证 | exporting.capture_snapshot 单读事务，四格式来源/长数字回归；ZIP CRC/hash 另验 |
| 导出未处理/疑问/过期/未确认、仅确认筛选 | 已验证 | review_summary 与 confirmed_only 目标检查；既有/融合导出测试 |
| 串行草稿与决策、revision/basis 校验、丢响应原请求重试 | 已验证 | useEditor/editorRecovery、decide_issue；跨刷新丢响应与多窗口事务回归 |
| 保存失败/冲突不丢草稿、不加进度、不跳项；IME/切图项目 | 已验证 | 前端22测试、融合16浏览器场景和既有21场景；原子回滚/冲突测试 |
| 稳定疑点 ID、局部重核、结构变化受影响表失效 | 已验证 | fusion_id/basis、reconcile/relocate_text；无关格编辑保留、结构/撤销失效回归 |
| 确认随版本/采用/revision 失效，撤销不恢复旧确认 | 已验证 | schema 8 triggers、review target检查、history/reconcile 回归 |
| 迁移备份、统计、级联删除、搬迁和异常恢复 | 已验证 | Store._migrate、ProjectMaintenance、复制后来源/进度/导出、取消工作器未退禁止删除测试 |
| 后端为正式融合/疑点判定单一来源 | 已验证 | fusion.py/review_issues.py/fusion_store.py；前端仅展示/提交/保存状态 |
| 既有 Python/前端/构建与受影响 UI 回归 | 已验证 | 149 Python、22 前端和构建；engineering-v5/tested-components.json，前端字节与rc1一致，当前16融合场景重跑 |
| 500 张 CPU 容量、分页、持续编辑和项目恢复 | 已验证 | engineering-v5/capacity.json：500 完成、482 保存重开保留；工作器峰值/耗时分开 |
| 质量指标分类型/模式、净纠错/引错、关键字段不劣 | 回顾性已验证 | v5 holdout.json：表格408/565、+27/-0，关键字段不劣；文本等于固定基准；不能冒称独立验收 |
| 证据不足的类别仅建议，自动替换需充分质量收益证据 | 已验证 | 表格仅有暴露样本回顾性收益；印刷体/手写等于固定基准；两档所有类别均关闭自动候选替换 |
| 候选/源码/策略/使用/迁移/质量工程报告/已知限制 | 已验证 | E:\OCR-fusion-20260912\rc2 的实际 ZIP/回执；docs 下说明与报告；源码逐 Git blob 验证 |
| 策略回退仅新任务、历史不重算、普通入口保留 | 已验证 | 任务策略/来源 snapshot、load_policy 创建时读取；普通校对和单引擎路径保留 |
| 干净 Windows、实际 A4000、内网真实集 | 后续外部范围，未验 | 计划 §9 单列；当前主机4070 Ti SUPER结果不能替代 |

本轮完成依据为计划 §8.1 的公开数据质量评测、§8.2 的工程回归及 §9 的候选交付回执。当前没有人工效率、最终漏错或独立留出结论；公开数据不足以证明某类收益时，该类继续仅建议。
