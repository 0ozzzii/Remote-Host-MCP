# 受限沙盒 Node.js 微内核（Tier 3）

面向**只有 Node.js、没有可用的 Python、内存极小**的容器沙盒 —— 典型如法国
Katabump、Pterodactyl（翼龙）面板的 Node.js egg。

## 为什么需要它

完整的 Remote Host MCP 运行时依赖 Python ≥ 3.10。翼龙类沙盒通常：

- 没有 root，装不了系统包；
- 没有可用的 Python，或版本低于 3.10；
- 无法执行 `npm install`（无外网 / 磁盘配额极紧）；
- 内存常被限制在 256 MB ~ 512 MB。

`mcp-agent.js` 是为此准备的**单文件、零依赖**微内核：只用 Node.js 核心模块，
不装任何 npm 包，提供 5 个高频工具，让这类沙盒也能接进 Agent。

| | Python 完整运行时 | Node 微内核 |
|---|---|---|
| 工具数量 | 65 | 5 |
| 依赖 | Python ≥ 3.10 + venv | 仅 Node.js |
| 安装 | venv + pip 安装 | 拷贝单文件 |
| 磁盘占用 | 数百 MB | 单个 `.js` 文件 |
| 适用 | VPS / 云主机 / DSW | 翼龙类受限沙盒 |

## 30 秒上手

```bash
# 1. 取文件（或直接上传到沙盒）
curl -fsSLO https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/deployments/sandbox-node/mcp-agent.js

# 2. 启动（未设 token 时会自动生成并打印，务必记下）
RHMCP_MICRO_TOKEN='换成你自己的长随机串' \
RHMCP_MICRO_PORT=8765 \
node mcp-agent.js
```

看到这样的输出就成功了：

```text
remote-host-mcp-micro 0.2.0-alpha.4 listening on http://0.0.0.0:8765
  MCP endpoint : http://0.0.0.0:8765/mcp
  Sandbox root : /home/container
  Allowed roots: /home/container
  Bearer token : from RHMCP_MICRO_TOKEN
```

冒烟验证：

```bash
curl -s http://127.0.0.1:8765/health
# {"status":"ok","server":"remote-host-mcp-micro","version":"..."}
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RHMCP_MICRO_TOKEN` | 自动生成 | Bearer 令牌。**未设置时会生成一个并打印**，绝不会"无认证"运行。 |
| `RHMCP_MICRO_PORT` | `8765` | 监听端口。回退顺序：`RHMCP_MICRO_PORT` → `SERVER_PORT` → `PORT` → `8765`。 |
| `RHMCP_MICRO_HOST` | `0.0.0.0` | 绑定地址。 |
| `RHMCP_MICRO_ROOT` | 进程工作目录 | 相对路径的解析基准。 |
| `RHMCP_ALLOWED_ROOTS` | 同 `RHMCP_MICRO_ROOT` | 文件工具允许访问的根目录，`:` 分隔。 |
| `RHMCP_MICRO_MAX_OUTPUT` | `1048576`（1 MiB） | 单次调用返回字节上限。 |
| `RHMCP_MICRO_TIMEOUT_MS` | `30000` | `exec` 默认超时。 |
| `RHMCP_MAX_TIMEOUT_MS` | `90000` | `exec` 超时上限（与 Python 运行时同名同义）。 |

## 内置工具

| 工具 | 作用 | 受 `RHMCP_ALLOWED_ROOTS` 约束 |
|---|---|---|
| `exec` | 执行 shell 命令，返回退出码 / stdout / stderr | 否（沙盒账号权限） |
| `read_file` | 读取 UTF-8 文本文件 | 是 |
| `write_file` | 写入文本，自动创建父目录，支持 `append` | 是 |
| `list_dir` | 列出目录条目（名称 / 类型 / 大小） | 是 |
| `system_info` | 平台、CPU、内存、负载、运行时长、自身 RSS | 否 |

`exec` 不做路径限制，与 Python 运行时的模型一致：进程以启动它的账号身份运行，
`RHMCP_ALLOWED_ROOTS` 只约束专用文件工具，不削减 shell 权限。

## 资源占用实测

> 实测环境：Windows 11 + Node **v24.15.0**。数据为 `system_info` 自报的
> `rss_bytes`（进程常驻内存）。

| 配置 | RSS | 相对基线增量 |
|---|---|---|
| 纯空 Node 进程（对照基线） | **46.0 MB** | — |
| 本 agent，默认参数 | **52.9 MB** | +6.9 MB |
| `--max-old-space-size=16` | 49.1 MB | +3.1 MB |
| `--jitless` | 48.9 MB | +2.9 MB |

**结论（请务必知情）**：

- 微内核自身的载荷只有约 **3 ~ 7 MB**，已经接近极限；
- **46 MB 是 Node.js V8 运行时的固定基线**，与代码无关 —— 空进程同样占用；
- 因此"常驻 15 ~ 25 MB"这个目标**在 Node.js 上无法达成**，不是本实现的问题；
- 收紧参数最多再省 4 MB，代价是 JIT 关闭后的性能下降，**不建议**。

对 308 MB 的沙盒而言，约 50 MB 占用（约 16%）是可接受的。若内存确实吃紧到
50 MB 都放不下，应改用 Tier 2 胖中枢模式（见
[`../../docs/architecture/tier2-hub-agent.md`](../../docs/architecture/tier2-hub-agent.md)），
把重活交给中枢，沙盒只留一条 WebSocket 长连接。

## Pterodactyl / Katabump 集成

翼龙面板的 Node.js egg 会用一个启动命令拉起容器内的入口文件。两种接法：

### 接法 A：直接作为启动命令（最简单）

把面板的启动命令改成：

```bash
node /home/container/mcp-agent.js
```

### 接法 B：伴随你现有的 `index.js` 启动（推荐）

如果你已经在沙盒里跑着别的 Node 服务，**不要覆盖** `index.js`，
只在其**顶部**加一行 require 即可：

```js
// index.js —— 你原有的入口文件
require('./mcp-agent.js');   // ← 只加这一行，微内核随主服务一起启动

// ……以下是你原有的业务代码，原样保留……
```

`mcp-agent.js` 是 CommonJS 模块，`require` 时其顶层代码即会执行并开始监听，
与你原有代码互不干扰（各自监听不同端口）。

### 端口注意事项

- 翼龙会把 `SERVER_PORT` 注入环境变量，微内核**已自动识别**，无需手动指定；
- 若面板只放行一个端口，把微内核和你的主服务错开（例如主服务用面板端口，
  微内核用 `RHMCP_MICRO_HOST=127.0.0.1` 只监听本地，再由下面的 Tunnel 暴露）。

## 用 Cloudflare Tunnel 暴露（无公网 IP）

沙盒一般不给公网 IP，用 Tunnel 打通：

```bash
# 1. 装 cloudflared（静态二进制，无需 root）
curl -fsSL -o cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared

# 2. 在 Cloudflare Zero Trust 面板建一个 Tunnel，拿到 Token，然后
./cloudflared tunnel --no-autoupdate run --token '<你的 Tunnel Token>'

# 3. 在该 Tunnel 的 Public Hostname 里把服务指向
#    http://127.0.0.1:8765
```

> 完整的 Tunnel 自动化（含 systemd 托管与证书）由主安装器的
> `installer/lib/cloudflare.sh` 负责，这里只是受限沙盒的手动替代路径。

## 客户端接入

拿到公网域名后，在 Agent 客户端里加一段 MCP 配置：

```json
{
  "mcpServers": {
    "remote-host-sandbox": {
      "type": "http",
      "url": "https://你的域名/mcp",
      "headers": {
        "Authorization": "Bearer 你的_RHMCP_MICRO_TOKEN"
      }
    }
  }
}
```

本地直连（无 Tunnel）时把 `url` 换成 `http://127.0.0.1:8765/mcp` 即可。

## 协议实现说明

- **传输**：MCP Streamable HTTP，**无状态**（不签发 `Mcp-Session-Id`）；
- **响应格式**：`application/json`。规范允许服务端以 JSON 而非 SSE 应答，
  这样能省掉一条常驻事件流的开销；
- **`GET /mcp`**：返回 `405`。规范明确允许服务端声明"本端点不提供 SSE 流"；
- **协议版本**：支持 `2025-06-18` / `2025-03-26` / `2024-11-05`，
  按客户端请求协商，无法识别时回落到 `2025-06-18`；
- **批量请求**：支持 JSON-RPC 2.0 数组；
- **通知**：无 `id` 的消息一律不回包，按规范返回 `202`。

## 安全说明

- **强制鉴权**：未设 `RHMCP_MICRO_TOKEN` 时自动生成随机令牌并打印，
  不存在"无认证"运行路径；
- **常量时间比较**：令牌校验使用 `crypto.timingSafeEqual`，抵抗计时侧信道；
- **路径逃逸防护**：文件工具一律先解析为绝对路径再校验是否落在
  `RHMCP_ALLOWED_ROOTS` 内，`../` 逃逸会被拒绝并返回明确错误；
- **请求体上限**：2 MiB，超限直接断开，防止 OOM；
- **输出上限**：单次调用默认 1 MiB，防止一条命令打爆内存；
- **`exec` 的边界**：它等价于"你登录这个沙盒后能执行的一切"。
  不要把它暴露在无 TLS、无鉴权的公网路径上。

## 已知限制

- 只有 5 个工具，`Jobs/Tasks`、PTY 终端、文件上传下载、审计等能力**均不提供**；
- 不支持 SSE 事件流（`GET /mcp` 返回 405）；
- 不支持 `Mcp-Session-Id` 会话保持；
- 内存下限受 Node.js 运行时制约（见上文"资源占用实测"）。
