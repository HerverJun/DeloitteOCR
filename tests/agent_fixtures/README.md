# Agent P0 夹具

可重建生成器：`scripts/agent_eval/generate_fixtures.py`。本次冻结输出位于 `build/ocr-agent-20260920-p0/fixtures-final/`；持久哈希、拆分和评分规格位于 `audit/ocr-agent-20260920-p0/`。二进制、千页数据与封存正文留在 build，不混入应用包。fixtures 和 fixtures-v2 是保留的生成预检批次，不参与后续模型评测。

FX01 原生 20 页中文 PDF；FX02 8 页无文字层扫描 PDF 与图片及冻结 OCR；FX03 独立 Decimal 真值；FX04 修订竞争；FX05 任务状态与故障窗口；FX06 同名不同项目；FX07 注入正文；FX08 100/1000 页、大表和文件故障规格。

每个 split 有稳定逻辑标签→UUID 映射，产品测试创建实际 DB 时按逻辑标签解析，不比较随机生产 ID。JSON 状态是后续 Store fixture loader 的输入规格；P0 未创建 agent 表，也未运行注入故障。任务初始状态由主 fixture、关联 fixture 与 overlay 合成。`assertions` 是评分适配器从真实 DB/文件/事件轨迹计算的事实，禁止把模型自报的布尔值当观察结果。

每场景 6 开发、4 封存。仅自动结构、内容完整性和哈希检查可不曝光；读取或调试物化封存题目/答案前执行 `exposure.py`，被读取项失去封存资格。构造模板由本次实现者编写，记录在 exposure.json；它不是第三方独立盲测或真实扫描质量集。实际模型成绩全部为 not_tested。

使用每个脚本的 `--help` 查看参数。生成器要求全新目标，避免覆盖冻结批次。字体使用仓库 OFL NotoSansSC，PDF 工具只用于离线测试数据生成，不新增生产依赖。
