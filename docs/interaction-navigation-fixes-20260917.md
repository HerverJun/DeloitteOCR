# 点击与定位交互排查（2026-09-17）

针对“查看任务进度点击后像没反应”，继续检查当前源码中的队列入口、复核跳转、建议筛选、图片定位、人工框选及工作区面板切换。额外确认并修复 4 类问题，修复前通过真实浏览器复现了 8 个失败场景。

| 问题 | 触发方式 | 修复后的行为 |
|---|---|---|
| 文档复核“下一处”总是第一项 | 不作采用/拒绝决定，连续点击“下一处” | 按已打开条目继续，跨每组 10 项分页，末项回到首项；已处理项退出列表后继续其后继 |
| 重复打开同一建议不重新定位 | 打开建议 B，在面板切到 A，再点队列中的 B；结构与视觉审校均受影响 | 每次点击都有独立定位请求，重新选中指定建议 |
| 已处理结构建议被默认筛选挡住 | 在文档队列选择“含已处理”，打开已接受的结构建议 | 自动显示历史建议并选中准确条目；普通刷新不会反复重放旧定位 |
| 点击只更新隐藏面板 | 展开校对区后定位原图；小窗口定位或人工绑定；正在看原图时从任务队列查看结果 | 定位与人工绑定显示原图；查看结果及复核入口显示校对区。自动定位数据更新保留正在编辑的面板 |

主要代码为 `App.tsx`、`WorkspaceLayout.tsx`、`DocumentReviewQueue.tsx`、`StructureReview.tsx`、`MultimodalReview.tsx`；文档切换时重新建立复核队列状态，快速校对区分自动定位更新与用户主动跳转。

## 验证

- 新增浏览器导航回归 13 项通过，含修复前的 8 个反例、跨页/循环/条目退出、重复定位、低置信度文字定位以及自动刷新不切走编辑面板。
- 既有真实后端浏览器回归 7 项通过，覆盖正确页面目标、审校结果跳转、真实保存后复制、原始内容保留及响应鉴权。
- “查看任务进度”在 5 种窗口尺寸的回归通过，覆盖超过 60 条记录、反复点击与键盘重开。
- 前端 51 项单元测试通过，TypeScript / Vite 构建通过；Vite 仍有既有的主包体积提示。

导航与任务进度回归使用真实构建页面及模拟 API；后端回归使用隔离数据库、真实 HTTP/保存/PDF 提取和合成 OCR/审校建议。本次结论针对交互与状态逻辑，不包含 GPU 模型准确率或便携包更新。

回执位于 `build/navigation-audit/before.json`、`build/navigation-audit/after.json`、`build/navigation-audit/integration/results.json` 和 `build/task-progress/results.json`。外部 Edge 带 `--disable-gpu --disable-gpu-compositing`，各浏览器与隔离后端运行后关闭。

当前本地工作台服务读取新构建的 `frontend/dist`，刷新页面即可加载修复。

## 重跑

在 `frontend` 目录运行 `npm test` 和 `npm run build`，再在仓库根目录运行：

```powershell
node frontend/scripts/navigation-regressions.mjs
node frontend/scripts/task-progress.mjs
```

真实后端回归使用尚不存在的输出目录：

```powershell
& 'D:/OCR-multimodal-workbench-20260917/bundle/runtimes/service/python.exe' -B -X utf8 scripts/audit_bugfix_ui.py --bundle 'D:/OCR-multimodal-workbench-20260917/bundle' --output build/navigation-audit/integration-new
```
