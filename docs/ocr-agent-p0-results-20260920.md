# OCR Agent 第一阶段 P0 完成回执

2026-09-20；范围：开发计划的第一阶段 **P0 基线与契约（A01—A03）**。实施分支 `codex/ocr-agent`，本阶段完成；助手运行时和 UI 尚未实现，不代表完整 agent 可用。

## 交付物

| 任务 | 已交付 | 证据 |
| --- | --- | --- |
| A01 | 534 文件源码/配置/测试/文档基线与副本、脏工作树记录、实际运行时/依赖/包位置、隔离 schema 12 数据库、六场景入口、回归基线 | audit/ocr-agent-20260920-p0/baseline.json、runtime-baseline.json、preservation-check.json、existing-regressions.json、scenario-entrypoints.md |
| A02 | 12 个工具输入 schema、统一结果/错误/引用、运行状态/配对校验、20 条 API 声明、作用域/授权/预算/SSE/功能开关默认值、ADR | src/ocr_workbench/agent/contracts.py、config/agent-policy.json、docs/ocr-agent-p0-architecture-decisions.md；audit 下 contracts.json、policy-defaults.json、architecture-decisions.md |
| A03 | FX01—FX08 两组实例、4 份 PDF 共 56 页、16 张合成扫描图、Decimal 真值、竞争/故障/越项目/注入/千页压力状态规格、60 条中文任务（36 开发/24 封存）、评分器规格与曝光记录 | build/ocr-agent-20260920-p0/fixtures-final；audit 下 fixtures-manifest.json、evaluation-split.json、scoring-contract.json、exposure.json |

Pydantic 直接依赖锁定为 2.13.5，与已有传递锁和本机服务运行时一致；没有增加生产 PDF 依赖。现有业务源码、UI、原计划和任务 JSON 未修改；任务实际进度单列 task-state.json。数据库版本仍为 12，13 只是下一阶段候选号。

## 验证

- 新增 10 项契约边界测试通过，覆盖整数严格性、未知字段/授权注入、来源互斥、整批调用原子验证、配对、未完成任务不能报成功、UTF-8 上限、状态与 generation。
- 完整后端运行 494 项：493 通过、1 跳过；跳过的是需要独立 GriTS 评分运行时的既有测试。
- 前端 11 个测试文件、51 项全部通过。本阶段无前端代码修改，未重新构建历史交付包。
- 夹具 49 个冻结文件哈希、8 个类别/16 实例、60 任务、每场景 6/4 拆分、模板/文档组不重叠均通过。全部 40 页原生 PDF 可提取预期中文关键词，16 页扫描 PDF 无原生文字层。四份 PDF 首页面渲染目视检查通过。
- 服务运行时抽查的已锁依赖无版本偏差；隔离数据库 schema=12，integrity_check=ok，foreign_key_check 无错误。

早期两次环境尝试完整留档：系统 Python 缺依赖；源码副本缺配置引用的 ignored build/pdfplumber 路径，出现 5 个生命周期失败。原工作区同源码复现该组 12 项全部通过，最终全套通过；没有把环境失败静默删除或称作本次修复。

## 结论边界

这是 P0 契约与评测准备完成。项目归属校验、凭据、数据库迁移、工具执行、SSE 服务、UI、真实模型探针和故障注入仍由后续阶段实现。策略文件默认 agent_enabled=false，当前生产服务不加载新模块，不联网。

所有模型任务均为 not_tested；没有真实模型成功率、内容质量改进或生产发布资格。数据全部合成，作者知晓生成模板；封存表示未用于后续模型/提示调试，不是第三方独立盲测。已记录模板作者、预检批次、PDF 视觉检查与自动校验曝光情况；后续读取/调试物化封存题目必须失效并补新批次。

fixtures 与 fixtures-v2 保留为预检批次；最终只有 fixtures-final 计入 60 条任务。评分固定整体 ≥65/72、每场景 ≥10/12、首次有效工具调用 ≥98%、引用 100%、高影响错误 0，不生成模拟成绩。

## 复现

以下使用本机已验证路径；换机器先根据 runtime-baseline.json 配置等价依赖。各新增脚本均提供 --help。新生成目录必须不存在冻结 manifest。

```powershell
$servicePython = 'E:/DeloitteOCR-ComplexTables-20260919/bundle/runtimes/service/python.exe'
& $servicePython -c 'import sys,unittest;sys.path.insert(0,"C:/Users/A/Desktop/OCR/src");suite=unittest.defaultTestLoader.discover("tests");result=unittest.TextTestRunner(verbosity=2).run(suite);sys.exit(not result.wasSuccessful())'
npm --prefix frontend test
& $servicePython scripts/agent_eval/export_contracts.py --output build/ocr-agent-contract-recheck
```

```powershell
$fixturePython = 'C:/Users/A/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
& $fixturePython scripts/agent_eval/validate_fixtures.py --root build/ocr-agent-20260920-p0/fixtures-final --receipt build/ocr-agent-20260920-p0/recheck.json
& $fixturePython scripts/agent_eval/generate_fixtures.py --output build/ocr-agent-fixtures-rebuild --audit build/ocr-agent-fixtures-rebuild-receipts
```

下一步是 P1 B01—B05，详见 audit/ocr-agent-20260920-p0/NEXT.md。本阶段没有提交或发布旧成果；本次文件清单与 SHA256 见 artifacts.json。
