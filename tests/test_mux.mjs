#!/usr/bin/env node
// Test of services/mux/mux.mjs against tests/fake-mcp-server.mjs (fetch only, Node >= 18).
// Proves that many concurrent sessions are served by a fixed number of shared MCP server
// processes, with progress, cancellation, restart after a crash and a bounded session count.
//
//   cd services/mux && npm ci && node ../../tests/test_mux.mjs
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const LOG = join(mkdtempSync(join(tmpdir(), "mux-")), "fake.log");
writeFileSync(LOG, "");
const SESSIONS = 50;
let passed = 0, failed = 0;
const check = (name, cond, detail = "") => {
  cond ? passed++ : failed++;
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${cond ? "" : `  -> ${typeof detail === "string" ? detail : JSON.stringify(detail)}`}`);
};
const port = await new Promise((r) => { const s = createServer().listen(0, () => { const p = s.address().port; s.close(() => r(p)); }); });
const URL_ = `http://127.0.0.1:${port}/x/mcp`;
const lines = () => readFileSync(LOG, "utf8").split("\n").filter(Boolean);

const mux = spawn(process.execPath, [join(ROOT, "services/mux/mux.mjs"), "--port", String(port), "--path", "/x/mcp",
  "--health", "/x/healthz", "--", process.execPath, join(ROOT, "tests/fake-mcp-server.mjs")],
  { env: { ...process.env, FAKE_LOG: LOG, MUX_BACKENDS: "2", MUX_MAX_SESSIONS: "60" }, stdio: ["ignore", "inherit", "inherit"] });

async function health() {
  const r = await fetch(`http://127.0.0.1:${port}/x/healthz`);
  return { code: r.status, ...(await r.json()) };
}

async function post(sid, body, extra = {}) {
  const headers = { "content-type": "application/json", accept: "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-06-18", ...extra };
  if (sid) headers["mcp-session-id"] = sid;
  const r = await fetch(URL_, { method: "POST", headers, body: JSON.stringify(body) });
  const raw = await r.text();
  const messages = (r.headers.get("content-type") || "").includes("text/event-stream")
    ? raw.split("\n").filter((l) => l.startsWith("data:")).map((l) => JSON.parse(l.slice(5)))
    : raw ? [JSON.parse(raw)] : [];
  return { status: r.status, sid: r.headers.get("mcp-session-id") || sid, messages,
           result: messages.find((m) => m.id === body.id) };
}

async function session() {
  const init = await post(null, { jsonrpc: "2.0", id: 1, method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "1" } } });
  if (init.status !== 200) throw new Error(`initialize -> ${init.status}`);
  await post(init.sid, { jsonrpc: "2.0", method: "notifications/initialized" });
  return init;
}
const call = (sid, name, id = 2, meta) =>
  post(sid, { jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: {}, ...(meta ? { _meta: meta } : {}) } });
const textOf = (r) => r.result?.result?.content?.[0]?.text;

try {
  for (let i = 0; i < 100; i++) {
    try { if ((await health()).code === 200) break; } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  check("health answers once the first MCP server process is up", (await health()).code === 200);

  const init = await session();
  check("initialize returns the MCP server's own name, version and instructions",
    init.result?.result?.serverInfo?.name === "fake-mcp" && init.result.result.instructions === "fake instructions", init.result);

  // 50 sessions at once, each with a 500 ms tool call.
  const t0 = Date.now();
  const all = await Promise.all(Array.from({ length: SESSIONS }, async () => {
    const s = await session();
    const [slow, pid] = await Promise.all([call(s.sid, "slow", 2), call(s.sid, "pid", 3)]);
    return { sid: s.sid, slow: textOf(slow), pid: textOf(pid) };
  }));
  const elapsed = Date.now() - t0;
  check(`${SESSIONS} concurrent sessions all answered`, all.every((r) => r.slow === "slow done" && r.pid), all.find((r) => r.slow !== "slow done"));
  check(`${SESSIONS} slow calls ran in parallel (${elapsed} ms)`, elapsed < 5000, elapsed);
  const pids = new Set(all.map((r) => r.pid));
  check(`served by the 2 shared MCP processes only (${pids.size})`, pids.size <= 2 && lines().filter((l) => l.startsWith("start")).length === 2, lines());
  check("all sessions distinct", new Set(all.map((r) => r.sid)).size === SESSIONS);

  const listed = await post(init.sid, { jsonrpc: "2.0", id: 4, method: "tools/list" });
  check("tools/list relayed", listed.result?.result?.tools?.length === 5, listed.result);

  const prog = await call(init.sid, "progress", 5, { progressToken: "tok-1" });
  const notes = prog.messages.filter((m) => m.method === "notifications/progress");
  check("progress relayed to the client with its own token", notes.length === 3 && notes.every((n) => n.params.progressToken === "tok-1"), prog.messages);
  check("progress call answered", textOf(prog) === "progress done");

  // Cancellation: the client cancels its call, the MCP process is told.
  const waiting = call(init.sid, "wait", 6);
  await new Promise((r) => setTimeout(r, 200));
  await post(init.sid, { jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: 6, reason: "test" } });
  await Promise.race([waiting, new Promise((r) => setTimeout(r, 1000))]);
  await new Promise((r) => setTimeout(r, 200));
  check("cancellation forwarded to the MCP server process", lines().some((l) => l.startsWith("cancelled")), lines());

  // Crash: the call fails, the process is restarted, the session keeps working.
  const starts = lines().filter((l) => l.startsWith("start")).length;
  const crashed = await call(init.sid, "crash", 7);
  check("call on a crashing MCP process gets a JSON-RPC error", !!crashed.result?.error, crashed.messages);
  check("same session still works meanwhile (other shared process)", !!textOf(await call(init.sid, "pid", 8)));
  await new Promise((r) => setTimeout(r, 2500));
  const h0 = await health();
  check("crashed process restarted automatically",
    lines().filter((l) => l.startsWith("start")).length === starts + 1 && h0.backends.every((b) => b.running), [lines(), h0]);

  const unknown = await call("no-such-session", "pid");
  check("unknown session -> 404 (client re-initializes)", unknown.status === 404);
  const get = await fetch(URL_, { headers: { accept: "text/event-stream" } });
  check("GET without session -> 400", get.status === 400);
  const notInit = await post(null, { jsonrpc: "2.0", id: 1, method: "tools/list" });
  check("request without session that is not initialize -> 400", notInit.status === 400);

  // Session cap (60): 20 more sessions evict the oldest idle ones.
  const extra = await Promise.all(Array.from({ length: 20 }, () => session()));
  check("sessions beyond the cap still open", extra.every((s) => s.status === 200));
  const h = await health();
  check(`open sessions bounded by MUX_MAX_SESSIONS (${h.sessions})`, h.sessions <= 60, h);
  const statuses = await Promise.all(all.map((r, i) => call(r.sid, "pid", 30 + i).then((x) => x.status)));
  const closed = statuses.filter((st) => st === 404).length;
  check(`least recently used idle sessions closed (${closed} of the first batch)`, closed >= 11, statuses);
  check("recently used session kept", (await call(init.sid, "pid", 99)).status === 200);

  const del = await fetch(URL_, { method: "DELETE", headers: { "mcp-session-id": extra[0].sid, "mcp-protocol-version": "2025-06-18" } });
  check("DELETE ends a session", del.status === 200 && (await call(extra[0].sid, "pid")).status === 404, del.status);
} catch (err) {
  check("no exception", false, err.stack);
} finally {
  mux.kill("SIGTERM");
  await new Promise((r) => mux.once("exit", r));
}
console.log(`\n${passed}/${passed + failed} checks passed`);
process.exit(failed ? 1 : 0);
