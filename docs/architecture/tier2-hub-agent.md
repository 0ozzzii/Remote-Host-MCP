# Tier 2 · 胖中枢 + 瘦代理（CF-MCP-HUB 对接规范）

> **适用场景**：64 MB ~ 256 MB 的小内存主机、无公网 IP 的内网机器、只想被集中
> 纳管而不想自己暴露服务的节点。
>
> **状态说明**：本文分两部分 —— **第一节是仓库中已实现的契约**（可直接对接），
> **第三节是尚未实现的设计提案**（指令下行）。请勿把提案当作现状使用。

## 一、已实现的契约（`RHMCP_HUB_*`）

以下契约由 Hub 侧的安装脚本写入 `rhmcp.env`，**变量名是固定契约，不可重命名**
（实现见 `src/remote_host_mcp/hub_settings.py`）。

### 1.1 方向：代理主动出站

关键设计：**所有流量都是代理出站（outbound）发起的**。这带来两个直接好处：

- 节点**不需要公网 IP、不需要开入站端口**，穿透 NAT / 云安全组无压力；
- 执行服务器**永远不依赖 Hub**：Hub 挂了、配置写错了，本地服务和审计照常工作。

### 1.2 环境变量

| 变量 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `RHMCP_HUB_BASE_URL` | https URL | 无 | Hub 基地址。**无 fragment**，自动去掉结尾 `/`。 |
| `RHMCP_HUB_HOST_ID` | string | 无 | 本节点在中枢里的标识。 |
| `RHMCP_HUB_AGENT_KEY` | string | 无 | 代理密钥，**必须以 `agt_` 开头**，否则视为无效并忽略。 |
| `RHMCP_HUB_REPORT_LEVEL` | 枚举 | `tool` | `meta` / `tool` / `full`，上报粒度。 |
| `RHMCP_HUB_REDACT_ENABLED` | bool | `true` | 上报前是否脱敏。 |
| `RHMCP_HUB_OUTPUT_MODE` | 枚举 | `preview` | `r2` / `local` / `preview`，产物落盘策略。 |
| `RHMCP_HUB_REPORT_ENABLED` | bool | `true` | 上报总开关。 |
| `RHMCP_HUB_AUDIT_ENABLED` | bool | `true` | 本地审计总开关。 |
| `RHMCP_HUB_AUDIT_LOG_PATH` | 绝对路径 | `<state_dir>/audit/calls.jsonl` | 审计日志位置，**必须是绝对路径**。 |
| `RHMCP_HUB_AUDIT_MAX_BYTES` | int | `32 MiB` | 单文件轮转阈值，范围 64 KiB ~ 4 GiB。 |
| `RHMCP_HUB_AUDIT_KEEP_FILES` | int | `3` | 保留代数，范围 0 ~ 100。 |
| `RHMCP_HUB_REPORT_INTERVAL_SECONDS` | int | `30` | 上报间隔，范围 1 ~ 3600。 |
| `RHMCP_HUB_REPORT_BATCH_SIZE` | int | `50` | 单批事件数，范围 1 ~ 200。 |
| `RHMCP_HUB_REPORT_MAX_BATCH` | int | `200` | 单批上限，范围 1 ~ 1000。 |
| `RHMCP_HUB_HEARTBEAT_SECONDS` | int | `60` | 心跳间隔，范围 5 ~ 86400。 |

布尔值接受 `1/true/yes/on` 与 `0/false/no/off`（大小写不敏感）。

### 1.3 三个端点

全部走 HTTPS POST，鉴权头固定为 `Authorization: Bearer <RHMCP_HUB_AGENT_KEY>`：

| 端点 | 请求体 | 用途 |
|---|---|---|
| `/agent/v1/report` | `{"events": [...]}` | 批量上报审计事件 |
| `/agent/v1/heartbeat` | `{"version": "<版本号>"}` | 心跳保活 |
| `/agent/v1/config` | — | 拉取中枢下发的配置 |

### 1.4 两种读取语义（重要）

同一份配置被两个消费者以**不同严格度**读取：

| 消费者 | `strict` | 行为 |
|---|---|---|
| RHMCP 执行服务器 | `False` | 读不通就**警告并用默认值**，绝不因为 Hub 块缺失/写错而拒绝启动 |
| sidecar 上报代理 | `True` | 配置错误**大声失败**（抛 `HubConfigError`），因为它的唯一职责就是上报 |

设计意图：**中枢是可选增强，不是运行前提**。装错了 Hub 变量，不应该让一台已经
在正常提供工具的主机停摆。

### 1.5 上报的可靠性

- 本地审计先落 `calls.jsonl`，**批次被 Hub 确认后才推进游标**；
- 因此崩溃或 POST 失败都会**重放**，不丢事件（配合日志轮转的 tail 感知）；
- 审计日志按 `AUDIT_MAX_BYTES` 轮转、按 `AUDIT_KEEP_FILES` 保留，默认配置下
  日志总量上限约 **128 MiB**，与运行时长无关。

### 1.6 最小可用配置

```bash
RHMCP_HUB_BASE_URL=https://hub.example.com
RHMCP_HUB_HOST_ID=node-tokyo-01
RHMCP_HUB_AGENT_KEY=agt_xxxxxxxxxxxxxxxx
```

三项齐备（`base_url` + `agent_key` + `host_id`）才算 `configured`；缺任意一项，
上报静默停用，其余功能不受影响。

## 二、当前能力边界

| 能力 | 方向 | 状态 |
|---|---|---|
| 审计事件批量上报 | 代理 → 中枢 | ✅ 已实现 |
| 心跳保活 | 代理 → 中枢 | ✅ 已实现 |
| 拉取中枢下发的配置 | 代理 → 中枢 | ✅ 已实现 |
| **中枢主动下发工具调用** | 中枢 → 代理 | ❌ **未实现** |

**结论（请务必知悉）**：当前的 Tier 2 是**"可观测 + 可纳管配置"**，还**不是**
"可统一调度"。中枢能看到各节点的调用审计，也能下发配置，但**不能**从中枢
直接对某个节点发起 `exec` 之类的工具调用。

## 三、指令下行设计提案（未实现）

> ⚠️ 以下为**设计提案**，仓库中尚无对应实现。落地前请先评审。

要实现"中枢统一下发工具调用"，代理必须能在**不开放入站端口**的前提下收到指令。
两个候选方案：

### 方案 A · 长轮询（推荐）

复用现有的出站 HTTP 栈，新增一个 `/agent/v1/commands` 端点：

```text
代理: POST /agent/v1/commands/poll   (长期挂起，服务端最多持有 N 秒)
中枢: 200 {"commands": [{id, tool, arguments}]}   或   204 无指令
代理: POST /agent/v1/commands/{id}/result  {"content": [...], "is_error": false}
```

- **优点**：零新增依赖，与现有鉴权和错误模型一致；Cloudflare Workers
  无需 Durable Objects；代理侧只是多一个循环。
- **代价**：指令延迟 = 轮询周期（可用长挂起压到秒级）；Worker 的 CPU/时长
  配额需要留意。
- **适用**：指令频率不高、能接受秒级延迟的纳管场景。

### 方案 B · WebSocket 反向注册

代理启动时向中枢建立一条 WebSocket 长连接并注册 `host_id`，之后指令沿该连接下行：

```text
代理 → 中枢:  wss://hub.example.com/agent/v1/ws   (Authorization: Bearer agt_...)
代理 → 中枢:  {"type":"register","host_id":"node-tokyo-01","version":"..."}
中枢 → 代理:  {"type":"command","id":"...","tool":"exec","arguments":{...}}
代理 → 中枢:  {"type":"result","id":"...","content":[...],"is_error":false}
```

- **优点**：指令**实时**下发，无需轮询；天然的双向流。
- **代价**：Cloudflare Workers 上维持 WebSocket 需要 **Durable Objects**
  （普通 Worker 请求结束后不能保持连接），引入状态与计费复杂度；代理侧需要
  断线重连、心跳、背压等一整套长连接治理逻辑。
- **适用**：需要实时交互（终端流、长任务进度推送）的场景。

### 取舍建议

**先做方案 A**。当前 Tier 2 的目标节点是 64 MB ~ 256 MB 的小鸡，
长轮询的额外开销（一个挂起的 HTTP 请求）远小于长连接治理的复杂度；
真的需要实时流时再上方案 B。

无论选哪个方案，都必须沿用现有约定：

- **鉴权**恒为 `Authorization: Bearer agt_...`，复用 `agent_key`；
- **执行服务器不因中枢不可用而停摆** —— 指令通道必须是可选的旁路；
- **审计与脱敏照旧** —— 中枢下发的调用和被调用的调用走同一套审计；
- **回传仍受 `REDACT_ENABLED` 约束**，不要在结果通道绕过脱敏。

## 四、典型的 Tier 分工

| Tier | 适用 | 内存 | 能力 |
|---|---|---|---|
| 1 | VPS / 云主机 / DSW | ≥ 512 MB | 完整 65 工具，本地直连 |
| **2** | **小内存节点 / 无公网 IP** | **64 ~ 256 MB** | **本文档：出站纳管 + 审计上报** |
| 3 | 受限沙盒（仅 Node） | ~300 MB | 5 工具微内核，见 [`../../deployments/sandbox-node/README.md`](../../deployments/sandbox-node/README.md) |

Tier 2 与 Tier 3 可以叠加：Node 微内核负责执行，Hub 负责纳管与观测。

## 五、安全边界

- `agent_key` 是**凭据**：它授权中枢接收本节点的全部审计内容。像密码一样保管，
  不要写进日志、不要提交进仓库；
- `RHMCP_HUB_*` 变量由安装器**原样透传**写入 `rhmcp.env`（安装器不解释其语义），
  变量值禁止包含换行；
- 上报内容受 `RHMCP_HUB_REDACT_ENABLED` 约束，默认开启。关闭脱敏等于把本机的
  命令与输出**原文**送往中枢，请确认中枢侧的信任模型后再动；
- `RHMCP_HUB_BASE_URL` 强制 https，且拒绝带 fragment 的 URL。

## 参考实现位置

| 内容 | 路径 |
|---|---|
| 配置解析与校验 | `src/remote_host_mcp/hub_settings.py` |
| 上报代理（三端点客户端） | `src/remote_host_mcp/report_agent.py` |
| 审计中间件挂载 | `src/remote_host_mcp/host_server.py` |
| 安装器的透传逻辑 | `installer/install.sh` 的 `write_hub_runtime_env()` |
| 契约测试 | `tests/test_hub_settings.py` |
