# 文档编辑器界面改造 · 2026-09-12

本轮沿用 React、Fluent UI 和现有本地 API，将工作台改为中性灰白的文档编辑器。绿色保留用于品牌、主操作、选中状态；原有识别、校对、版本、导出和任务队列逻辑继续使用。

## 界面变化

- 浅色顶栏和资料栏。Deloitte 标志复用原 SVG 路径，只将白色字形改为深色，绿点不变。
- 标题区和识别条缩小；识别条常规高度为 54px。批次预处理移入可用键盘开关的 Fluent Popover，按钮显示当前处理方式。
- 统一字体、灰阶、边框和 6px 控件圆角；原图使用中性灰画布，表格与文字使用白色编辑区。
- 增加“舒适／紧凑”切换，并在本机记住选择；紧凑模式调整文档列表、表格和文字行距。
- “确认此结果”直接显示。保留原有版本、采用结果和忙碌状态校验；编辑后仍按原有逻辑取消已确认状态。
- 有低分单元格时显示数量及“下一处”。按钮移动真实键盘焦点；阈值设置和原始分数说明仍在更多操作中。
- 低高度窗口将结果标题、文件名和结果选择并排，1024×576 紧凑示例中可见表格高度为 141px。

## 主要文件

- `frontend/src/editor.css`：本轮视觉变量、表面样式、密度和低高度布局。现有 `professional.css` 保留原有布局和功能适配。
- `frontend/src/recognition.css`、`RecognitionBar.tsx`：紧凑识别条和预处理弹层。
- `frontend/src/theme.ts`、`BrandHeader.tsx`、`WorkspaceLayout.tsx`：Fluent 主题、浅色品牌标志和密度切换。
- `frontend/src/App.tsx`、`TableEditor.tsx`：复核入口和低分定位提示。

## 验证

- TypeScript / Vite 生产构建通过；现有 19 项前端测试通过。
- 原有隔离浏览器回归 11 项通过：`build/ui-beautify-audit-20260912-run3/audit-fixes-evidence.json`。
- 原有异步编辑与导出回归 10 项通过：`build/ui-beautify-functional-20260912/regression-results.json`。
- 本轮界面检查 7 项通过：`build/editor-design-20260912/result.json`。覆盖 1440×900、1366×768、1024×576、密度持久化、弹层键盘操作、低分定位、复核与保存撤销、空项目入口。
- 另验证 700px 与 500px 宽度展开资料栏时，工作区保留全宽、无横向溢出，收起按钮未被遮挡。
- 浏览器检查无页面脚本错误；本轮示例页面无外部网络请求。

截图位于 `build/editor-design-20260912/`：`desktop.png`、`laptop.png`、`compact.png`、`settings-popover.png`、`empty.png`。截图使用独立示例数据，结果由 fixtures 构造，不代表本轮重新运行 OCR。截图由外部无头 Edge 生成，启用 `--disable-gpu` 和 `--disable-gpu-compositing`，检查结束后关闭浏览器。

## 预览与交付范围

本轮已更新源码和 `frontend/dist`，未重新制作离线便携发布包。工作区原有的其他未提交修复保留。

在本机启动独立示例预览：

```powershell
& 'E:/OCR-deloitte-build/bundle/runtimes/service/python.exe' -B -X utf8 build/editor-design-20260912/serve.py
```

服务监听 `127.0.0.1:8765`，仅使用 `build/editor-design-20260912/preview-data`。不会启动 OCR 工作线程，也不会读取现有用户项目。重复启动复用该目录的示例数据。首次初始化时复制图片作为上传临时文件，避免导入函数消费源 fixtures。
