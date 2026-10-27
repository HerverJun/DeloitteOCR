# Clear 离线部署与迁移

离线目录的统一入口是 `Clear.cmd`，程序、运行时、模型、Node/DSH、PostgreSQL 和转换工具在同一个 `Clear` 文件夹。业务 DATA、凭据、缓存、临时、备份与 PostgreSQL 集群在初始化时选择，必须位于普通本地固定磁盘；不支持网络共享、映射网络盘或任意目录链接。本机开发与构建仍限定 F 盘。

GitHub 提供源码、依赖锁和构建工具，不存放大型模型及运行库。仅克隆源码不能代替完整离线包。构建器 `products/Clear/platform/scripts/package_portable.py` 使用明确输入映射和历史来源哈希；缺项报告为阻断，不会启动时下载。输入锁与来源报告属于构建证据，运行不读取旧工作目录。

## 首次使用

在 Windows x64 服务器解压完整 `Clear` 目录。从该目录执行以下命令；将示例 DATA 替换为实际选择的位置。本机示例均位于 F 盘。

```powershell
.\Clear.cmd init --mode single --data "F:\Clear资料\DATA" --portal-port 8889
.\Clear.cmd --config "F:\Clear资料\DATA\clear.install.json" doctor
.\Clear.cmd --config "F:\Clear资料\DATA\clear.install.json" verify
.\Clear.cmd --config "F:\Clear资料\DATA\clear.install.json" qualify
.\Clear.cmd --config "F:\Clear资料\DATA\clear.install.json" start
```

初始化生成版本为 1 的 `clear.install.json`，包含本机实际路径、端口、资源绑定和独立安装 ID。程序目录可只读；DATA 及其他写入根不能与程序目录重叠。中文和空格目录可用，Paddle 临时目录需要 ASCII，默认自动选取并独立登记。不要复制他人的配置、服务 token、运行 PID 或 GPU 资格作为本机就绪证据。

`verify` 检查完整文件闭包与 SHA256；启动复用相同清单和 NTFS 文件身份的有效检查，文件变化会重新检查。`doctor` 报告依赖位置、磁盘和显卡情况；看到显卡不表示引擎已通过推理。`qualify` 使用小型合成资料完成真实离线执行和成果读回，仅证明运行路径，不代表业务准确率。可用 `qualify --engine ppocr` 单独诊断；部分通过不会把整包标为通过。隔离候选调试才使用 `start --allow-unqualified`。

## 多人局域网 HTTP

门户、私有执行服务、数据库和业务资料均在同一台 Windows 服务器。浏览器客户端只连接门户。初始化时输入管理员密码；凭据使用运行服务的 Windows 账户 DPAPI 加密，源代码与离线目录不包含密码。

```powershell
.\Clear.cmd init --mode multi --data "F:\Clear服务\DATA" --transport lan-http --listen 192.168.1.8 --public-origin http://192.168.1.8:8889 --portal-port 8889 --worker-port 8890 --postgres-port 55439
.\Clear.cmd --config "F:\Clear服务\DATA\clear.install.json" qualify
.\Clear.cmd --config "F:\Clear服务\DATA\clear.install.json" start
```

把 `192.168.1.8` 换为服务器实际局域网 IP。HTTP 明文传输账号、会话及业务内容，使用者须了解此特性。该模式必须明确配置，Cookie 保留 HttpOnly、SameSite，HTTP 下 Secure 为 false；登录、权限、CSRF、精确 Host/Origin 检查继续生效。私有 worker 与 PostgreSQL 只监听 127.0.0.1，不向局域网开放。需要加密时选择 `https` 并配置证书和私钥。单机模式始终只允许回环地址，局域网共享必须使用多人模式。

HTTPS 的证书和私钥须位于显式选择的凭据根；首次初始化可先把文件放在独立的凭据目录，再用 `--secrets 该目录 --certificate 目录内证书 --private-key 目录内私钥`。初始化会核对内容哈希，诊断与启动继续检查同一对文件；源程序包不携带目标机证书或私钥。

新集群的迁移由 `init` 明确执行；已有数据库升级使用 `migrate`，启动不会静默迁移或回退到其他库。初始化中断可以对相同配置使用 `init --resume`。

## 停止、备份和搬迁

`status` 只报告当前安装登记的进程；`stop` 在多人模式只停门户，执行服务继续运行。`stop --all` 先阻止新写入并等待现有任务排空，再停止自有执行服务和数据库。不能确认退出或存在待核实任务时，保留服务并报告原因，不按陌生 PID 强制终止进程。

```powershell
.\Clear.cmd --config "F:\Clear服务\DATA\clear.install.json" status
.\Clear.cmd --config "F:\Clear服务\DATA\clear.install.json" backup
.\Clear.cmd --config "F:\Clear服务\DATA\clear.install.json" stop --all
.\Clear.cmd --config "F:\Clear服务\DATA\clear.install.json" restore --backup "F:\Clear服务\DATA\backups\备份名称" --destination "F:\Clear迁移\新DATA"
```

单机备份需要先停止服务；多人备份复用一致性写入屏障、PostgreSQL dump、SQLite snapshot 和业务文件校验。恢复总是创建新 DATA，多人模式也创建新数据库，原环境保留。恢复命令输出新配置路径，后续命令使用该路径。旧原件、请求 ID、修订、哈希、结果快照和模型绑定保持；只按已知版本及字段迁移运行路径。旧 DATA 路径的读取通过登记映射解析，旧模型或检查点不自动替换为新默认值。中断任务必须核实后继续；目标账户重新填写外部 API 凭据。

仅搬动同一份程序目录时，先停止全部服务，将整个 `Clear` 目录搬到新位置，然后从新位置运行 `Clear.cmd --config 原配置路径 rebind`。该命令要求程序内容清单一致，重建本机绑定、token 和安装身份，失效旧资格；随后重新 `qualify`。不要对业务 JSON 全局替换盘符。

独立 Windows、实际跨盘、不同 GPU/驱动、跨 Windows 账户 DPAPI 的验收必须在相应目标环境执行。随包真实推理脚本可用于新机验收，未执行的项目必须保留为待验收。

新机可运行 `app\platform\scripts\accept_portable_machine.ps1 -DataRoot 新建DATA路径`，连续完成初始化、诊断、完整校验、全部引擎真实任务、启停及备份，并保存机器与账户证据。加 `-ReadOnlyProgram` 可临时设置程序目录只读 ACL，验收后恢复原 ACL。多人验收加 `-Mode multi -Listen 服务器局域网IP`。脚本只接受新 DATA，跨机器与跨账户结果须按报告中的实际机器信息确认，浏览器权限验收仍单独执行。
