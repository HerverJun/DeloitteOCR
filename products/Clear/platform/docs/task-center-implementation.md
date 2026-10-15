# 多人任务中心实现与验收记录

日期：2026-10-15。此版本为隔离候选，正式 Clear 0.10.0 的 `current.json` 与业务 DATA 未切换。

## 实现范围

中央库采用 PostgreSQL 17、SQLAlchemy 2、Alembic，迁移版本为 `0004_search_and_ai_filters`。原生 OCR、分类及平台领域 SQLite 保留。主任务、轮次、步骤、事件、AI 调用、成果、来源映射与持久调度分别存储；事件和审计表对运行账号只允许追加与读取。内部 ID 沿用项目的前缀 UUID 字符串，任务编码独立生成。缺失的历史时间、用户、模型与用量保留空值。

一次提交在同一中央事务中写入任务、首轮执行、初始事件和命令。请求键绑定完整参数的 HMAC 指纹。领取采用 `SKIP LOCKED`、租约和代际校验；心跳 10 秒、租约 60 秒，重任务默认并发 1，允许配置 1–4。导入、导出的 IO 命令单独准入。执行器独立于门户，门户重启复用已核验工作区、数据库及源码版本的执行器。

原生业务事务追加 SQLite outbox；中央端按来源与代际去重。原生接收日志和本机发送回执用于恢复，结果不明时仅查询已有接收记录，不重复 POST。人工重试创建新轮次，内部重试保留步骤尝试。OCR 与 PDF 阶段失败重试创建新原生 ID，保留旧结果。Data 重试要求原 session version 未改变；变更版本应新建任务。取消请求先进入取消中，执行端确认后结束。

任务列表仅查询中央库。表格含任务编码、名称、工具、AI、状态、项目、提交人、输入大小、创建时间、耗时、说明、进度及操作；默认 50 条，使用稳定游标，支持组合筛选。详情提供输入、执行轮次、步骤、事件、子任务、模型用量和成果。事件 SSE 可按持久序号续读。说明、错误和大集合按需展开或继续加载。

账号由管理员创建，密码使用 Argon2id；会话 token 在数据库内保存哈希，Cookie 为 HttpOnly，HTTPS 下启用 Secure，写请求校验 Origin 与 CSRF。项目角色 `owner/editor/viewer` 对应项目管理员、编辑者、查看者。编辑者控制自己的任务，项目管理员控制本项目，平台管理员管理账号和原生历史关联。原生入口经同源鉴权网关检查资源归属，服务器持有原生 token。

AI 连接由管理员在既有 Agent 配置页维护。当前连接版本可用于新任务，历史版本仅用于追溯；换凭据、更新或删除连接均不会由普通成员恢复旧全局配置。Agent 断点重试遇到连接 revision 变化会拒绝。OCR 原生视觉服务仍使用原生配置及实际调用记录。无真实供应商凭据，本轮未执行外部 AI 请求。

## 接入和验证口径

| 入口 | 已接入的持久记录 | 验证范围 |
|---|---|---|
| 平台资料、Data | 上传、导入、处理、导出与重试 | 真实 CSV 失败→恢复→重试；10 万行 CSV 门户重启 |
| 原生 OCR | 图片/PDF 导入、识别、融合、页面处理、视觉/结构/几何审校、导出 | 真实 PDF 上传、故意失败、独立阶段重试、PNG 下载；适配器回归 |
| 分类 | 导入、数据集版本、训练、预测、评估批次、导出 | 路由识别、来源、权限、幂等与 outbox 回归；未重新执行全部模型训练 |
| 取证 | 导入、分析、工具重试、报告归集和导出 | CPU metadata/frequency 真实执行、5 个成果、哈希下载及 ZIP |
| 人脸、音频、印章、CAT-Net | 原生分析、步骤尝试、成果及控制 | 接口/状态/权限/恢复回归；未重做全部模型资格验收 |
| Agent、批次 | 主任务、子任务、轮次、步骤、来源、模型调用、归集 | 适配器与恢复回归；已固定选择资料自动记录，未指定范围不虚构输入大小 |

登记规则为显式白名单，见 `tasks/registry.py`、`tasks/native.py`，新增执行接口必须扩充登记和契约。管理类修改进入审计日志。未知未来接口不自动宣称已覆盖。

## 验收证据

原始报告位于 `F:\OCR\output\task-history-multiuser-20261015\evidence`，最终数字见同任务根的交付报告。

- `backend-sealed.log`：任务、契约、项目与投影回归；不等同于全部历史后端测试。
- `frontend-current-final.log`：23 个文件、160 项通过；`frontend-final-typecheck.log` 与 `frontend-final-build.log` 为构建证据。jsdom 存在伪元素未实现提示，Vite 提示主 chunk 大于 500 kB。
- `portal-restart.json`：同执行器完成 100,000 行、8 列、3,611,931 bytes CSV，仅一轮执行。终端 Ctrl+C 会结束工具所管进程树；正式验证使用仅结束门户 PID。
- `million-task-benchmark.json`：独立数据库 100 万任务＋100 万轮次，20 个认证用户，latest/filtered/search 各 100 次请求，完成后删除测试库。
- `live-data-final.json`、`live-forensics.json`、`live-pdf-stages.json`：真实 CPU 流程。
- 最终备份与恢复报告包含文件哈希、登录读回、成果下载及恢复目录真实执行。

本机最新压测的 latest/filtered/search P95 分别为 137.87/81.67/76.32 ms；数据量为 1,000,000 主任务和 1,000,000 执行轮次。它验证本机查询范围，不代表目标服务器及所有筛选组合。

含成果的一致性备份已验证 169 个文件，恢复到新的 F 盘目录和独立数据库。演练发现并修复中断进程留下的 SQLite 热日志：排空和全局写屏障内允许 SQLite 完成回滚，再生成备份；热日志测试确认未提交行不会进入副本。旧合成演练的三个来源映射按原生已存 generation 修复，保留原事件、原状态及审计记录；同步积压归零。

恢复后的成果下载演练还修正了 PostgreSQL `LIKE` 对 Windows 反斜杠的转义问题。成果引用按完整目录边界搬移，数据库测试覆盖两种分隔符及相似名称目录；旧副本保留，新目录重新恢复后再验收下载。

本轮保留先前失败证据，包括缺索引压测与终端进程树中断，不用后续通过结果覆盖事实。全部数据是本任务合成资料。

## 尚未完成的生产资格

内网正式域名证书、独立目标服务器、全 GPU 模型和真实外部 AI 尚未验收。历史未归属任务只有管理员可见，暂未提供完整的批量归属整理 UI。单服务器只支持一个 canonical 原生数据根；多执行主机注册不在本候选范围。Windows DPAPI 凭据跨账号/跨机器恢复后需重新配置。候选复用已登记的 F 盘模型与运行时，交付清单明确共享路径，不能视为完整独立离线介质。

## 参考实现

采用设计原则，没有引入整套调度平台：

- [Apache Airflow TaskInstance](https://github.com/apache/airflow/blob/42945d62b1f6ecbb8255b306c9becf7c2900dfef/airflow-core/src/airflow/models/taskinstance.py)：尝试与执行状态。
- [Prefect 运行/状态模型](https://github.com/PrefectHQ/prefect/blob/302beb37eb79cd6ba37fcaa9fa002c4429b45702/src/prefect/server/database/orm_models.py)：当前摘要与历史分离。
- [Dify WorkflowRun](https://github.com/langgenius/dify/blob/69629bd7b719ab958be2b8a83d6cf13a1dcdbc0c/api/models/workflow.py)：步骤、执行人、模型用量。
- [PostgreSQL SELECT/行锁](https://www.postgresql.org/docs/17/sql-select.html)：多个消费者领取持久队列。
