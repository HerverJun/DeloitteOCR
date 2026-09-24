# G03 / E62 / E63 / E71 / E72 目标机入口审计

2026-09-24（香港时间）。这是源码和旧回执的只读审计，加一项微型验证脚本修复；没有运行候选服务、GPU、Edge、正式 F06 工作区或 29 GB 复制。检查时 `E:\OCR Agent 测试\mixed-candidate08-formal-01\mixed-stability.json` 仍为 `running/soak`，没有最终资格结论。

## 身份与既有证据

- 冻结 `candidate-08`：`E:\DeloitteOCR-Agent-Experimental-20260921\candidate-08`，manifest SHA-256 `91a30c3ba499c104f4949b81e6793961a7b42f98c90f2ed86b87997de5b91938`；构建回执 `audit/ocr-agent-20260921-langgraph/bundle-build-08.json`。`bundle-audit-08` 的同机包审计和选择性搬迁/离线安装回执继续有效，但其 `target-environment-results.json` 明记 `clean_windows=not_tested`。07 完整 ZIP + 08 差量 ZIP 的独立复核不等于 08 完整物理副本或目标机运行。
- `full-relocation-06/full-relocation.json` 是 **candidate-06** 73,629 个文件、29,104,300,359 字节同机搬迁的回执；不能迁移到 candidate-08。05、07 正式轨原始失败不改判，08 正式轨待完成。旧 `target_machine_acceptance.py --role` 的干净 Windows/A4000 回执格式也不能自动代表 Agent G03。
- 冻结 08 的 `tools/target_machine_acceptance.py` 与本次修订前源码同为 SHA-256 `752fca678bb8fe45028f91dc59bf9fe43696b307c498b5e8c8d391a41d5ecc7e`。本次修订后工作树脚本不同；**08 内脚本未被修改**，修复效果仅属源码微型夹具，新包须重新冻结并核对 `source-manifest.json`/manifest。包审计早于后来 E37/E40/E44 等源码变更，也不验证其新行为。

## 入口中的具体缺口与修订

1. `scripts/target_machine_acceptance.py` 原先允许开发树脚本通过 `--bundle` 审核任意候选，子进程还继续运行 `SCRIPT` 绝对路径。干净目标机未携开发树时无法收证；若本机源码已更新，则可能用不同版本脚本给旧包生成貌似属于该包的结果。现在在创建输出前要求入口正是 `--bundle/tools/target_machine_acceptance.py`，子进程执行该冻结入口；旧 08 仍需在目标机显式从包内运行，不能套用工作树修订。
2. 原 `verify_unchanged` 在运行后仅比对成员、大小和 mtime。等长改写并恢复 mtime 会误报 `pass`；修订为前后 SHA-256 和成员比对。代价是正式候选需在运行后再次读取全包约 29 GB。微型 `--self-test` 对等长、原 mtime 篡改的拒绝已通过；这不是大包实测。
3. 原父进程仅看 worker 退出码，不核对 `candidate-runtime.json` 的 `status`、default/opt-in 子结果及网络尝试数组；一个退出 0 但不完整/失败的回执可推进到总体已观察通过。修订增加这些字段核验。此处只做源码检查，没有启动候选 worker。
4. `--role clean-windows/a4000` 是旧交付入口：读取 `config/development-machine.json`、`audit_application.py` 和 GPU 回执；Agent 构建器 `prepare_agent_bundle.py` 特意移除了 `development-machine.json`，因此直接对 Agent 候选运行旧 `--role` 会失败，不能当 G03 入口。旧入口依据 MachineGuid 不同、开发命令 PATH 清单和操作者勾选判断“干净”；这不足以证明无系统级开发环境。它接受同机器/manifest 匹配的 `os-isolation-result.json`，不验证其对应审计程序、网络探针原始回执或时间/进程连续性，旧结果可能被重用；`offline_guard.cpp` 的 WFP 只临时阻断所列可执行程序的出站连接，绝非物理断网。
5. 新 Agent 入口的 `machine.clean_install`、`different_windows_user_dpapi`、`physical_disconnection` 明写 `not_tested`，`upgrade-rollback-results.json` 全为 `not_tested`；正确处理是保留缺口，不能把 `observed_checks_passed_with_qualification_gaps` 或退出码 0 提升为 G03/E63/E71/E72 通过。环境变量伪造 `%USERPROFILE%`/`%LOCALAPPDATA%` 仍在同一 Windows 身份，不能证明另一用户 DPAPI。进程内 Python audit hook 不覆盖原生 DLL、子进程或系统网络。`check_layout` 的 wheel 文件哈希和已安装版本/来源检查不执行干净机完整离线安装、许可证全覆盖检查或长期任务；worker 只创建新 checkpoint 并检查 saver 类型，不能证明重启读取、两库一致备份、项目删除、增长/竞争与旧版可执行回退。
6. `scripts/agent_eval/relocate_full_candidate.py` 负责完整复制并逐文件核验，才可产生完整搬迁回执；`target_machine_acceptance.py` 自己仅观察候选路径包含非 ASCII，`relocation_from_prior_path=not_tested`。完整复制在 08 尚未执行。`relocate_full_candidate.py` 的子验收只确认 `target_receipt.status` 字符串，正式目标机执行时仍须核对 manifest、候选身份、每阶段细项和错误日志。旧 06 回执不外推。

## 目标机待执行清单（独立新目录、独立回执）

- [ ] 先确定要验证的**新冻结候选**和 manifest/source 清单哈希。若仍是 08，只能使用其冻结脚本的旧语义；不能声称已带本次修复。保留 07 ZIP/08 差量的原始 SHA 与重建回执；若生成新候选，重新独立包审计、归档验证和源/前端/包差异记录，勿覆盖旧回执。
- [ ] 在另一台有机器身份/系统版本/用户 SID/开发环境清单记录的干净 Windows 上，用包内 Python 和包内 `tools/target_machine_acceptance.py`、全新包外输出运行 Agent 有界启动检查；核对每项、工作区及失败日志。对 E62 再做真实完整候选的中文路径跨盘搬迁、复制前后全清单哈希及搬迁后包内运行时启动；确认没有依赖系统 Node/Python、许可证/静态资源和锁均匹配。单纯中文目录启动不是“搬迁”。
- [ ] 在物理断开网线/Wi-Fi、记录适配器状态与外部网络探针不可达的窗口运行普通 OCR/校对/导出和 Agent 离线本地场景；另以管理员临时 WFP `offline_guard.exe --application` 收原始探针/应用回执，分别记录物理断网与进程级阻断。确认 tracing 环境启动不外连，所有锁定传递 wheel 在无网环境从包安装、许可清单完整、checkpoint 无 Key/服务对象及不可信 pickle 回退。物理断网和 WFP 不相互替代。
- [ ] 以**真正不同的 Windows 用户 SID**，使用隔离复制的工作区/凭据样本观察原用户 DPAPI Key 不可解密、可重新配置、本地 OCR 不受影响；扫描日志、数据库、checkpoint、诊断和包内文件无明文 Key。不能仅改变 `USERPROFILE`/`LOCALAPPDATA`。
- [ ] E63：冻结旧版与新版可执行包，在匹配的升级前完整工作区副本上执行真实包升级、连续异常退出/恢复及请求/operation 去重核对；保存两库备份标识、业务/会话/建议/产物状态与每次崩溃点的原始回执。旧版只打开匹配的升级前副本，测试旧可执行程序回退；不覆盖唯一的新工作区。合成 schema 回退不等于旧程序实际回退。
- [ ] E72：两库备份第二半故障、完整集合发布规则、还原可读性、项目清理、长期 checkpoint 增长和写竞争在独立目标工作区实测；记录官方 saver 的容量/延迟与失败处理。正在运行的 F06 正式目录不得复用。所有目标机结果给出 `pass/fail/not_tested/conditional`、确切命令、输入/输出 SHA、机器/SID、时间和日志；任一缺项仍保留 `not_tested`，不汇总为生产资格。

本次验证：`audit/ocr-agent-20260924-target-acceptance-selftest-01/self-test.json` 为小文件 `pass`（manifest、前后哈希、等长恢复 mtime 篡改、路径穿越）；`audit/ocr-agent-20260924-relocation-selftest-01/relocation-selftest.json` 为 20 字节的合成搬迁 `pass`。使用源码脚本指向微型非包目录的 `--bundle` 调用被入口身份校验拒绝，且未创建输出目录；`python -B -m py_compile scripts/target_machine_acceptance.py` 通过。修订后源码脚本 SHA-256 为 `8821cce6729db301610ba96909bda6878a58d5c37453c4ab765744d14fb8174b`。没有运行真实 `--bundle`、`--role`、完整搬迁或目标机测试。
