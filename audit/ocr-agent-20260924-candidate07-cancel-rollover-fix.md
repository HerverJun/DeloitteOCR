# candidate-07 正式轨失败诊断与有界修复回执

日期：2026-09-24（Asia/Hong_Kong）

原始轨：`E:\OCR Agent 测试\mixed-candidate07-formal-01`；仅以 SQLite `mode=ro` 及文本只读检查，未改动原始轨、候选包或封存题，未启动 GPU/OCR。修复与复验只在当前源码和临时测试工作区进行。

## 原始故障

- `mixed-stability.json` 为 `failed`；控制 run `034265c0de1b48869b3fc3cccef62981`（`READ-mixed_two_ui-2001`）于 `2026-09-23T22:07:04.988117+00:00` 创建，`22:07:05.823830` 写入 `error` 事件（seq 431，`code=internal_error`，消息为“旧检查点有待恢复或未结副作用，暂不能安全换段；请核对旧任务后新建会话”），`22:07:05.835361` 写入 `failed`（seq 432）。该 run 的模型请求、调用、operation、job 链接、决定、resume intent、inbox、产物全为 0。
- 控制 session `847e5aa41c394dc2add2c313b73cff5c` 的旧 thread `bb117ad83ddd41839caacbd57b43992a` 有 98 个 checkpoint，超过源码阈值 96；checkpoint payload 1,527,417 B，低于 8 MiB。预调用换段被拒，因此四小时阶段尚未开始。
- 该 session 的四个已取消 `CANCEL-mixed_two_ui-*` run 各残留一个 `ask_user` 的 `validated` 调用（`result_ref=NULL`，`operation_id=NULL`）和一个 `pending` clarification decision。最近一个是 `CANCEL-mixed_two_ui-2000`。历史模型请求 98 个均为 `received`；无未结 operation/inbox，四个 resume intent 均 `applied`。故障是取消澄清调用未收口，继而触发原本正确且保守的 rollover 安全门；不是零 job 修复的回归，亦无证据表明本次失败 run 造成业务副作用。

## 修复范围

仅更改 `src/ocr_workbench/agent/runtime.py` 中 `_cancel`：在 run 取消、代次递增的同一数据库事务内，将该 run 的 pending decision 标为 `obsolete`，并仅把无 operation、无结果且 `validated` 的 `ask_user` 调用结算为 `cancelled`（`required_action=none`），发布一次 `tool_finished` 事件。其余工具、operation、未知模型回包及 resume intent 的未结记录仍保留，rollover 安全门没有放宽。取消请求的已有幂等路径不重复产生事件；已经取消的 run 不接受以最新代次再次取消而递增代次。

新增 `tests/test_agent_checkpoint_rollover.py` 回归：真实图进入 `waiting_user`、并发重放同一取消、以已取消 run 的最新代次重取消遭拒、旧决定回复遭拒、后继 run 在低阈值下换段成功；保留真实未结 operation 阻断换段测试。

## 验证

使用便携 Python `build/ocr-agent-20260921-langgraph/langgraph-probe/运行时 中文/service/python.exe`，从当前 `src`/`tests` 导入，以 `unittest` 在临时工作区执行：

- 最终代码下的 `test_agent_checkpoint_rollover`、`test_agent_mutations`、`test_agent_inbox_api`、`test_agent_jobs`：21 tests，OK（其中模拟删除故障和数据库暂不可用测试按预期记录异常日志）。

原始正式轨维持失败原貌；此修复仅对后续新轨有效，需要用新的独立输出目录复验，不可把原始失败轨当作通过。
