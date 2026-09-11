# Deloitte OCR 工作台改版审计报告

日期：2026-09-12。版本：0.7.0rc2。源码分支：codex/deloitte-workbench。

## 交付结论

第二轮视觉精修及本机回归完成。新版使用官方 Deloitte 字标、黑绿框架、独立原图与校对面板、本地中文可变字体、14px 表格正文、可调分栏和底部队列。长编号保持文本显示，结果与原图版本提示保留。旧 D 盘已验收包未被覆盖。

独立可运行目录：E:/OCR-deloitte-build/bundle。测试及示例数据与旧包隔离。桌面已建立“Deloitte OCR 新版预览”和“Deloitte OCR 新版交付”快捷方式。

## 验证记录

| 项目 | 本轮结果 | 证据 |
| --- | --- | --- |
| 前端构建 | TypeScript 与 Vite 构建成功 | unit-tests/frontend-result.json |
| 单元测试 | 前端 8 项、包内 Python 62 项通过 | unit-tests |
| 核心 GUI | 13 组通过，四真实引擎、编辑、坐标、图像修正、导出、刷新与导入 | ui-core-release |
| 批量与管理 GUI | 5 组通过，批次预处理、独立/汇总工作簿、完整离线引擎包启用及回退、项目清理 | ui-features-release |
| 新布局与状态 GUI | 10 组通过，分栏、筛选、保存失败恢复、队列暂停/取消/重试/继续与刷新保留 | ui-features-release-redesign |
| 导出独立读回 | Excel/JSON/TXT/Markdown；前导零、长编号、合并单元格、2 工作表、原始与校对隔离正确 | 两套 GUI 的 export-verification.json |
| 视觉 | 1920×1080、1440×900、1366×768、1093×614、900×768 无整体横向溢出；队列不覆盖导出栏 | brand-demo-final、ui-features-release-redesign |
| 本地资源 | Logo、SVG、字体本地加载；两组浏览器证据外网请求为 0；SVG 无脚本与外链 | brand-audit、brand-demo-final |
| 启动 | 最终编译启动器完成全部文件 SHA-256 校验、GPU/驱动检查、运行时原生依赖导入 | final-startup.json |
| 完整归档 | ZIP 全部 65,000 文件独立解压读回、CRC/SHA-256 和清单一致，无重复或遗漏条目 | archive-verification.json |
| 保护组件 | 64,330 个 runtime/model/tool/lock 文件与原包一致 | component-diff-final.json |

GUI 均使用外部 Edge/Chromium，并传入 --disable-gpu、--disable-gpu-compositing。所有本轮测试浏览器、测试启动器和测试服务已退出。浏览器中仅模拟一次 PUT 503 验证保存失败，其余 OCR、图像处理与引擎包均真实执行；未伪造模型输出。队列刷新恢复使用真实任务，崩溃恢复状态机由本轮 Python 测试覆盖。

125% 检查采用 Chromium deviceScaleFactor=1.25 与 1093×614 CSS 视口，生成约 1366×768 像素截图；并非修改 Windows 系统缩放后的人工验收。布局偏好按浏览器同源保存，启动器端口变化时可能恢复默认。本轮主要文字颜色对均达到 4.5:1；该检查不等同于全页面 WCAG 认证。

## 资源与视觉证据

官方字标来源 https://www.deloitte.com/content/dam/assets-shared/logos/svg/a-d/deloitte.svg ，原 SVG 未重画。通过用户指定 sub2api Responses 接口，由 gpt-6-astra 主模型调用 image_generation 生成纸页插画参考，并输出独立可编辑 SVG；实际工具调用 1 次，回执为 completed。提示词、模型文本、PNG、SVG 和回执均保存在 asset-generation。

Noto Sans SC 字体及 SIL OFL 1.1 许可随包提供，许可收集脚本已支持该资源并在独立目录验证。品牌商标不宣称属于应用代码的开源授权范围。

前后截图使用同一份明确标注为演示的“设备验收记录”和同一真实 PaddleOCR-VL 输出；改版前资源由保留的 web-original 提供给独立浏览器上下文，未替换候选包文件或识别结果。对比图与空状态、不同分辨率截图见交付目录 screenshots。

## 清单关联

最终清单 SHA-256：`7dd4071be012b7088dc2047885689992a68cf726a315e49576339a47bd378cd9`。

核心 GUI 与示例截图最初关联清单 `9d23b8bb2ac0a9d935f1d98c9092a3de00bb47db03be3eaa9669d135fa48688a`。此后仅修正 licenses/frontend-index.json 中字体许可引用路径；所有应用、前端、模型及运行时字节相同。manifest-lineage.json 对两份完整清单逐项比对；最终清单再次通过启动器全量校验和归档逐文件读回。原始证据哈希保留，没有把旧记录静默改称新清单运行结果。

本报告针对本机本次视觉与操作改版；不把历史 OCR 数据集成绩、旧离线阻断验收或外部公司机器验收直接认定为本次重新执行。更完整的模型质量与目标机器验证仍使用其专门验收流程。
