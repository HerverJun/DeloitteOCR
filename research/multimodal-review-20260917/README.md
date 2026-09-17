# 研究材料保存边界

仓库保存本项目撰写的分析、检索索引、来源 URL/revision/SHA 与抓取脚本。上游仓库的 README、源码、目录树、元数据和网页 HTML 是本地研究缓存，由 `.gitignore` 排除；它们不是产品依赖，也不会打入产品。

可追溯入口：`github/repo-index.json`、`github/source-index.json`、`github/supplemental-sources.json`，以及 `benchmarks/source-index.json`、`benchmarks/supplemental-sources.json`。索引中的本地缓存路径可能只在研究工作区存在；核对历史材料时以记录的固定 URL/revision 为准，重新运行抓取脚本会取得当时远端可用内容，不能替代原始固定版本证据。

产品实际引入的第三方代码独立放在 `src/ocr_workbench/_vendor`，来源锁、补丁和许可证随代码提交；相关许可证另见 `licenses/structure-workflow` 和 `licenses/pdfplumber`。
