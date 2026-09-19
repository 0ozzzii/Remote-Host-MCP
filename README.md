# Remote Host MCP

**AI-native remote Linux host control plane over MCP**  
**面向 AI Agent 的远程 Linux 主机 MCP 控制平面**

Remote Host MCP is a general-purpose [Model Context Protocol (MCP)] server for AI-controlled Linux hosts and containers. It turns host administration primitives—shell execution, files, jobs, terminals, processes, services, transfers, artifacts and SSH—into explicit MCP tools with bounded outputs, durable state, rollback-oriented operations and hardened safety boundaries.

Remote Host MCP 是一个面向 Linux 主机与容器的通用 MCP Server，让 ChatGPT / AI Agent 可以通过结构化 MCP 工具安全地执行主机管理、文件操作、长任务、交互式终端、进程与服务控制、文件传输、结果制品返回和跨主机 SSH 操作。项目重点不是“把 SSH 原样暴露给模型”，而是把远程主机操作拆成**可发现、可约束、可验证、可回滚、适合 Agent 编排**的能力。

> Current development line / 当前开发线：**`0.2.0-alpha.4` / Python `0.2.0a4`**  
> Canonical MCP surface / 标准 MCP 工具面：**65 tools**  
> Python: **>= 3.10** · MCP SDK: **2.2.0**

[中文介绍](#中文) · [English](#english)

---

# 中文

## 1. 项目是什么

Remote Host MCP 是一个“**单主机控制平面**”：它运行在目标 Linux 主机或容器中，通过 MCP Streamable HTTP 向 AI 客户端暴露结构化操作能力。

它适合这些场景：

- 云服务器 / VPS / VM 的 AI 运维与自动化；
- ModelScope DSW、Jupyter 类持久工作空间；
- NAT、家庭服务器、实验室服务器等没有直接公网入口的主机；
- 容器内的自动化控制；
- 长时间计算、编译、仿真、数据处理任务；
- 需要文件上传/下载、结果打包、图片直接返回对话的 Agent 工作流；
- 一台 MCP 主机继续通过预配置 OpenSSH 控制其他 Linux 主机的多跳工作流；
- 需要显式快照、SHA 校验、租约协调、条件等待等 Agent-native 原语的自动化系统。

Remote Host MCP **不等同于 SSH Server**，也不会要求把 SSH 密码或私钥交给模型。对本机操作，MCP 直接调用受控运行时；只有跨主机操作才由 `ssh_*` 工具调用系统 OpenSSH 客户端。

---

## 2. 工作原理 / 实现方式

核心链路：

```text
ChatGPT / AI Agent / MCP Client
              │
              │ MCP Streamable HTTP
              ▼
       Remote Host MCP
              │
   ┌──────────┼─────────────────────────────────────┐
   │          │          │          │               │
   ▼          ▼          ▼          ▼               ▼
Exec/Jobs   Files      PTY      Process/Service   Agent Ops
   │          │          │          │               │
   └──────────┴──────────┴──────────┴───────────────┘
              │
              ▼
        Linux OS / Container
              │
              └── optional system OpenSSH ──> other SSH hosts
```

### 2.1 运行时分层

```text
remote_host_mcp.main
        │
        ▼
remote_host_mcp.app
        │   canonical Remote Host MCP runtime
        ├── compatibility environment adapter
        ├── product branding / health / tool metadata
        ▼
remote_host_mcp.host_server
        ├── shell + filesystem + transfer
        ├── durable jobs + MCP Tasks
        ├── persistent PTY
        ├── process / service / system
        ├── artifacts + SSH
        └── Agent-native operations
```

项目是在已经验证过的执行内核上继续增强，而不是重新发明一套远程 Shell。通用产品身份使用：

- distribution：`remote-host-mcp`
- Python package：`remote_host_mcp`
- CLI：`remote-host-mcp`
- environment：`RHMCP_*`
- protocol：MCP Streamable HTTP

同时保留 `dsw-direct-mcp` / `dsw_direct_mcp.*` / `DSW_MCP_*` 兼容入口，用于旧 DSWD 部署的可逆迁移。

### 2.2 权限模型

**真正的 Shell 权限边界是运行 Remote Host MCP 的 OS 用户身份。**

`RHMCP_ALLOWED_ROOTS` 会约束专用文件、传输、制品、快照等 path-bearing 工具，但它不是对任意 shell/PTY/job 的虚拟沙箱。若服务以 root 身份运行，则 Shell 类能力具有 root 权限；若以普通用户运行，则遵循该用户的 Linux 权限。

因此安装器把“完整主机控制（root profile）”与“当前用户控制”作为显式选择，而不是偷偷提升权限。

### 2.3 文件系统安全实现

专用文件操作使用 hardened path handling：

- allowed-root containment；
- Linux `openat2`（可用时）与稳定 dirfd / `*at` 路径操作；
- mutation 采用临时 staging + 原子发布；
- no-clobber 与 guarded overwrite；
- SHA-256 / inode / destination-entry 检查用于并发修改检测；
- symlink / special file 在高风险路径中按工具语义拒绝或 fail closed。

目标是避免典型的 path traversal、symlink race、destination replacement race 和“写了一半”的文件状态。

### 2.4 长任务与 MCP Tasks

同步 `exec` 适合有界短命令。超过同步请求生命周期的工作应使用 durable Jobs：

- 持久 job metadata；
- heartbeat；
- worker / child identity；
- cursor-addressable logs；
- cancellation / cleanup；
- MCP Tasks 与 durable job 共享同一个 `job_id / taskId`；
- 服务重启后的 recovery 是 observe/reconcile，**不会自动重放任意 shell 命令**。

### 2.5 Persistent PTY

PTY 工具提供真实交互式 Shell 状态：

- open / exec / write / read；
- ANSI/TUI screen；
- resize；
- signal；
- list / close。

OSC 133 用于识别正常命令完成；必要时结合 foreground process-group 状态判断中断流程。PTY 可以跨 MCP transport reconnect 保持，但不会假装在 Remote Host MCP 进程自身重启后仍然存在。

### 2.6 进程与服务

进程身份不是只靠 PID：运行时使用 PID + start ticks，并在可用时使用 pidfd，减少 PID reuse 风险。

服务控制使用经过校验的 `systemctl` argv，不用 shell 拼接。如果目标环境没有 operational systemd manager，服务操作会明确诊断并 fail closed，而不是退回到不受控的进程杀停逻辑。

---

## 3. 安装方式

安装器本身是**中英双语交互式 Linux installer**，并带一个全彩管理面板。无参数运行时先进入面板（默认中文），选择安装后再选择简体中文或 English；安装后也可以通过 `rmcp` 修改界面语言。

### 3.1 从已检出的仓库安装

适合开发、审计和需要固定 commit 的部署：

```bash
git clone https://github.com/0ozzzii/Remote-Host-MCP.git
cd Remote-Host-MCP

# 建议在正式/预发布测试中先 checkout 到明确 tag 或不可变 commit
git checkout <tag-or-commit>

bash install.sh
```

### 3.2 一行 bootstrap

公开仓库入口支持：

```bash
curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh | bash
```

对于 prerelease / candidate，**不要默认跟随移动中的 `main` 或 feature branch**；应固定 release/tag/commit：

```bash
RHMCP_INSTALL_REF=<tag-or-commit> \
  bash <(curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh)
```

bootstrap 需要 `curl` 与 `tar`。项目 Python package 要求 Python **>= 3.10**。

> 若希望看到**交互式管理面板**（安装 / Tunnel / 状态 / 服务 / 诊断 / 卸载），请使用进程替换形式：
>
> ```bash
> bash <(curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh)
> ```
>
> 它保留 TTY，面板才会出现。`curl ... | bash` 的 stdin 是管道，面板会被**安全跳过**并直接进入标准安装流程 —— 两种方式对自动化都完全兼容。

### 3.3 安装器会配置什么

安装流程会明确处理：

1. **语言**：简体中文 / English；
2. **部署布局**：标准 VPS 布局或 persistent-prefix 布局；
3. **运行身份**：root/full-host profile 或 current-user profile；
4. **监听端口**：优先 `8765`，先检测占用；若被占用则显示 owner（可判断时），选择下一个空闲端口或自定义端口；**不会为了抢端口杀已有进程**；
5. **认证模式**：默认 capability URL；高级场景可选 OAuth 2.1 Resource Server；
6. **公网入口**：Public IP/domain reverse proxy 或 Cloudflare Tunnel；
7. **持久目录与 secrets**；
8. **versioned release + `current` symlink**；
9. **服务与 `rmcp` 管理入口**。

### 3.4 两类主要公网接入

#### Cloudflare Tunnel

适合 DSW、NAT、家庭服务器和无公网 IP 主机：

```text
ChatGPT
   │
   ▼
Cloudflare
   │
   ▼
cloudflared tunnel
   │
   ▼
127.0.0.1:<port>
   │
   ▼
Remote Host MCP
```

安装器可以接收 raw Tunnel token，也可以接收完整的 `cloudflared service install <token>` / `--token <token>` 文本；它只解析 token，**不会 eval 或执行粘贴进来的命令文本**。

#### Public VPS / Domain

```text
ChatGPT
   │ HTTPS :443
   ▼
Nginx / Caddy / Apache
   │
   ▼
127.0.0.1:<port>
   │
   ▼
Remote Host MCP
```

Remote Host MCP 本身设计为 loopback-only，不直接监听 `0.0.0.0`。已有 Nginx/Caddy/Apache 优先，安装器不会静默替换已有 443/TLS/ACME 所有权。

### 3.5 安装目录

#### 标准 VPS

```text
/opt/remote-host-mcp/
  current -> releases/<version>-<sha>/
  releases/
/etc/remote-host-mcp/
  rhmcp.env
  install-state.env
  secrets/
/var/lib/remote-host-mcp/
  state/
  runtime/
  backups/
/var/log/remote-host-mcp/
```

#### Persistent prefix / DSW / container

```text
<prefix>/remote-host-mcp/
  current -> releases/<version>-<sha>/
  releases/
  config/
  secrets/
  state/
  runtime/
  logs/
  backups/
```

ModelScope DSW 推荐：

```text
/mnt/workspace/remote-host-mcp
```

这种布局把 release、配置、state、logs、backup 放到持久盘，避免依赖 `/root`、`/tmp` 等易失路径。

---

## 4. 安装后的管理：`rmcp`

安装完成后使用统一管理入口：

```bash
rmcp
```

`rmcp` 用于：

- start / stop / restart / status；
- 查看完整 MCP connection URL；
- capability key rotation；
- diagnostics / logs；
- update check；
- 切换管理界面语言。

版本化 release + `current` symlink 是后续 side-by-side update、health-check、切换与 rollback 的基础，不需要原地覆盖旧 release。

---

## 5. 能实现什么：65 个 MCP 工具

`0.2.0-alpha.4` 的 canonical surface 是 **65 个 tools**。下面按能力域列出完整工具面。

### 5.1 状态、系统与能力发现（3）

- `status`
- `system_info`
- `host_capabilities`

用于读取服务状态、受控系统信息，以及在 Agent 执行前获得 secret-free capability matrix，减少“先试一个命令看看”的猜测式调用。

### 5.2 文件系统（11）

- `list_directory`
- `path_info`
- `read_text_file`
- `read_file_chunk`
- `hash_file`
- `write_text_file`
- `make_directory`
- `move_path`
- `copy_path`
- `remove_path`
- `chmod_path`

覆盖目录浏览、文件读取、分块读取、哈希、写入、目录创建、移动、复制、删除和权限修改。专用文件工具受 `RHMCP_ALLOWED_ROOTS` 约束，并使用 hardened path / atomic publication 设计。

### 5.3 可恢复上传与下载（7）

- `upload_begin`
- `upload_chunk`
- `upload_status`
- `upload_finish`
- `upload_abort`
- `download_info`
- `download_chunk`

支持显式 transfer handle、offset、size / SHA-256 校验，以及完成阶段的原子发布，适合大文件和可恢复传输。

### 5.4 MCP 原生制品与结果交付（4）

- `file_artifact`
- `artifact_info`
- `artifact_preview`
- `artifact_bundle`

其中 `file_artifact` 可以把允许根内的小型 PNG/JPEG/GIF/WebP 直接以 MCP `ImageContent` 返回给客户端；其他小型二进制可作为 embedded resource 返回。默认 inline budget 为 4 MiB，hard ceiling 为 8 MiB；更大的对象使用 chunk download。

`artifact_bundle` 用于生成 bounded、no-clobber ZIP 结果包；拒绝 symlink / special file。

这也支持“**在主机生成截图 → 图片直接出现在 Chat 对话里**”的组合工作流：

```text
exec / exec_argv
    │ 调用主机已有截图工具
    ▼
allowed-root/*.png
    │
    ▼
file_artifact
    │ MCP ImageContent
    ▼
Chat / MCP Client
```

Remote Host MCP 不把 DISPLAY/Xauthority/session credential 变成 MCP 参数；图形会话配置仍由主机运维侧负责。

### 5.5 Agent-native 操作（13）

- `wait_condition`
- `exec_argv`
- `snapshot_create`
- `snapshot_list`
- `snapshot_restore`
- `snapshot_delete`
- `lease_acquire`
- `lease_status`
- `lease_list`
- `lease_release`
- `inspect_paths`
- `file_diff`
- `apply_patch`

这些能力的目标是让 Agent 少做猜测式 shell 调用：

- **`wait_condition`**：对 file/job/process/loopback-port/log 做 bounded wait；port wait 仅 loopback，不是远程端口扫描器；
- **`exec_argv`**：直接 argv 执行，不经过 shell parsing；限制 argv/stdin/output/time，并拒绝显式继承 secret-like 环境变量名；
- **snapshot**：持久 scoped rollback point；创建时限制路径数、节点数、总字节并拒绝 symlink/special file；restore 对每个顶层捕获路径原子发布；
- **lease**：TTL 自动过期的多 Agent 协调原语；lease 不是 authorization；
- **`inspect_paths`**：最多 20 个显式路径，bounded stat/hash/text preview，不递归扫描整台主机；
- **`file_diff` + `apply_patch`**：bounded unified diff + SHA-guarded structured line edit；如果文件在观察后被并发修改，SHA mismatch 会 fail closed。

### 5.6 本机命令与 Durable Jobs（8）

- `exec`
- `job_run`
- `job_start`
- `job_status`
- `job_read`
- `job_cancel`
- `job_list`
- `job_cleanup`

短命令使用 `exec`；需要持久状态、日志游标、取消/恢复观察的长任务使用 durable job 工具。Agent 在不需要 shell 语法时应优先 `exec_argv`。

### 5.7 Persistent PTY / 交互式终端（10）

- `terminal_open`
- `terminal_exec`
- `terminal_write`
- `terminal_read`
- `terminal_status`
- `terminal_screen`
- `terminal_resize`
- `terminal_signal`
- `terminal_list`
- `terminal_close`

适合需要真实 shell state、交互式程序、ANSI/TUI、REPL、长会话和 resize/signal 的任务。

`terminal_screen` 是终端 ANSI/TUI 屏幕，不是 GUI 桌面截图；GUI 图片应使用“截图程序 + `file_artifact`”工作流。

### 5.8 严格 OpenSSH / SCP 跨主机能力（4）

- `ssh_check`
- `ssh_exec`
- `ssh_upload`
- `ssh_download`

模型不直接提交密码、私钥内容、identity-file path 或 known-hosts path。SSH 身份与 host-key trust 由主机运维侧通过系统 OpenSSH config / ssh-agent / host-key store 预配置。

强制边界包括：

```text
BatchMode=yes
PasswordAuthentication=no
KbdInteractiveAuthentication=no
StrictHostKeyChecking=yes
UpdateHostKeys=no
```

`ssh_exec` 的 remote command 通过 SSH stdin 送入 `sh -s --`，避免把任意远程命令文本直接放进本地 `ssh` argv。上传/下载成功还要求 SHA-256 一致。

SSH port forwarding、host-key enrollment、交互式密码/密钥注册和 SSH key lifecycle management 当前刻意不属于 MCP 工具面。

### 5.9 进程（3）

- `process_list`
- `process_info`
- `process_signal`

包含 argv secret redaction，并使用更可靠的进程身份检查来降低 PID reuse 风险。

### 5.10 服务（2）

- `service_status`
- `service_action`

面向 systemd 环境，操作经过校验的 `systemctl` argv；没有 operational systemd manager 时会返回能力诊断并 fail closed。

---

## 6. 认证与网络安全

### Capability mode

默认私有单 owner 模式。客户端使用 capability URL 访问，不提供 unauthenticated public mode。

### OAuth 2.1 Resource Server

可选高级模式。Remote Host MCP 负责验证 bearer token 的：

- signature；
- issuer；
- audience / resource；
- expiry；
- scopes。

登录、PKCE、refresh token、client registration 等属于外部 OAuth/OIDC Authorization Server，不由 Remote Host MCP 自己伪造一套身份系统。

### 公网暴露原则

Remote Host MCP runtime 保持 loopback-only。公网连接通过：

- HTTPS reverse proxy；或
- private/public tunnel（例如 Cloudflare Tunnel）。

不要把 MCP runtime 直接裸绑到公网 `0.0.0.0`。

### Secret 原则

不要提交或写入仓库：

- tokens / API keys；
- Cloudflare Tunnel credentials；
- SSH private keys；
- MCP capability keys；
- connection strings；
- production `.env`。

---

## 7. Deployment profiles

仓库提供三个主要部署 profile：

```text
deployments/generic-linux/
deployments/modelscope-dsw/
deployments/docker/
```

- **Generic Linux**：VPS / VM / workstation；
- **ModelScope DSW**：以 `/mnt/workspace` 为持久真源，适合 side-by-side / blue-green 风格切换；
- **Docker / container**：默认以容器 namespace 为 host boundary，除非显式暴露宿主机 mount / namespace。

### 7.1 三模态分发架构

上面三个 profile 回答的是"**怎么装**"。按节点能力，本仓库还提供三档**模态**，按硬件条件共存、不互相取代：

| 模态 | 适用节点 | 内存门槛 | 依赖 | 能力面 |
|---|---|---|---|---|
| **Tier 1 · 全能直连** | VPS / 云主机 / DSW | ≥ 512 MB | Python ≥ 3.10 + venv | 完整 **65** 个 MCP 工具 + 交互式管理面板 |
| **Tier 2 · 胖中枢 + 瘦代理** | 小内存小鸡、无公网 IP 的节点 | 64 ~ 256 MB | Python ≥ 3.10 | 出站纳管 + 审计上报（**不含**指令下行） |
| **Tier 3 · 受限沙盒微内核** | 只有 Node.js 的容器（翼龙 / Katabump 类） | ~300 MB | **仅 Node.js**，零 npm 依赖 | **5** 个高频工具，单文件 |

#### 一行极速安装（Tier 1）

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh)
```

不带任何参数运行会先进入**全彩交互式管理面板**（安装 / Tunnel / 状态 / 服务 / 诊断 / 卸载）；
带参数（如 `--non-interactive`、`--repair`）或在非交互终端中运行时**完全跳过面板**，
行为与既有自动化 100% 一致。

#### 其余模态的入口

- **Tier 2** 对接规范（`RHMCP_HUB_*` 契约、三端点、指令下行设计提案）：[`docs/architecture/tier2-hub-agent.md`](docs/architecture/tier2-hub-agent.md)
- **Tier 3** 微内核与保姆级部署教程：[`deployments/sandbox-node/README.md`](deployments/sandbox-node/README.md)

---

## 8. 当前 alpha4 的设计方向

`0.2.0-alpha.4` 在原来的 shell / files / transfer / jobs / PTY / process / service 基础上，重点加入 Agent-native operations。

原则不是追求“SSH 功能数量最多”，而是：

- 更少 speculative calls；
- read-before-write；
- bounded inspection；
- structured mutation；
- concurrency detection；
- persistent rollback points；
- multi-Agent coordination；
- result/artifact handoff；
- 对高风险操作 fail closed。

对于不需要 shell 的动作，Agent 应优先 `exec_argv`；进行多文件高风险修改前应优先 snapshot；可能超过同步请求时限的任务应使用 durable jobs。

---

## 9. 开发与验证

开发依赖：

```bash
python -m pip install -e '.[dev]'
pytest
python -m compileall src
```

MCP tool contract 测试会验证：

- 精确 65-tool canonical surface；
- tool title / description / JSON input schema；
- 除原生 content-only `file_artifact` 外的 output schema；
- read-only / destructive / idempotent / open-world annotations；
- 高风险工具显式标注；
- 参数 description 完整性。

项目同时维护 standard validation、installer validation 和 repair validation 三套永久 CI。

---

## 10. 进一步文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — runtime layering、权限边界、Jobs/PTY/process/service/auth architecture
- [`docs/INSTALLER_ARCHITECTURE.md`](docs/INSTALLER_ARCHITECTURE.md) — installer、layout、ingress、update/rollback 模型
- [`docs/AGENT_NATIVE_OPS.md`](docs/AGENT_NATIVE_OPS.md) — alpha4 Agent-native operations 与安全边界
- [`docs/SSH_AND_ARTIFACTS.md`](docs/SSH_AND_ARTIFACTS.md) — OpenSSH/SCP 与原生 MCP file/image return
- [`docs/AUDIT_CLOSURE_20260913.md`](docs/AUDIT_CLOSURE_20260913.md) — security/correctness audit closure
- [`docs/ROLLBACK_AND_COMPATIBILITY.md`](docs/ROLLBACK_AND_COMPATIBILITY.md) — migration / compatibility / rollback
- [`docs/DSW_CANARY_RUNBOOK.md`](docs/DSW_CANARY_RUNBOOK.md) — ModelScope DSW canary 方法

---

# English

## 1. What is Remote Host MCP?

Remote Host MCP is a **single-host MCP control plane** for Linux hosts and containers. It runs on the target machine and exposes explicit host-management capabilities through MCP Streamable HTTP.

It is designed for:

- AI operations on VPS, cloud VMs and workstations;
- ModelScope DSW and Jupyter-like persistent workspaces;
- NAT/home/lab servers without direct public ingress;
- container automation;
- long-running builds, simulation, computation and data-processing jobs;
- Agent workflows that need upload/download, result bundling, or native image return to chat;
- using one MCP-controlled host as an operator-managed OpenSSH client for other Linux hosts;
- automation that benefits from snapshots, SHA-based concurrency guards, leases and bounded waits.

Remote Host MCP is **not an SSH server wrapped in MCP**. Local-host operations execute directly through the MCP runtime. The `ssh_*` tools are only for cross-host control and call the system OpenSSH client using operator-preconfigured trust and identity.

---

## 2. How it works

```text
ChatGPT / AI Agent / MCP Client
              │
              │ MCP Streamable HTTP
              ▼
       Remote Host MCP
              │
   ┌──────────┼─────────────────────────────────────┐
   │          │          │          │               │
   ▼          ▼          ▼          ▼               ▼
Exec/Jobs   Files      PTY      Process/Service   Agent Ops
   │          │          │          │               │
   └──────────┴──────────┴──────────┴───────────────┘
              │
              ▼
        Linux OS / Container
              │
              └── optional system OpenSSH ──> other SSH hosts
```

### 2.1 Runtime layering

```text
remote_host_mcp.main
        │
        ▼
remote_host_mcp.app
        │   canonical Remote Host MCP runtime
        ├── compatibility environment adapter
        ├── product branding / health / tool metadata
        ▼
remote_host_mcp.host_server
        ├── shell + filesystem + transfer
        ├── durable jobs + MCP Tasks
        ├── persistent PTY
        ├── process / service / system
        ├── artifacts + SSH
        └── Agent-native operations
```

Canonical product identity:

- distribution: `remote-host-mcp`
- Python package: `remote_host_mcp`
- CLI: `remote-host-mcp`
- environment variables: `RHMCP_*`
- protocol: MCP Streamable HTTP

Legacy `dsw-direct-mcp`, `dsw_direct_mcp.*` and `DSW_MCP_*` compatibility remains available for reversible migration of older DSWD deployments.

### 2.2 Authority model

The real shell authority boundary is the **OS identity running Remote Host MCP**.

`RHMCP_ALLOWED_ROOTS` constrains dedicated path-bearing tools such as filesystem, transfer, artifact and snapshot operations. It is not a virtual sandbox around arbitrary shell, PTY or job execution. A root service has root shell authority; a user service has that user's Linux permissions.

### 2.3 Hardened filesystem implementation

Dedicated filesystem operations use:

- allowed-root containment;
- Linux `openat2` when available;
- stable dirfd / `*at` mutation paths;
- staging plus atomic publication;
- no-clobber and guarded-overwrite semantics;
- SHA-256 / inode / destination-entry checks for concurrent modification;
- fail-closed handling of unsafe symlink/special-file cases where required.

The goal is to avoid path traversal, symlink races, destination replacement races and partially published files.

### 2.4 Durable jobs and MCP Tasks

Use synchronous `exec` for bounded short commands. Long-running work can use durable Jobs with persistent metadata, heartbeat, worker/child identity, cursor-addressable logs, cancellation and cleanup.

MCP Tasks reuses the same `job_id` as `taskId`. Restart recovery observes/reconciles existing state and **never automatically replays arbitrary shell commands**.

### 2.5 Persistent PTY

PTY tools provide real interactive shell state, ANSI/TUI screen capture, resize, raw writes and signals. OSC 133 is used for exact normal command completion, with foreground process-group state as a fallback for interrupted flows.

PTY sessions survive transport reconnects, but are not claimed to survive a Remote Host MCP process restart.

### 2.6 Process and service control

Process identity uses PID plus start ticks, and pidfd where available, to reduce PID-reuse hazards.

Service control uses validated `systemctl` argv without shell expansion. If no operational systemd manager is available, the service surface reports that capability state and fails closed instead of falling back to broad process-killing behavior.

---

## 3. Installation

The installer is a **bilingual interactive Linux installer** with a full-colour management panel. With no arguments it opens the panel first (Chinese by default); after choosing to install you then pick Simplified Chinese or English, and the language can later be changed from `rmcp`.

### 3.1 Install from a checked-out repository

Recommended for development, review and immutable-commit deployments:

```bash
git clone https://github.com/0ozzzii/Remote-Host-MCP.git
cd Remote-Host-MCP

# For production/prerelease testing, pin an immutable tag or commit first.
git checkout <tag-or-commit>

bash install.sh
```

### 3.2 Bootstrap entry

```bash
curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh | bash
```

For prerelease/candidate testing, pin the intended release/tag/commit rather than assuming a moving branch:

```bash
RHMCP_INSTALL_REF=<tag-or-commit> \
  bash <(curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh)
```

The bootstrap requires `curl` and `tar`. The Python package requires **Python >= 3.10**.

> To see the **interactive management panel** (install / tunnel / status / service / diagnose / uninstall), use process substitution:
>
> ```bash
> bash <(curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh)
> ```
>
> It preserves the TTY, which is what makes the panel appear. With `curl ... | bash` stdin is a pipe, so the panel is **skipped safely** and the standard install flow runs. Both forms are fully compatible with automation.

### 3.3 What the installer configures

The interactive flow covers:

1. language;
2. standard VPS vs persistent-prefix layout;
3. root/full-host vs current-user authority profile;
4. preferred port `8765`, with occupied-port detection and safe alternate/custom selection—**it never kills the existing port owner**;
5. capability authentication by default or optional OAuth 2.1 Resource Server mode;
6. public-domain/reverse-proxy ingress or Cloudflare Tunnel;
7. persistent config/state/log/backup and protected secret storage;
8. immutable versioned releases plus a `current` symlink;
9. service setup and the permanent `rmcp` management entrypoint.

### 3.4 Ingress models

#### Cloudflare Tunnel

```text
ChatGPT -> Cloudflare -> cloudflared -> 127.0.0.1:<port> -> Remote Host MCP
```

The installer may accept either a raw Tunnel token or a pasted `cloudflared service install <token>` / `--token <token>` command. It parses the token only; pasted command text is never `eval`'d or executed.

#### Public VPS / domain

```text
ChatGPT -> HTTPS :443 -> Nginx/Caddy/Apache -> 127.0.0.1:<port> -> Remote Host MCP
```

The runtime stays loopback-only and does not bind directly to `0.0.0.0`. Existing Nginx/Caddy/Apache ownership wins; the installer does not silently replace an existing TLS/ACME setup.

### 3.5 Installation layouts

Standard VPS:

```text
/opt/remote-host-mcp/
  current -> releases/<version>-<sha>/
  releases/
/etc/remote-host-mcp/
  rhmcp.env
  install-state.env
  secrets/
/var/lib/remote-host-mcp/
  state/
  runtime/
  backups/
/var/log/remote-host-mcp/
```

Persistent prefix / DSW / container:

```text
<prefix>/remote-host-mcp/
  current -> releases/<version>-<sha>/
  releases/
  config/
  secrets/
  state/
  runtime/
  logs/
  backups/
```

Recommended ModelScope DSW root:

```text
/mnt/workspace/remote-host-mcp
```

---

## 4. Post-install management: `rmcp`

```bash
rmcp
```

The permanent management UX provides:

- start / stop / restart / status;
- complete MCP connection information;
- capability-key rotation;
- diagnostics and logs;
- update checking;
- runtime language selection.

The versioned-release + `current` symlink model is the basis for side-by-side candidate installation, health checks, cutover and rollback without overwriting the previous release.

---

## 5. Capabilities: 65 canonical MCP tools

### 5.1 Status, system and capability discovery (3)

`status`, `system_info`, `host_capabilities`

### 5.2 Filesystem (11)

`list_directory`, `path_info`, `read_text_file`, `read_file_chunk`, `hash_file`, `write_text_file`, `make_directory`, `move_path`, `copy_path`, `remove_path`, `chmod_path`

### 5.3 Resumable transfer (7)

`upload_begin`, `upload_chunk`, `upload_status`, `upload_finish`, `upload_abort`, `download_info`, `download_chunk`

Transfers use explicit handles, offsets, size/SHA-256 validation and atomic final publication.

### 5.4 Native MCP artifacts and result handoff (4)

`file_artifact`, `artifact_info`, `artifact_preview`, `artifact_bundle`

`file_artifact` returns small PNG/JPEG/GIF/WebP files from allowed roots as native MCP `ImageContent`; other small binary files can be returned as embedded resources. The default inline budget is 4 MiB with an 8 MiB hard ceiling. Larger data uses chunked download.

A GUI screenshot is intentionally composed as:

```text
exec / exec_argv -> configured screenshot utility -> allowed-root PNG -> file_artifact -> MCP ImageContent
```

DISPLAY/Xauthority/session credentials remain an operator-side concern and are not exposed as MCP parameters.

### 5.5 Agent-native operations (13)

`wait_condition`, `exec_argv`, `snapshot_create`, `snapshot_list`, `snapshot_restore`, `snapshot_delete`, `lease_acquire`, `lease_status`, `lease_list`, `lease_release`, `inspect_paths`, `file_diff`, `apply_patch`

Key semantics:

- bounded file/job/process/loopback-port/log waits;
- shell-free argv execution with bounded input/output/time and restricted environment inheritance;
- persistent scoped rollback snapshots;
- automatically expiring TTL coordination leases;
- explicit-path-only bounded inspection (up to 20 paths, no recursive host scan);
- bounded unified diff;
- SHA-guarded structured line edits that fail closed on concurrent modification.

### 5.6 Local execution and durable jobs (8)

`exec`, `job_run`, `job_start`, `job_status`, `job_read`, `job_cancel`, `job_list`, `job_cleanup`

Prefer `exec_argv` when shell syntax is unnecessary and durable jobs for work that may outlive a synchronous MCP request.

### 5.7 Persistent PTY / terminal (10)

`terminal_open`, `terminal_exec`, `terminal_write`, `terminal_read`, `terminal_status`, `terminal_screen`, `terminal_resize`, `terminal_signal`, `terminal_list`, `terminal_close`

`terminal_screen` represents an ANSI/TUI terminal screen, not a graphical desktop screenshot.

### 5.8 Strict OpenSSH / SCP cross-host tools (4)

`ssh_check`, `ssh_exec`, `ssh_upload`, `ssh_download`

No password, private-key contents, identity-file path or known-hosts path is accepted as an MCP argument. OpenSSH identity and trust are preconfigured by the operator through system OpenSSH configuration, ssh-agent and the host-key store.

Enforced options include:

```text
BatchMode=yes
PasswordAuthentication=no
KbdInteractiveAuthentication=no
StrictHostKeyChecking=yes
UpdateHostKeys=no
```

Remote command text is fed through SSH stdin to `sh -s --` instead of being placed in the local `ssh` argv. File-transfer success requires SHA-256 agreement.

SSH port forwarding, host-key enrollment, password prompts, interactive key enrollment and SSH-key lifecycle management are intentionally outside the current tool surface.

### 5.9 Processes (3)

`process_list`, `process_info`, `process_signal`

Includes argv secret redaction and stronger process identity handling to reduce PID-reuse risks.

### 5.10 Services (2)

`service_status`, `service_action`

Designed for systemd-backed environments using validated `systemctl` argv. Environments without an operational systemd manager are diagnosed and handled fail-closed.

---

## 6. Authentication and network security

### Capability mode

The default private single-owner model. Access uses a capability URL; there is no unauthenticated public installer mode.

### OAuth 2.1 Resource Server

Optional advanced mode. Remote Host MCP validates bearer signature, issuer, audience/resource, expiry and scopes. Login, PKCE, refresh tokens and client registration belong to an external OAuth/OIDC Authorization Server.

### Exposure model

The runtime remains loopback-only. Public ingress belongs behind HTTPS reverse proxying or a tunnel such as Cloudflare Tunnel.

Do not expose the raw MCP runtime directly on `0.0.0.0`.

### Secrets

Never commit or publish tokens, API keys, Tunnel credentials, SSH private keys, MCP capability keys, connection strings or production `.env` files.

---

## 7. Deployment profiles

```text
deployments/generic-linux/
deployments/modelscope-dsw/
deployments/docker/
```

- **Generic Linux** — VPS, VM, workstation.
- **ModelScope DSW** — `/mnt/workspace` persistence and side-by-side/blue-green style cutover.
- **Docker/container** — the container namespace is the host boundary unless host mounts/namespaces are explicitly exposed.

### 7.1 Three-tier distribution

The profiles above answer "**how to install**". The repository additionally ships three **tiers** keyed to node capability; they coexist rather than replace each other:

| Tier | Target node | Memory floor | Dependency | Surface |
|---|---|---|---|---|
| **Tier 1 · Full direct** | VPS / VM / DSW | ≥ 512 MB | Python ≥ 3.10 + venv | All **65** MCP tools + interactive management panel |
| **Tier 2 · Fat hub + thin agent** | Small hosts, no public IP | 64 ~ 256 MB | Python ≥ 3.10 | Outbound enrolment + audit reporting (**no** command downlink) |
| **Tier 3 · Restricted sandbox micro-kernel** | Node-only containers (Pterodactyl / Katabump class) | ~300 MB | **Node.js only**, zero npm | **5** high-frequency tools, single file |

#### One-line install (Tier 1)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh)
```

Run with no arguments, it first opens a **full-colour interactive management panel** (install / tunnel / status / service / diagnose / uninstall). Run with an argument (such as `--non-interactive` or `--repair`), or on a non-interactive terminal, the panel is **skipped entirely** and behaviour is byte-identical to the existing automation.

#### Entry points for the other tiers

- **Tier 2** contract and design notes (`RHMCP_HUB_*`, three endpoints, command-downlink proposal): [`docs/architecture/tier2-hub-agent.md`](docs/architecture/tier2-hub-agent.md)
- **Tier 3** micro-kernel and step-by-step deployment guide: [`deployments/sandbox-node/README.md`](deployments/sandbox-node/README.md)

---

## 8. Alpha4 design direction

`0.2.0-alpha.4` adds an Agent-oriented control layer on top of the existing shell, filesystem, transfer, jobs, PTY, process, service, artifact and SSH surfaces.

The goal is not maximum SSH feature parity. The goal is:

- fewer speculative calls;
- read-before-write workflows;
- bounded inspection;
- structured mutation;
- concurrency detection;
- persistent rollback points;
- multi-Agent coordination;
- explicit result/artifact handoff;
- fail-closed handling of high-risk operations.

Agents should prefer `exec_argv` when shell syntax is unnecessary, take snapshots before risky multi-file mutation, and use durable jobs for work that may exceed synchronous request deadlines.

---

## 9. Development and validation

```bash
python -m pip install -e '.[dev]'
pytest
python -m compileall src
```

The MCP contract tests verify the exact 65-tool canonical surface, titles/descriptions, JSON input schemas, output schemas (except native content-only `file_artifact`), tool annotations, destructive/read-only/open-world metadata and parameter documentation.

The repository maintains permanent standard validation, installer validation and repair validation workflows.

---

## 10. Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — runtime layering, authority, Jobs/PTY/process/service/auth architecture
- [`docs/INSTALLER_ARCHITECTURE.md`](docs/INSTALLER_ARCHITECTURE.md) — installer, layouts, ingress and update/rollback model
- [`docs/AGENT_NATIVE_OPS.md`](docs/AGENT_NATIVE_OPS.md) — alpha4 Agent-native operations and safety boundaries
- [`docs/SSH_AND_ARTIFACTS.md`](docs/SSH_AND_ARTIFACTS.md) — OpenSSH/SCP and native MCP file/image return
- [`docs/AUDIT_CLOSURE_20260913.md`](docs/AUDIT_CLOSURE_20260913.md) — security/correctness audit closure
- [`docs/ROLLBACK_AND_COMPATIBILITY.md`](docs/ROLLBACK_AND_COMPATIBILITY.md) — migration, compatibility and rollback
- [`docs/DSW_CANARY_RUNBOOK.md`](docs/DSW_CANARY_RUNBOOK.md) — ModelScope DSW canary workflow
