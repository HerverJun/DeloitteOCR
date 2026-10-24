# 真实原生八图中断与恢复验收准备

脚本：[evaluate_screen_native_recovery.py](../../scripts/evaluate_screen_native_recovery.py)。当前仅编写和静态语法检查，尚未执行 prepare 或真实推理；不存在实测通过结论。

该脚本复用 evaluate_screen_agent_native.py 的 F 盘路径检查、源码清单及 production storage_environment。默认只复制八个冻结输入和 src/config 到独立 F 盘目录，记录输入、源码、模型配置、策略、driver 指纹。未指定 sample-id 时固定取清单前八项；不得按 OCR 成败或真值选择。原始输入只读，上传使用专用副本字节，不把冻结图片交给会消费 temporary 的 add_image。

模型队列结束且最终 screen-policy.json 冻结后，由根任务显式调度 --run。它在同一验收进程创建真正的 create_app、FastAPI lifespan、TaskQueue 和默认 EngineAdapter，走生产鉴权 API 导入/提交/暂停/恢复；不替换 handler、adapter、quality 或选择函数，不读 truth，不注入识别回执或 cache，不启动 Agent 主控。单独的原生工作区从空库开始。

## 过程与判定

1. 同 request_id 重复提交八图，必须保留同 batch。原生策略须与冻结解析结果完全相同。
2. 至少两图已终态且具有可导出的最终表格后，停止本 app 的 coordinator 调度；确认另一个真实 OCR 任务存在在途 IPC request、活跃 worker、对应 task/output。
3. 通过生产 pause 请求暂停待执行项，再调用该 app 自有 queue.stop 取消当前任务。它只清理其 EngineAdapter 拥有的 ProcessJob；脚本不按进程名终止进程、不执行全局 GPU 清理。必须实际观察 interrupted 状态、旧 attempt 目录存在、Job drained/closed、worker 退出及 GPU lock/reservation 清理。若模型先完成而未发生中断，保留证据并判失败，不假定中断成功。
4. 关闭第一轮 lifespan，重建同 workspace 的实际 create_app/store。查回原 batch，再次同 request_id 提交不得新增 batch；显式 resume request_id 重放必须返回原 receipt。
5. 等所有八输入终态。先前成功图片的 task/result/selection/final-content/candidate 及计算 attempt 不变；被中断任务的旧 attempt 原样保留，另有真实新计算 attempt 且任务 succeeded。允许按冻结策略出现不同升级候选，但不允许同最终内容重复采用或一个 task 保存多个结果。
6. 只调用一次 final export；ZIP 无重复 member，manifest 覆盖八输入，每个实际成功工作簿读回格值、文本类型、合并跨度、来源索引和业务说明，对照实际选定 OCR 输出。失败项不得生成假工作簿。该检查证明导出忠实性，不证明 OCR 内容正确率；后者交给离线 truth 评分。
7. 关闭第二轮 lifespan，检查 GPU/coordinator/fusion/external/document 线程退出，所有观察到的自有 worker ProcessJob/process/lock/reservation 释放。完整回执和失败原因保留。

这是可控的原生服务/队列生命周期重启，不是操作系统崩溃、断电或业务生产进程强杀测试。脚本自己不会确认其它模型队列已停；根任务负责安排实际 GPU 独占验收窗口。原生 export 接口没有 request_id，此用例只验证恢复期间没有自动重复导出和一次最终导出；平台重复下载成果身份复用另有平台证据，不能混同。

## 准备命令

以下路径都在 F 盘。native-source 的 config/screen-policy.json 必须先存在并冻结；根任务可选传 policy-file 指定另一份明确、有界的生产验收策略，脚本会独立记录它，不改产品默认。首次默认命令只准备，不加载模型：

```powershell
& 'F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr/runtimes/control/python.exe' -I -B -X utf8 `
  'F:/OCR/output/ocr-screen-benchmark-20261007/worktrees/platform/scripts/evaluate_screen_native_recovery.py' `
  --output 'F:/OCR/output/ocr-screen-benchmark-20261007/native-recovery8-final-001' `
  --inputs 'F:/OCR/output/ocr-screen-benchmark-20261007/dataset-v2/pilot/inputs.json' `
  --native-source 'F:/OCR/output/ocr-screen-benchmark-20261007/worktrees/ocr' `
  --bundle 'F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr'
```

待安排真实 GPU 窗口，重复相同参数并追加 --run。既有准备会重新核对输入、原始图片、冻结源码、模型配置、策略和 driver；一旦创建 run-started.json，禁止重用该目录再次启动。失败后使用新目录，不改写既有证据。

运行前由调用者把 PowerShell 进程 TEMP/TMP/PYTHONDONTWRITEBYTECODE 设置到 F 盘，脚本再设置所有子进程存储环境。禁止改写 HOME/CODEX_HOME。源码/配置/数据必须解析到本机 F 盘，junction/symlink 不得跨出；GPU和旧业务 workspace 不由该脚本迁移或清理。

主要证据：prepared.json/input-map.json、run-started.json、native-input-map.json、submit/resume-receipts.json、interruption.json、before/after-restart-tables.json、before/after-restart-attempts.json、export-readback.json、native-eight-final.zip、native-recovery-summary.json。实际 OCR 原始 JSON、engine.log、IPC 和数据库继续保留在该验收目录。观察到的 IPC request 数是证据采样数量，不能当完整 GPU 调用计数；实际 task/attempt/cache 来源另由权威表与目录核对。
