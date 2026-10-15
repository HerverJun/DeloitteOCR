# 多人任务中心部署、备份和回退

## 存储和部署边界

源码、Git 元数据、数据库 data/WAL/日志、依赖、缓存、临时、资料、成果和备份全部在本机 F 盘。原生应用只监听回环地址，内网用户仅访问 HTTPS 门户。数据库也只监听回环地址；运行账号与建库/迁移账号分离。

登记根为 `F:\OCR\output\task-history-multiuser-20261015`：`worktrees/platform` 是开发源，`dependencies` 是 PG 和 Python 离线依赖，`runs/workspace` 是合成测试工作区，`delivery` 是封存候选，`evidence` 是报告。共享资产为 `F:\OCR\output\clear-automation-dev-20261002\program`；Agent 依赖层为 `F:\OCR\output\platform-agent-dev-20261004\dependencies\agent`。盘外只读工具为系统 Chrome/Node，不在那里创建项目产物。

候选源码含平台、OCR、分类三仓自有文件及三个前端 dist。原生启动器要求这三份源码位于同级目录。离线 Python 包见 `config/task-layer.lock`；PG 官方 ZIP URL、SHA256 和共享资产声明见候选依赖回执。安装前按数据库、导入文件、WAL、备份、恢复副本和压测峰值核算空间，禁止自动回退到其他盘。

## 新环境建立

1. 在新的 F 盘专用目录准备 `temp`、`cache/localappdata`、`cache/appdata`、`dependencies`、`runs`。设置当前进程 TEMP/TMP/TMPDIR、LOCALAPPDATA/APPDATA、pip/npm/HF/Torch/PG 缓存和日志目录；不修改 HOME/CODEX_HOME。
2. 按依赖回执校验 PostgreSQL 17 ZIP，解压为 `dependencies/postgresql/pgsql`；用 F 盘 Python 从 wheelhouse 执行 `pip install --no-index --find-links <F wheelhouse> --target <F python-layer> -r config/task-layer.lock`。基础运行时仍需与共享资产清单一致。
3. 运行 `scripts/setup_task_postgres.ps1 -TaskRoot <F root> -Port <空闲回环端口>` 建立新集群，不能覆盖既有凭据。新集群开启 checksums、fsync、synchronous_commit、full_page_writes；本轮既有测试集群创建时未开启 checksums。
4. 在独立会话读取管理员私密 JSON 到环境变量 `CLEAR_DATABASE_URL`，设置 `CLEAR_TASK_LAYER`，通过 `scripts/task_python.py -m workbench_platform.tasks.cli migrate` 显式迁移。启动不会自动迁移或回退 SQLite。
5. 用 `create-admin --username <账号> --name <姓名>` 交互输入管理员密码；勿把密码写进命令历史。`scripts/provision_task_runtime.py --admin-config <私密配置> --output <新的 runtime-private.json>` 创建受限运行账号。管理员配置只供迁移、恢复和维护使用。
6. 申请内网正式域名和受信 TLS 证书，配置防火墙只开放门户端口。测试的 localhost 自签证书只供本机演练。
7. 用 `scripts/serve_task_stack.py` 指定 `--program`、`--data`、`--private-config`、`--dependency-layer`、`--postgres-bin`、`--port`、`--worker-port`、`--public-origin`、`--certificate`、`--private-key`。启用 Agent 时另传 `--agent-layer`。所有路径必须在 F 盘。

门户和 worker 可分别以 `--mode portal|worker` 运行。默认门户退出不停止 worker；明确停止执行服务用 `scripts/stop_task_worker.py --data <F data> --private-config <F runtime config> --port <worker port>`。不要用结束整个终端进程树的操作模拟仅门户重启。源码变化后先排空并明确停止旧 worker，版本漂移校验会拒绝执行新命令。初期 `CLEAR_TASK_CONCURRENCY=1`，最大 4，必须按实际模型资源调整。

## 备份和恢复

门户运行期间每日香港时间 02:00 备份；机器/门户停机时不会补造成功记录。生产运维可在固定 Windows 计划任务里调用同一维护入口，避免重复调度。备份开始阻止新写入并排空执行，最多等待 5 分钟，未核实任务会使备份失败，不强停业务执行。后台持久事件追上后，在数据库写屏障内执行 PG custom dump、SQLite online backup 和资料/成果复制，记录逐文件 SHA256。

在已设置 F 盘环境、`CLEAR_DATABASE_URL` 和 `CLEAR_POSTGRES_BIN` 的维护会话中：

```powershell
python -B scripts/task_python.py -m workbench_platform.tasks.cli backup --root <F 工作区>
python -B scripts/task_python.py -m workbench_platform.tasks.cli verify-backup --root <F 备份目录>
python -B scripts/task_python.py -m workbench_platform.tasks.cli restore --root <F 备份目录> --destination <F 新目录>
```

这里的 `python` 必须替换为已登记的 F 盘运行时绝对路径。恢复需建库权限，使用新数据库和不存在的目标目录，`pg_restore --no-owner --no-privileges`；完成后另建受限运行账号。恢复时清除登录会话、重新绑定中央库身份，搬移操作路径及加密 JSON 命令中的工作区路径；任务输入和历史摘要保留原始快照。SQLite/文件清单校验失败时不切换。

DPAPI 私密文件仅当前 Windows 身份可解密；跨机器或跨账号恢复需管理员重新配置 AI，不能承诺密钥可移植。恢复验收必须包含：登录、跨项目拒绝、同哈希成果下载和一次真实执行，而非仅 pg_restore 返回 0。

任务元数据、事件、审计长期保留。归档只是隐藏默认列表。正式成果与原件不按临时规则删除；debug 日志 30 天，无引用临时文件 7 天。`retention --root <F data>` 默认只预览，`--apply` 才执行；私密命令无法读取时跳过相关清理。备份副本需要管理员按容量策略管理，当前不自动删除历史备份。

## 历史迁移与回退

先停止旧程序写入，取得一致性备份，在新 F 盘工作区执行 `import-legacy --root <F 副本>`。按 namespace/native ID/generation 和投影别名去重，可重复执行；执行人、时间、AI 缺失时保留空值，不推测。未归属项目任务仅平台管理员可查询。核验来源映射、数量、状态、输入和成果哈希后，才进入正式切换审批。

旧库副本须附 `legacy-snapshot.json`，明确 `{"cold": true}`；已有平台备份则使用带 manifest 的已校验目录。迁移工具拒绝直接读取仍可能写入的原工作区。冷副本声明不代替停写及一致性备份操作。

正式切换前保留旧程序和升级前只读备份。回退运行旧程序配旧数据副本，禁止让旧程序打开已经要求 `task-ledger-pg-v1` 的新工作区；新工作区和新 PG 数据保留以便追溯。不执行破坏性 Alembic downgrade。备份和回退资料含凭据/原件，应沿用工作区 ACL。

## 运行观察

管理员接口 `/api/platform/v2/metrics` 提供待调度数、worker 心跳、同步积压及采样时间、磁盘余量、最近备份和备份错误。领域服务不可达单独记录，不覆盖已确认终态。待核实任务先使用“核实”；无法确认外部 AI 或副作用请求时保持待核实，不自动重发。排队的兼容 v1 请求超过 30 秒可能返回带任务 ID 的 503，任务已经持久化；客户端必须沿用同请求键或查询任务，不能生成新键反复提交。
