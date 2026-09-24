# LangGraph resume 后、checkpoint 前强退：隔离验收回执（2026-09-24）

范围：合成 3×3 像素图片、临时业务 SQLite 与官方持久 SQLite saver；无 GPU、Edge、真实主控或封存题。仅新增 `tests/agent_fixtures/resume_fault_worker.py` 与 `tests/test_agent_checkpoint_faults.py` 中的一项故障注入测试。未修改产品运行时或导出文件。

故障点：子进程先建立真实 `waiting_jobs` interrupt/checkpoint，完成合成 stage；观察器调用 `Command(resume)`，`await_jobs` 中 `AgentStore.finish_call` 已提交 `result_ref` 和 `tool_finished` 后立即 `os._exit(92)`。图节点返回与后续 saver 提交尚未发生。新进程 `open()` 后断言图仍为 `step=0,cursor=0,next=await_jobs,interrupt.kind=jobs`，业务调用却已 finished；显式恢复后断言只存在一个 stage、一个 agent_call、一条原始 `tool_finished`（整行未变）、两个模型请求（原 step 0 与唯一后续 step 1），最终 completed。此故障点下未发现产品 bug；旧 checkpoint 重放使用了既存业务结果。

执行命令：

```powershell
& 'build/ocr-agent-20260921-langgraph/langgraph-probe/运行时 中文/service/python.exe' -B scripts/agent_eval/run_tests.py test_agent_checkpoint_faults.CheckpointFaultTests.test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once
& 'build/ocr-agent-20260921-langgraph/langgraph-probe/运行时 中文/service/python.exe' -B scripts/agent_eval/run_tests.py test_agent_jobs.JobTests.test_waiting_jobs_automatically_resumes_without_extra_model_poll test_agent_recovery.RecoveryTests.test_resume_receipt_reconciles_without_replaying_unknown_wakeup
```

最终原始输出（故障注入单测）：

```text
test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once (test_agent_checkpoint_faults.CheckpointFaultTests.test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once) ... ok

----------------------------------------------------------------------
Ran 1 test in 2.725s

OK
```

邻近回归原始输出：

```text
test_waiting_jobs_automatically_resumes_without_extra_model_poll (test_agent_jobs.JobTests.test_waiting_jobs_automatically_resumes_without_extra_model_poll) ... ok
test_resume_receipt_reconciles_without_replaying_unknown_wakeup (test_agent_recovery.RecoveryTests.test_resume_receipt_reconciles_without_replaying_unknown_wakeup) ... ok

----------------------------------------------------------------------
Ran 2 tests in 3.336s

OK
```

首次故障注入尝试失败的 traceback 保留（测试钩子误挂在另一个 `AgentStore` 实例，未到预定强退点；随后改为 `runtime.agent.finish_call`）：

```text
test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once (test_agent_checkpoint_faults.CheckpointFaultTests.test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once) ... ERROR

======================================================================
ERROR: test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once (test_agent_checkpoint_faults.CheckpointFaultTests.test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "asyncio\runners.py", line 118, in run
  File "asyncio\base_events.py", line 691, in run_until_complete
  File "C:\Users\A\Desktop\OCR\tests\test_agent_checkpoint_faults.py", line 37, in test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once
    process = await asyncio.to_thread(subprocess.run,
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "asyncio\threads.py", line 25, in to_thread
  File "concurrent\futures\thread.py", line 59, in run
  File "subprocess.py", line 550, in run
  File "subprocess.py", line 1209, in communicate
  File "subprocess.py", line 1630, in _communicate
subprocess.TimeoutExpired: Command '['C:\\Users\\A\\Desktop\\OCR\\build\\ocr-agent-20260921-langgraph\\langgraph-probe\\����ʱ ����\\service\\python.exe', '-c', 'import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")', 'C:\\Users\\A\\Desktop\\OCR\\src', 'C:\\Users\\A\\Desktop\\OCR\\tests\\agent_fixtures\\resume_fault_worker.py', 'C:\\Users\\A\\AppData\\Local\\Temp\\tmpnjf8oohy', '652b0b53804545afa88f0c0f9ea92def', '14312fb6530a477b9ec4b79cf5577d12', '0c809d09c93c4480a7b5b98a1306eaab']' timed out after 30 seconds

----------------------------------------------------------------------
Ran 1 test in 30.207s

FAILED (errors=1)
```

环境探测失败也发生在故障点前：系统 Python 最初未设 `PYTHONPATH` 报 `No module named 'ocr_workbench'`，设为 `src` 后报 `No module named 'pillow_heif'`；隔离运行时不含 pytest，故依文档使用 `scripts/agent_eval/run_tests.py`。这些不计为产品测试失败。

验收映射：本测试仅覆盖 E67 的“消费 resume 后强退”与 E68 的“业务调用结果提交、图 checkpoint 落后”这一条路径，并对 E19/E21 的去重结果提供相邻证据。E24/E25/E42 的队列、取消、锁与事务失败边界，以及 E68 的“图状态领先 UI event”没有由本测试覆盖。具体实现中 `finish_call` 与其 `tool_finished` 在同一个业务事务，正常成功提交时不会产生该工具结果与事件的分离窗口；其他事件投影路径仍需独立故障注入验证。F02 未据此宣称通过。正式四小时运行期间未执行广泛回归、压力测试或真业务评测。
