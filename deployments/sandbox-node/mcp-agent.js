#!/usr/bin/env node
'use strict';

/**
 * Remote Host MCP — restricted-sandbox micro-kernel (Tier 3).
 *
 * Zero third-party dependencies: Node.js core modules only. This exists for
 * Pterodactyl/Katabump-style containers that have Node but can never run
 * `npm install` and have no usable Python.
 *
 * The tool surface is deliberately minimal (5 tools). The full 65-tool surface
 * stays in the Python runtime; this agent exists so a ~300 MB Node-only
 * sandbox still has a usable control plane.
 *
 * Transport: MCP Streamable HTTP, stateless (no session id, no SSE stream).
 * The spec allows a JSON response body and HTTP 405 for the GET stream, which
 * keeps the resident footprint small.
 *
 * Environment:
 *   RHMCP_MICRO_TOKEN       Bearer token. Generated and printed when unset.
 *   RHMCP_MICRO_PORT        Listen port (falls back to SERVER_PORT / PORT).
 *   RHMCP_MICRO_HOST        Bind address (default 0.0.0.0).
 *   RHMCP_MICRO_ROOT        Working directory for relative paths.
 *   RHMCP_ALLOWED_ROOTS     ':'-separated roots the file tools may touch.
 *   RHMCP_MICRO_MAX_OUTPUT  Per-call byte ceiling (default 1 MiB).
 */

const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs/promises');
const { exec } = require('node:child_process');
const { randomBytes, timingSafeEqual } = require('node:crypto');

const VERSION = process.env.RHMCP_MICRO_VERSION || '0.2.0-alpha.4';
const SERVER_NAME = 'remote-host-mcp-micro';

const LATEST_PROTOCOL_VERSION = '2025-06-18';
const SUPPORTED_PROTOCOL_VERSIONS = new Set([
  '2025-06-18',
  '2025-03-26',
  '2024-11-05',
]);

const MAX_BODY_BYTES = 2 * 1024 * 1024;

function positiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

const CONFIG = {
  host: process.env.RHMCP_MICRO_HOST || '0.0.0.0',
  port: positiveInt(
    process.env.RHMCP_MICRO_PORT || process.env.SERVER_PORT || process.env.PORT,
    8765,
  ),
  root: path.resolve(process.env.RHMCP_MICRO_ROOT || process.cwd()),
  maxOutput: positiveInt(process.env.RHMCP_MICRO_MAX_OUTPUT, 1024 * 1024),
  defaultTimeoutMs: positiveInt(process.env.RHMCP_MICRO_TIMEOUT_MS, 30000),
  maxTimeoutMs: positiveInt(process.env.RHMCP_MAX_TIMEOUT_MS, 90000),
};

// A missing token is never allowed to mean "no auth": generate one and print
// it so the operator can paste it into the client config.
CONFIG.token = process.env.RHMCP_MICRO_TOKEN || randomBytes(32).toString('hex');
CONFIG.generatedToken = !process.env.RHMCP_MICRO_TOKEN;

function allowedRoots() {
  const raw = process.env.RHMCP_ALLOWED_ROOTS;
  if (!raw) return [CONFIG.root];
  return raw
    .split(':')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => path.resolve(entry));
}

// File tools are the only ones that honour RHMCP_ALLOWED_ROOTS, matching the
// Python runtime: `exec` runs with the authority of the account that started
// this process and is intentionally not path-restricted.
function resolveAllowedPath(candidate) {
  const resolved = path.resolve(CONFIG.root, String(candidate || '.'));
  const roots = allowedRoots();
  const inside = roots.some(
    (root) => resolved === root || resolved.startsWith(root + path.sep),
  );
  if (!inside) {
    throw new Error(
      `Path escapes RHMCP_ALLOWED_ROOTS: ${resolved} (allowed: ${roots.join(', ')})`,
    );
  }
  return resolved;
}

function clampTimeout(value) {
  if (value === undefined || value === null || value === '') return CONFIG.defaultTimeoutMs;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) return CONFIG.defaultTimeoutMs;
  return Math.min(parsed, CONFIG.maxTimeoutMs);
}

function truncate(text) {
  if (typeof text !== 'string') return '';
  if (Buffer.byteLength(text, 'utf8') <= CONFIG.maxOutput) return text;
  return Buffer.from(text, 'utf8').subarray(0, CONFIG.maxOutput).toString('utf8');
}

function textResult(text, isError = false) {
  return { content: [{ type: 'text', text: truncate(text) }], isError };
}

function jsonResult(value, isError = false) {
  return textResult(JSON.stringify(value, null, 2), isError);
}

/* ------------------------------------------------------------------ tools */

const TOOLS = [
  {
    name: 'exec',
    description:
      'Run a shell command inside the sandbox and return its exit code, stdout and stderr.',
    inputSchema: {
      type: 'object',
      properties: {
        command: { type: 'string', description: 'Shell command to execute.' },
        cwd: { type: 'string', description: 'Working directory (defaults to the sandbox root).' },
        timeout_ms: { type: 'integer', description: 'Timeout in milliseconds (default 30000).' },
      },
      required: ['command'],
      additionalProperties: false,
    },
  },
  {
    name: 'read_file',
    description: 'Read a UTF-8 text file and return its contents.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'File path, absolute or relative to the sandbox root.' },
        max_bytes: { type: 'integer', description: 'Read ceiling in bytes (default 1048576).' },
      },
      required: ['path'],
      additionalProperties: false,
    },
  },
  {
    name: 'write_file',
    description: 'Write UTF-8 text to a file, creating parent directories when missing.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'File path, absolute or relative to the sandbox root.' },
        content: { type: 'string', description: 'Text to write.' },
        append: { type: 'boolean', description: 'Append instead of overwriting (default false).' },
      },
      required: ['path', 'content'],
      additionalProperties: false,
    },
  },
  {
    name: 'list_dir',
    description: 'List a directory with entry type and size.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Directory path (defaults to the sandbox root).' },
      },
      required: [],
      additionalProperties: false,
    },
  },
  {
    name: 'system_info',
    description: 'Report sandbox host facts: platform, CPU, memory and uptime.',
    inputSchema: { type: 'object', properties: {}, required: [], additionalProperties: false },
  },
];

function toolExec(args) {
  return new Promise((resolve) => {
    const command = String(args.command || '');
    if (!command.trim()) {
      resolve(textResult('command is required', true));
      return;
    }
    let cwd = CONFIG.root;
    try {
      cwd = args.cwd ? resolveAllowedPath(args.cwd) : CONFIG.root;
    } catch (error) {
      resolve(textResult(error.message, true));
      return;
    }
    exec(
      command,
      {
        cwd,
        timeout: clampTimeout(args.timeout_ms),
        maxBuffer: CONFIG.maxOutput,
        shell: '/bin/sh',
        encoding: 'utf8',
      },
      (error, stdout, stderr) => {
        const timedOut = Boolean(error && (error.killed || error.signal === 'SIGTERM'));
        const exitCode = error && typeof error.code === 'number' ? error.code : error ? 1 : 0;
        resolve(
          jsonResult(
            {
              exit_code: exitCode,
              timed_out: timedOut,
              stdout: truncate(stdout || ''),
              stderr: truncate(stderr || (error && !timedOut ? String(error.message) : '')),
            },
            Boolean(error),
          ),
        );
      },
    );
  });
}

async function toolReadFile(args) {
  try {
    const target = resolveAllowedPath(args.path);
    const ceiling = positiveInt(args.max_bytes, CONFIG.maxOutput);
    const stats = await fs.stat(target);
    if (!stats.isFile()) return textResult(`Not a regular file: ${target}`, true);
    if (stats.size > ceiling) {
      return textResult(
        `File is ${stats.size} bytes, above the ${ceiling}-byte ceiling. Raise max_bytes to read it.`,
        true,
      );
    }
    return textResult(await fs.readFile(target, 'utf8'));
  } catch (error) {
    return textResult(`read_file failed: ${error.message}`, true);
  }
}

async function toolWriteFile(args) {
  try {
    const target = resolveAllowedPath(args.path);
    const content = String(args.content ?? '');
    const append = args.append === true;
    if (!append) await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, content, { encoding: 'utf8', flag: append ? 'a' : 'w' });
    return jsonResult({ path: target, bytes: Buffer.byteLength(content, 'utf8'), append });
  } catch (error) {
    return textResult(`write_file failed: ${error.message}`, true);
  }
}

async function toolListDir(args) {
  try {
    const target = args.path ? resolveAllowedPath(args.path) : CONFIG.root;
    const entries = await fs.readdir(target, { withFileTypes: true });
    const listing = await Promise.all(
      entries.map(async (entry) => {
        const summary = {
          name: entry.name,
          type: entry.isDirectory() ? 'dir' : entry.isFile() ? 'file' : 'other',
        };
        if (entry.isFile()) {
          try {
            summary.size = (await fs.stat(path.join(target, entry.name))).size;
          } catch {
            /* raced removal: report the entry without a size */
          }
        }
        return summary;
      }),
    );
    return jsonResult({ path: target, count: listing.length, entries: listing });
  } catch (error) {
    return textResult(`list_dir failed: ${error.message}`, true);
  }
}

async function toolSystemInfo() {
  try {
    const load = os.loadavg();
    return jsonResult({
      hostname: os.hostname(),
      platform: `${os.platform()} ${os.release()} ${os.arch()}`,
      node: process.version,
      cpus: os.cpus().length,
      loadavg: load.map((value) => Number(value.toFixed(2))),
      memory: {
        total_bytes: os.totalmem(),
        free_bytes: os.freemem(),
      },
      uptime_seconds: Math.round(os.uptime()),
      process_uptime_seconds: Math.round(process.uptime()),
      rss_bytes: process.memoryUsage().rss,
      sandbox_root: CONFIG.root,
      allowed_roots: allowedRoots(),
    });
  } catch (error) {
    return textResult(`system_info failed: ${error.message}`, true);
  }
}

async function callTool(name, args) {
  const safeArgs = args && typeof args === 'object' ? args : {};
  switch (name) {
    case 'exec': return toolExec(safeArgs);
    case 'read_file': return toolReadFile(safeArgs);
    case 'write_file': return toolWriteFile(safeArgs);
    case 'list_dir': return toolListDir(safeArgs);
    case 'system_info': return toolSystemInfo();
    default: return textResult(`Unknown tool: ${name}`, true);
  }
}

/* -------------------------------------------------------------- JSON-RPC */

const RPC = {
  PARSE_ERROR: -32700,
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  INTERNAL_ERROR: -32603,
};

function rpcResult(id, result) {
  return { jsonrpc: '2.0', id, result };
}

function rpcError(id, code, message, data) {
  const error = { code, message };
  if (data !== undefined) error.data = data;
  return { jsonrpc: '2.0', id, error };
}

async function handleRequest(message) {
  const { id, method, params } = message;
  switch (method) {
    case 'initialize': {
      const requested = params && params.protocolVersion;
      const negotiated = SUPPORTED_PROTOCOL_VERSIONS.has(requested)
        ? requested
        : LATEST_PROTOCOL_VERSION;
      return rpcResult(id, {
        protocolVersion: negotiated,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: SERVER_NAME, version: VERSION },
        instructions:
          'Restricted-sandbox micro-kernel. Filesystem tools honour RHMCP_ALLOWED_ROOTS; exec runs with the sandbox account authority.',
      });
    }
    case 'ping':
      return rpcResult(id, {});
    case 'tools/list':
      return rpcResult(id, { tools: TOOLS });
    case 'tools/call': {
      const name = params && params.name;
      if (!name) return rpcError(id, RPC.INVALID_PARAMS, 'tools/call requires params.name');
      const result = await callTool(name, params.arguments);
      return rpcResult(id, result);
    }
    default:
      return rpcError(id, RPC.METHOD_NOT_FOUND, `Method not found: ${method}`);
  }
}

/* ------------------------------------------------------------------ HTTP */

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body, 'utf8'),
    'cache-control': 'no-store',
  });
  res.end(body);
}

function tokenMatches(header) {
  if (typeof header !== 'string') return false;
  const prefix = 'Bearer ';
  if (!header.startsWith(prefix)) return false;
  const presented = Buffer.from(header.slice(prefix.length).trim(), 'utf8');
  const expected = Buffer.from(CONFIG.token, 'utf8');
  if (presented.length !== expected.length) return false;
  return timingSafeEqual(presented, expected);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('Request body exceeds the 2 MiB ceiling'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

async function handleMcpPost(req, res) {
  let raw;
  try {
    raw = await readBody(req);
  } catch (error) {
    sendJson(res, 413, rpcError(null, RPC.INVALID_REQUEST, error.message));
    return;
  }
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    sendJson(res, 400, rpcError(null, RPC.PARSE_ERROR, 'Invalid JSON'));
    return;
  }

  const batch = Array.isArray(payload);
  const messages = batch ? payload : [payload];
  const responses = [];

  for (const message of messages) {
    if (!message || typeof message !== 'object' || message.jsonrpc !== '2.0') {
      responses.push(rpcError(null, RPC.INVALID_REQUEST, 'Not a JSON-RPC 2.0 message'));
      continue;
    }
    // Notifications carry no id and must never be answered.
    if (message.id === undefined || message.id === null) continue;
    try {
      responses.push(await handleRequest(message));
    } catch (error) {
      responses.push(rpcError(message.id, RPC.INTERNAL_ERROR, error.message));
    }
  }

  if (responses.length === 0) {
    // Pure notification batch: acknowledge without a body, per spec.
    res.writeHead(202, { 'content-length': '0' });
    res.end();
    return;
  }
  sendJson(res, 200, batch ? responses : responses[0]);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);

  if (url.pathname === '/health' && req.method === 'GET') {
    sendJson(res, 200, { status: 'ok', server: SERVER_NAME, version: VERSION });
    return;
  }

  if (url.pathname !== '/mcp') {
    sendJson(res, 404, rpcError(null, RPC.INVALID_REQUEST, 'Not found'));
    return;
  }

  if (!tokenMatches(req.headers.authorization)) {
    res.writeHead(401, {
      'content-type': 'application/json; charset=utf-8',
      'www-authenticate': 'Bearer',
    });
    res.end(JSON.stringify(rpcError(null, RPC.INVALID_REQUEST, 'Unauthorized')));
    return;
  }

  if (req.method === 'GET') {
    // No SSE stream is offered; the spec allows an explicit 405 here.
    res.writeHead(405, { allow: 'POST', 'content-length': '0' });
    res.end();
    return;
  }

  if (req.method === 'DELETE') {
    res.writeHead(204, { 'content-length': '0' });
    res.end();
    return;
  }

  if (req.method !== 'POST') {
    res.writeHead(405, { allow: 'POST', 'content-length': '0' });
    res.end();
    return;
  }

  handleMcpPost(req, res).catch((error) => {
    sendJson(res, 500, rpcError(null, RPC.INTERNAL_ERROR, error.message));
  });
});

server.listen(CONFIG.port, CONFIG.host, () => {
  process.stdout.write(
    [
      `${SERVER_NAME} ${VERSION} listening on http://${CONFIG.host}:${CONFIG.port}`,
      `  MCP endpoint : http://${CONFIG.host}:${CONFIG.port}/mcp`,
      `  Sandbox root : ${CONFIG.root}`,
      `  Allowed roots: ${allowedRoots().join(', ')}`,
      CONFIG.generatedToken
        ? `  Bearer token : ${CONFIG.token}  (generated for this run — set RHMCP_MICRO_TOKEN to pin it)`
        : '  Bearer token : from RHMCP_MICRO_TOKEN',
      '',
    ].join('\n'),
  );
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
  });
}
