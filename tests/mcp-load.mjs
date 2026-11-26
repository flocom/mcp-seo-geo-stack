#!/usr/bin/env node
// Load test for a Streamable HTTP MCP endpoint (no dependencies, Node >= 18): N sessions at once,
// each doing initialize -> notifications/initialized -> tools/list (and TOOL_CALL if set).
// Usage: node tests/mcp-load.mjs <url> [sessions=50]   (MCP_HEADERS: extra headers as JSON)
const [url, n = "50"] = process.argv.slice(2);
const extra = process.env.MCP_HEADERS ? JSON.parse(process.env.MCP_HEADERS) : {};
const call = process.env.TOOL_CALL ? JSON.parse(process.env.TOOL_CALL) : null;

async function post(sid, body) {
  const headers = { "content-type": "application/json", accept: "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-06-18", ...extra };
  if (sid) headers["mcp-session-id"] = sid;
  const res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
  const text = await res.text();
  if (res.status >= 400) throw new Error(`${body.method} -> HTTP ${res.status}: ${text.slice(0, 200)}`);
  return { sid: res.headers.get("mcp-session-id") || sid, text };
}

async function session() {
  const { sid } = await post(null, { jsonrpc: "2.0", id: 1, method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "mcp-load", version: "1" } } });
  await post(sid, { jsonrpc: "2.0", method: "notifications/initialized" });
  const list = await post(sid, { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
  if (!list.text.includes('"tools"')) throw new Error(`tools/list: ${list.text.slice(0, 200)}`);
  if (call) {
    const r = await post(sid, { jsonrpc: "2.0", id: 3, method: "tools/call", params: call });
    if (!r.text.includes('"result"')) throw new Error(`tools/call: ${r.text.slice(0, 200)}`);
  }
}

const start = Date.now();
const results = await Promise.allSettled(Array.from({ length: Number(n) }, session));
const failures = results.filter((r) => r.status === "rejected");
console.log(`${results.length - failures.length}/${results.length} concurrent sessions OK in ${Date.now() - start} ms`);
if (failures.length) {
  console.error(`FAIL: ${failures[0].reason.message}`);
  process.exit(1);
}
