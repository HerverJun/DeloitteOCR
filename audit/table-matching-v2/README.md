# 表格局部匹配 v2 证据索引

这是源码工作区实验的精简证据，不是新离线包的验收回执。实际交付结论见 `docs/table-cell-matching-v2-quality.md`；默认精确定位未达标，继续保留实验入口。

- `test-selection-lock.json`：首次测试推理前的原始代码/策略/采用规则锁，保留原样。
- `test-tooling-amendment.json`：封存后仅回放矩阵 tuple/list JSON 恢复修复；匹配源码、策略及矩阵序列化结果未变，不构成新的完整代码独立测试。
- `reports/`：开发/验证/原封存测试主轨、开发/测试实际采用结果轨，以及完整页检测摘要。详细逐格报告的本地路径和 SHA 随摘要保存。
- `cells-*`、`pages-*`：推理前的数据资格、协议和分组锁。原始 cells 资格文件未覆盖后续独立 pages 补充，二者分别读取。
- `m0-legacy.json`、`real-artifact-regressions.json`：历史重放、候选 oracle、局部扰动；`n3-probe.json` 为预选 12 表模型候选研究，不能代替盲测。
- `runtime.json`、`performance.json`、`cache-profile.json`：本机运行时/模型哈希与自动化性能，CPU/GPU 分列；不是人工效率或异机验收。
- `python-regression.log`、`replay-lock-regression.log`、`validation-summary.json`：完整应用回归和封存后评测工具修复检查。前端原始 stdout 未单独留存，摘要明确标注。
- `ui-results.json`、`ui-location-timing.json`、两张 PNG：外部无头 Edge 自动化及截图，浏览器已关闭，图中为合成互动样本。
- `data-sources/`：取得的数据卡/官方许可原文及哈希。超时记录保留；`receipts-final.json` 记录后续成功取得的官方 blob。
- `benchmark-input-restoration.json`、`final-input-integrity.json`：性能导入临时移动一个开发图像后的原 SHA 恢复记录；全部 260 裁剪图和 60 完整页重新校验通过。
- `delivery-manifest.json`：本目录文件哈希及完整原报告索引。

公共图像、转录、逐格大文件、模型权重和交互数据库保留在本地 `build/table-matching-v2`，不复制到本目录。WTW 摘要来自已曝光历史检测，不是本轮新测试。原有历史模型输出和报告仅作只读复算，未改写。
