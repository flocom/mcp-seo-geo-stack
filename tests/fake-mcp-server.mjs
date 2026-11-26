#!/usr/bin/env node
// Fake stdio MCP server for tests/test_mux.mjs (no dependencies).
// Tools: pid (its process id), slow (answers after 500 ms), progress (3 progress notifications),
// wait (never answers; only a cancellation ends it), crash (exits). FAKE_LOG gets one line per
// start and per cancellation received.
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";

const note = (line) => process.env.FAKE_LOG && appendFileSync(process.env.FAKE_LOG, `${line}\n`);
const send = (msg) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...msg }) + "\n");
const text = (t) => ({ content: [{ type: "text", text: String(t) }] });
note(`start ${process.pid}`);

createInterface({ input: process.stdin }).on("line", (line) => {
  const msg = JSON.parse(line);
  if (msg.method === "notifications/cancelled") return note(`cancelled ${msg.params.requestId}`);
  if (msg.id === undefined) return;
  const reply = (result) => send({ id: msg.id, result });
  switch (msg.method) {
    case "initialize":
      return reply({ protocolVersion: msg.params.protocolVersion, capabilities: { tools: { listChanged: true } },
                     serverInfo: { name: "fake-mcp", version: "1.2.3" }, instructions: "fake instructions" });
    case "tools/list":
      return reply({ tools: ["pid", "slow", "progress", "wait", "crash"].map((name) =>
        ({ name, description: name, inputSchema: { type: "object" } })) });
    case "tools/call": {
      const token = msg.params._meta?.progressToken;
      switch (msg.params.name) {
        case "pid": return reply(text(process.pid));
        case "slow": return setTimeout(() => reply(text("slow done")), 500);
        case "progress":
          [1, 2, 3].forEach((n) => setTimeout(() =>
            send({ method: "notifications/progress", params: { progressToken: token, progress: n, total: 3 } }), n * 50));
          return setTimeout(() => reply(text("progress done")), 250);
        case "wait": return;
        case "crash": return process.exit(1);
      }
      return send({ id: msg.id, error: { code: -32602, message: "unknown tool" } });
    }
    default:
      return send({ id: msg.id, error: { code: -32601, message: "Method not found" } });
  }
});
