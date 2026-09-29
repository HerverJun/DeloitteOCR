# 候选程序切换与数据回退

程序包与用户数据使用不同目录。候选包的 `launch.cmd` 默认使用包内 `data`，正式切换时由操作员设置 `WORKBENCH_INSTALL_DATA_DIR` 指向包外的固定用户数据目录；OCR 与分类原生进程读取启动器生成的同一受信数据指针。端口可通过 `WORKBENCH_PORT` 指定。不要同时用两个候选程序挂载同一数据目录。

切换前停止平台、OCR 与分类服务，确认正在运行的任务已完成或安全取消。按 [platform-backup.md](platform-backup.md) 在同一停服窗口备份平台、OCR、分类三个数据目录，并分别验证清单。将备份组及旧程序包保留在新程序包之外。新程序包先运行包内 `verify.cmd`，用自带 Python 完成全文件校验；不修改旧包或原数据。

设置数据目录并启动新包：

```powershell
$env:WORKBENCH_INSTALL_DATA_DIR = $userData
$env:WORKBENCH_PORT = '8765'
& (Join-Path $newCandidate 'launch.cmd')
```

检查工作空间、三应用安装/启动、旧项目、固定资料集、任务终态及托管成果下载；仅在实际可用的模型、GPU 和业务材料下执行相应检查。完成这些检查前保留旧包和升级前备份。

若新包失败，先停掉新包及其原生子进程。不要覆盖现有用户目录。将升级前备份的三个组件分别恢复到另一全新 `$rollbackData`，并用旧包指向该目录启动：

```powershell
$env:WORKBENCH_INSTALL_DATA_DIR = $rollbackData
$env:WORKBENCH_PORT = '8765'
& (Join-Path $oldCandidate 'launch.cmd')
```

回退后重新核对工作空间、原件、映射和成果哈希。失败时的新数据目录与新包都保留供诊断；升级前原用户目录仍不覆盖。该流程只能保证回到备份时的数据状态，不能承诺自动合并升级期间新增的写入。不同数据库 schema 版本的兼容性须随固定候选单独验证。
