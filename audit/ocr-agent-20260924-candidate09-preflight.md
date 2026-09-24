# 下一候选（暂称 candidate-09）静态预检

2026-09-24 香港时间，仅检查小型源码/构建脚本、已有 manifest 元数据、卷空闲量和正式轨进程回执；没有运行大包构建/哈希、候选服务、GPU、Edge 或完整复制。`E:\OCR Agent 测试\mixed-candidate08-formal-01\mixed-stability.json` 读时仍为 `running/soak`。`process.json` 的启动命令明确以冻结的 `source/scripts/agent_eval/mixed_stability.py`、`--bundle E:\DeloitteOCR-Agent-Experimental-20260921\candidate-08`、独立输出目录运行 14,400 秒/三重复；当前源码/将来的 09 不是这条 F06 的被测版本。不要修改 08 原始回执或把其未来结果算作 09 成绩。

## 构建路径与当前阻断

`scripts/prepare_agent_bundle.py` 把当前 `src→app`、`config→config`、`frontend/dist→web`、`scripts→tools`，并将 `frontend/src` 和脚本收进 `source-code.zip`/`source-manifest.json`；`runtimes/service` 则来自明确提供的 `--service-runtime`，不是 08 旧 service 的继承。它将 08 的模型和非 service 运行时作为候选继承资产，`--hardlink-inherited-assets` 仅在同卷生效。故 09 若从 08 构建，最新 E37/E40 Python/TS 源文件和目标机脚本会进入包，但 **web 只拿现有 dist，不会自行执行前端构建**。

现实阻断：`frontend/dist/index.html` 与资源最后构建时间是 03:14，而 `AgentArtifact.tsx`/`agentApi.ts` 的覆盖清单下载修改是 08:15。08 源清单中的这两文件哈希分别 `35ac…d739`、`e326…e0b2`，当前分别 `6813…d78a`、`be47…5d2f`；`artifacts.py` 当前 `1779…b750` 与 08 的 `d56f…85ff` 不同。当前目标机脚本 SHA-256 `8821…174b`，08 内为 `752f…ecc7`。修订构建入口增加 `require_fresh_frontend`：生成物 `dist/index.html` 早于前端顶层构建配置、`src` 或 `public` 输入即在创建 staging 前失败。当前源码静态调用已按预期拒绝旧 dist；应在正式 F06 结束后先重跑前端测试与 `npm run build`，再构建 09。mtime 防线是保守的预检，包审计仍需核对真实资源字节与 UI 行为。

08 的 manifest 元数据为 73,630 文件、29,104,348,303 字节；`runtimes/service` 约 249,067,051 字节。08 构建回执的共享 hardlink 为 69,277 文件、28,554,365,305 逻辑字节；这不是独立物理副本。09 使用 08 同卷 hardlink 将继续与旧候选共享这些 inode，旧资产均须保持只读。没有这模式时仅继承资产约 28.5 GB，新目录在当前任一卷均缺空间。

按预检时 `Get-PSDrive`，C/D/E/F 空闲约 6.54/15.51/16.31/19.22 GB。`prepare_agent_bundle.py` 的硬链接模式在 E 卷的门槛约为已知 08 非链接继承复制量 253,783,604 字节 + 3 GiB 余量，即至少约 3.48 GB；仍会复制独立 service runtime、源码、前端和许可并全哈希约 29 GB，运行时必须重新量测余量且留给其他进程空间。F06 正在 E 工作区写入期间不得据本快照开工。07 完整 ZIP 文件长 27,900,400,290 字节，08 差量 18,081,355 字节；当前任何卷余量均不足以另写约 28 GB 的独立 09 全量 ZIP，完整 09 物理搬迁约需 29.1 GB 加回执/安全余量，也不能在当前卷执行。

## 归档链判定

`scripts/agent_eval/candidate_delta_archive.py` 只接受 **一份完整 base ZIP + 一层目标差量**。08 只有 07 完整 ZIP 与 08 差量 ZIP，不是完整 08 ZIP；把 08 差量当作 `--base-archive` 会因缺完整成员而失败。现有验证器不接受 07→08→09 三层链，不能声称可用。可用路径是直接对 07 完整 ZIP 及新 09 目录生成**累积的 07→09 差量**；其 `changed/deleted`、09 manifest、07 完整 ZIP SHA-256 全绑定，并由第二次 `verify` 对 07 与新层逐成员 CRC/SHA-256、组合清单独立核验。此路径不需要额外 28 GB 完整 ZIP，但必须保留 07 ZIP 和 09 差量两件；旧 08 差量及其回执原样保留作历史。工具的 `--candidate` 已作为通用目标目录参数，旧 `--candidate08` 仍兼容；小型合成 CLI 与核验测试通过。差量实际大小只有构建后才知道，不能用 08 的 18 MB 推定 09 大小；`space_preflight` 对 changed 文件、ZIP 扩张及 256 MiB 余量在写入前判断。

## F06 完成后可执行顺序（此处未运行）

1. 只读封存 08 正式轨最终原始状态/错误/性能及进程、冻结 source 与 08 manifest 身份；失败原样保留。独立安排 09 的 F06，不能转移 08 四小时成绩。先复测当前源码 E37/E40/E44、重启/恢复相邻项、完整后端回归；前端 `npm test`、`npm run build`、TypeScript/Vite 构建回执和真实 HTTP 401/4xx、完整/部分下载+覆盖清单、实际 UI 下载。之后不得无回归地再改源码。
2. 再量测 E/F/C/D 空闲与 F06 占用、核对新工作目录不存在。使用 `--base-bundle 'E:\DeloitteOCR-Agent-Experimental-20260921\candidate-08'`、`--service-runtime 'C:\Users\A\Desktop\OCR\build\ocr-agent-20260921-langgraph\langgraph-probe\运行时 中文\service'`、`--output 'E:\DeloitteOCR-Agent-Experimental-20260921\candidate-09'`、包外全新 `--receipt` 和 `--hardlink-inherited-assets` 执行 `scripts/prepare_agent_bundle.py`。这一步会读/哈希约 29 GB，须待正式 F06 退出并留有余量后运行；不得修改 08/07 的共享资产。核对 09 `bundle-build`、source manifest、web/入口哈希和 service 锁与当前修复身份。
3. 在不同的新输出目录运行 `scripts/audit_agent_bundle.py --bundle <09目录> --output <新审计目录>`；审阅 `bundle-validation.json` 各阶段、完整哈希、41 wheel/许可、冻结测试、真实候选入口及工作区/回退限制。它的选择性中文路径搬迁不代表完整 29 GB 搬迁。审计若失败，保留原始失败，修复后构建新身份，不能改动冻结 09。
4. 以 `python -X utf8 -B scripts/agent_eval/candidate_delta_archive.py build --base-archive 'D:\OfflineOCR-Agent-Experimental-candidate-07.zip' --expected-base-sha256 9754d2fbb861b88d66e443721d89f5f31ead9ee3dd15594ec45400360a1ed648 --candidate <09目录> --layer-archive <新09差量ZIP> --receipt <新构建回执>` 构建 07→09 累积差量；再以同脚本 `verify` 和**另一新回执**独立核对。它会重复流读 07 ZIP 与候选全文件，必须在正式轨停止且余量稳定后运行。两件交付必须共同保留、在目标机重建后全哈希并实际启动；当前工具不提供三层解包或完整物理搬迁验收。
5. 在有足够额外空间的独立磁盘/目标机做 09 完整中文路径搬迁、`target_machine_acceptance.py` 包内入口和干净 Windows、物理断网、跨用户 DPAPI、旧可执行程序回退及 E72 双库/增长实测；按前一份 `ocr-agent-20260924-target-machine-entry-audit.md` 的清单分别收证。新候选仍属实验待验证，不把静态预检、07→09 逻辑 ZIP 身份或同机包审计提升为目标机/生产资格。

本轮仅运行 3 个 `test_prepare_agent_bundle.py` 微型夹具和 8 个 `test_candidate_delta_archive.py` 微型夹具，均通过；当前源码前端新鲜度调用按预期报旧 dist。没有构建或哈希大候选、归档、真实搬迁、前端构建或完整回归。
