#!/usr/bin/env node
// Smoke test for a Streamable HTTP MCP endpoint (no dependencies, Node >= 18).
// Usage: node tests/mcp-smoke.mjs <url> [minTools] [expectedToolSubstring]
// Performs initialize -> notifications/initialized -> tools/list and checks the result.
const [url, minToolsArg = "1", expected = ""] = process.argv.slice(2);
if (!url) {
  console.error("usage: mcp-smoke.mjs <url> [minTools] [expectedToolSubstring]");
  process.exit(2);
}
const minTools = Number(minToolsArg);
let sessionId;

async function rpc(body, { notify = false } = {}) {
  const headers = {
    "content-type": "application/json",
    accept: "application/json, text/event-stream",
    "mcp-protocol-version": "2025-06-18",
  };
  if (sessionId) headers["mcp-session-id"] = sessionId;
  const res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
  sessionId = res.headers.get("mcp-session-id") || sessionId;
  if (notify) {
    if (res.status >= 400) throw new Error(`notification -> HTTP ${res.status}`);
    await res.text();
    return null;
  }
  const text = await res.text();
  if (res.status >= 400) throw new Error(`${body.method} -> HTTP ${res.status}: ${text.slice(0, 300)}`);
  const type = res.headers.get("content-type") || "";
  // SSE: take the data line that carries the response with our id.
  const payloads = type.includes("text/event-stream")
    ? text.split("\n").filter((l) => l.startsWith("data:")).map((l) => JSON.parse(l.slice(5)))
    : [JSON.parse(text)];
  const msg = payloads.find((p) => p.id === body.id);
  if (!msg) throw new Error(`${body.method}: no response with id ${body.id} in ${text.slice(0, 300)}`);
  if (msg.error) throw new Error(`${body.method}: ${JSON.stringify(msg.error)}`);
  return msg.result;
}

try {
  const init = await rpc({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "mcp-seo-geo-stack-smoke", version: "1.0.0" },
    },
  });
  console.log(`server: ${init.serverInfo?.name} ${init.serverInfo?.version} (protocol ${init.protocolVersion})`);
  await rpc({ jsonrpc: "2.0", method: "notifications/initialized" }, { notify: true });
  const { tools = [] } = await rpc({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
  const names = tools.map((t) => t.name);
  console.log(`tools (${names.length}): ${names.slice(0, 15).join(", ")}${names.length > 15 ? ", ..." : ""}`);
  if (names.length < minTools) throw new Error(`expected at least ${minTools} tools, got ${names.length}`);
  if (expected && !names.some((n) => n.includes(expected))) throw new Error(`no tool name contains "${expected}"`);
  // Optional: call a tool and print (truncated) result. TOOL_CALL='{"name":"x","arguments":{}}'
  // TOOL_EXPECT=regex that must appear in the result text.
  if (process.env.TOOL_CALL) {
    const call = JSON.parse(process.env.TOOL_CALL);
    const result = await rpc({ jsonrpc: "2.0", id: 3, method: "tools/call", params: call });
    const text = (result.content || []).map((c) => c.text || "").join("\n");
    console.log(`tools/call ${call.name} (isError=${!!result.isError}): ${text.replace(/\s+/g, " ").slice(0, 400)}`);
    if (process.env.TOOL_EXPECT && !new RegExp(process.env.TOOL_EXPECT, "i").test(text)) {
      throw new Error(`tool result does not match /${process.env.TOOL_EXPECT}/`);
    }
  }
  console.log("OK");
} catch (err) {
  console.error(`FAIL: ${err.message}`);
  process.exit(1);
}
