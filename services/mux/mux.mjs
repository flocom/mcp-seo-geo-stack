#!/usr/bin/env node
// Multiplexeur MCP : de nombreuses sessions Streamable HTTP, quelques processus serveur MCP partagés.
//
//   node mux.mjs --port 8000 --path /gsc-bing/mcp --health /gsc-bing/healthz -- <commande stdio...>
//
// Pourquoi : supergateway (mode stateful) lance un processus serveur MCP par session HTTP
// (~180 Mo pour search-console-mcp). 50 connexions simultanées = 50 processus, la mémoire du
// serveur s'épuise et cloudflared tombe avec le reste (erreur Cloudflare 1033).
//
// Ici, MUX_BACKENDS processus (2 par défaut) sont lancés une fois et partagés par toutes les
// sessions. Une session ne coûte que quelques Ko : son propre objet Server du SDK MCP, qui relaie
// chaque requête (tools/list, tools/call, prompts, resources, completion) au processus le moins
// chargé, avec progression, annulation et délai. C'est sûr parce que search-console-mcp et
// analytics-mcp sont sans état côté client : mêmes identifiants pour tous, aucune requête du
// serveur vers le client (sampling, roots, elicitation).
//
// Un processus qui s'arrête est relancé aussitôt (avec un délai croissant s'il échoue à démarrer) ;
// les sessions continuent, servies en attendant par les autres processus.
//
// Réglages (variables d'environnement) :
//   MUX_BACKENDS          processus serveur MCP partagés (défaut 2)
//   MUX_MAX_SESSIONS      sessions ouvertes au plus ; au-delà, la plus ancienne inactive est fermée (défaut 200)
//   MUX_SESSION_IDLE_MS   session sans requête pendant ce délai : fermée (défaut 30 min)
//   MUX_REQUEST_TIMEOUT_MS délai maximal d'une requête relayée, remis à zéro à chaque progression (défaut 5 min)
//   MUX_DEBUG=1           recopie la sortie d'erreur des processus serveur dans les logs
import http from "node:http";
import { randomUUID } from "node:crypto";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import * as T from "@modelcontextprotocol/sdk/types.js";

// --- Configuration ------------------------------------------------------------------------------

function parseArgs(argv) {
  const sep = argv.indexOf("--");
  if (sep < 0 || sep === argv.length - 1) throw new Error("usage: mux.mjs [--port N] [--path P] [--health H] -- <commande...>");
  const opts = { port: 8000, path: "/mcp", health: "/healthz" };
  const flags = argv.slice(0, sep);
  for (let i = 0; i < flags.length; i += 2) {
    const key = flags[i].replace(/^--/, "");
    if (!(key in opts)) throw new Error(`option inconnue : ${flags[i]}`);
    opts[key] = key === "port" ? Number(flags[i + 1]) : flags[i + 1];
  }
  return { ...opts, command: argv[sep + 1], args: argv.slice(sep + 2) };
}

const intEnv = (name, def) => {
  const n = Number.parseInt(process.env[name] ?? "", 10);
  return Number.isFinite(n) && n > 0 ? n : def;
};

const CFG = {
  ...parseArgs(process.argv.slice(2)),
  backends: intEnv("MUX_BACKENDS", 2),
  maxSessions: intEnv("MUX_MAX_SESSIONS", 200),
  idleMs: intEnv("MUX_SESSION_IDLE_MS", 30 * 60_000),
  requestTimeoutMs: intEnv("MUX_REQUEST_TIMEOUT_MS", 5 * 60_000),
  debug: process.env.MUX_DEBUG === "1",
};
const MAX_BODY = 10 * 1024 * 1024;
const log = (...a) => console.error("[mux]", ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let stopping = false;

// --- Processus serveur MCP partagés --------------------------------------------------------------

class Backend {
  constructor(index) {
    this.index = index;
    this.client = null;
    this.starting = null;
    this.inflight = 0;
    this.failures = 0;
    this.onListChanged = () => {};
  }

  /** Le client connecté au processus, lancé (ou relancé) au besoin. */
  get() {
    if (this.client) return Promise.resolve(this.client);
    this.starting ??= this.#spawn().finally(() => (this.starting = null));
    return this.starting;
  }

  async #spawn() {
    if (this.failures) await sleep(Math.min(30_000, 500 * 2 ** this.failures));
    const client = new Client({ name: "mcp-seo-geo-stack-mux", version: "1.0.0" }, { capabilities: {} });
    const transport = new StdioClientTransport({
      command: CFG.command,
      args: CFG.args,
      env: process.env, // identifiants Google et clé Bing exportés par entrypoint.sh
      stderr: CFG.debug ? "inherit" : "ignore",
    });
    client.onclose = () => {
      if (this.client === client) {
        this.client = null;
        if (stopping) return;
        log(`processus serveur MCP n°${this.index} arrêté ; relance`);
        setTimeout(() => this.#restart(), 1000).unref();
      }
    };
    client.onerror = (err) => CFG.debug && log(`processus n°${this.index} :`, err?.message ?? err);
    for (const schema of [T.ToolListChangedNotificationSchema, T.PromptListChangedNotificationSchema,
                          T.ResourceListChangedNotificationSchema]) {
      client.setNotificationHandler(schema, (n) => this.onListChanged(n.method));
    }
    try {
      await client.connect(transport, { timeout: 120_000 });
    } catch (err) {
      this.failures++;
      await client.close().catch(() => {});
      throw err;
    }
    this.failures = 0;
    this.client = client;
    return client;
  }

  #restart() {
    if (stopping || this.client) return;
    this.get().catch((err) => {
      log(`relance du processus n°${this.index} impossible : ${err?.message ?? err}`);
      this.#restart();
    });
  }

  async close() {
    await this.client?.close().catch(() => {});
  }
}

const pool = Array.from({ length: CFG.backends }, (_, i) => new Backend(i + 1));

/** Le processus le moins chargé (à égalité : un déjà lancé). */
function pick() {
  return pool.reduce((best, b) =>
    b.inflight < best.inflight || (b.inflight === best.inflight && b.client && !best.client) ? b : best);
}

// --- Relais des requêtes ------------------------------------------------------------------------

const RELAYED = [
  ["tools", T.ListToolsRequestSchema, T.ListToolsResultSchema],
  ["tools", T.CallToolRequestSchema, T.CallToolResultSchema],
  ["prompts", T.ListPromptsRequestSchema, T.ListPromptsResultSchema],
  ["prompts", T.GetPromptRequestSchema, T.GetPromptResultSchema],
  ["resources", T.ListResourcesRequestSchema, T.ListResourcesResultSchema],
  ["resources", T.ListResourceTemplatesRequestSchema, T.ListResourceTemplatesResultSchema],
  ["resources", T.ReadResourceRequestSchema, T.ReadResourceResultSchema],
  ["completions", T.CompleteRequestSchema, T.CompleteResultSchema],
];

function relay(resultSchema) {
  return async (request, extra) => {
    const backend = pick();
    backend.inflight++;
    try {
      const client = await backend.get();
      // Le jeton de progression du client est remplacé par celui du SDK côté processus ;
      // les progressions sont renvoyées au client avec son propre jeton.
      const { _meta, ...params } = request.params ?? {};
      const { progressToken, ...meta } = _meta ?? {};
      const outgoing = Object.keys(meta).length ? { ...params, _meta: meta } : params;
      return await client.request({ method: request.method, params: outgoing }, resultSchema, {
        signal: extra.signal, // annulation côté client -> notifications/cancelled au processus
        timeout: CFG.requestTimeoutMs,
        resetTimeoutOnProgress: true,
        onprogress: progressToken === undefined ? undefined : (p) =>
          extra.sendNotification({ method: "notifications/progress", params: { ...p, progressToken } }).catch(() => {}),
      });
    } finally {
      backend.inflight--;
    }
  };
}

// --- Sessions HTTP -----------------------------------------------------------------------------

let serverInfo, serverCapabilities, instructions;
const sessions = new Map(); // id -> { server, transport, lastSeen, busy }

async function openSession() {
  const server = new Server(serverInfo, { capabilities: serverCapabilities, instructions });
  for (const [cap, requestSchema, resultSchema] of RELAYED) {
    if (serverCapabilities[cap]) server.setRequestHandler(requestSchema, relay(resultSchema));
  }
  if (serverCapabilities.logging) server.setRequestHandler(T.SetLevelRequestSchema, () => ({}));
  const session = { server, transport: null, lastSeen: Date.now(), busy: 0 };
  session.transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
    onsessioninitialized: (id) => sessions.set(id, session),
    onsessionclosed: (id) => sessions.delete(id),
  });
  session.transport.onclose = () => {
    if (session.transport.sessionId) sessions.delete(session.transport.sessionId);
  };
  await server.connect(session.transport);
  return session;
}

function closeSession(id, reason) {
  const session = sessions.get(id);
  if (!session) return;
  sessions.delete(id);
  if (CFG.debug) log(`session fermée (${reason})`);
  session.server.close().catch(() => {});
}

/** Place pour une session de plus : ferme la plus ancienne inactive. false si toutes sont occupées. */
function makeRoom() {
  while (sessions.size >= CFG.maxSessions) {
    let oldest = null;
    for (const [id, s] of sessions) if (!s.busy && (!oldest || s.lastSeen < oldest[1].lastSeen)) oldest = [id, s];
    if (!oldest) return false;
    closeSession(oldest[0], "plafond de sessions");
  }
  return true;
}

setInterval(() => {
  const limit = Date.now() - CFG.idleMs;
  for (const [id, s] of sessions) if (!s.busy && s.lastSeen < limit) closeSession(id, "inactive");
}, Math.min(60_000, CFG.idleMs)).unref();

function broadcast(method) {
  for (const { server } of sessions.values()) server.notification({ method }).catch(() => {});
}
for (const b of pool) b.onListChanged = broadcast;

// --- Serveur HTTP ------------------------------------------------------------------------------

function sendJson(res, status, payload, headers = {}) {
  const body = JSON.stringify(payload);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body), ...headers });
  res.end(body);
}

const rpcError = (code, message) => ({ jsonrpc: "2.0", error: { code, message }, id: null });

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) throw Object.assign(new Error("body too large"), { status: 413 });
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw Object.assign(new Error("invalid JSON"), { status: 400 });
  }
}

const isInitialize = (body) => (Array.isArray(body) ? body : [body]).some((m) => T.isInitializeRequest(m));

async function handle(req, res) {
  const path = (req.url ?? "/").split("?", 1)[0];
  if (path === CFG.health) {
    const ready = pool.some((b) => b.client);
    return sendJson(res, ready ? 200 : 503, { status: ready ? "ok" : "starting", sessions: sessions.size,
      backends: pool.map((b) => ({ running: !!b.client, inflight: b.inflight })) });
  }
  if (path !== CFG.path) return sendJson(res, 404, { error: "not_found" });

  const sid = req.headers["mcp-session-id"];
  if (sid) {
    const session = sessions.get(sid);
    // Session inconnue (fermée, expirée, ou perdue au redémarrage) : 404, le client en rouvre une.
    if (!session) return sendJson(res, 404, rpcError(-32001, "Session not found"));
    session.lastSeen = Date.now();
    let body;
    if (req.method === "POST") {
      session.busy++;
      res.once("close", () => { session.busy--; session.lastSeen = Date.now(); });
      body = await readJson(req);
    }
    return session.transport.handleRequest(req, res, body);
  }

  if (req.method !== "POST") return sendJson(res, 400, rpcError(-32000, "Bad Request: No valid session ID provided"));
  const body = await readJson(req);
  if (!isInitialize(body)) return sendJson(res, 400, rpcError(-32000, "Bad Request: No valid session ID provided"));
  if (!makeRoom()) return sendJson(res, 503, rpcError(-32000, "Too many busy sessions"), { "Retry-After": "5" });
  const session = await openSession();
  session.busy++;
  res.once("close", () => { session.busy--; session.lastSeen = Date.now(); });
  return session.transport.handleRequest(req, res, body);
}

const httpServer = http.createServer((req, res) => {
  handle(req, res).catch((err) => {
    if (res.headersSent) return res.destroy();
    sendJson(res, err.status ?? 500, rpcError(-32603, err.status ? err.message : "Internal error"));
    if (!err.status) log("erreur :", err?.message ?? err);
  });
});
httpServer.keepAliveTimeout = 65_000;
httpServer.requestTimeout = 0; // flux SSE longs
httpServer.headersTimeout = 70_000;

// --- Démarrage et arrêt -----------------------------------------------------------------------

async function main() {
  const first = await pool[0].get(); // capacités, nom et instructions du serveur MCP
  serverInfo = first.getServerVersion() ?? { name: "mcp", version: "0.0.0" };
  const { subscribe, ...resources } = first.getServerCapabilities()?.resources ?? {};
  serverCapabilities = { ...first.getServerCapabilities() };
  if (serverCapabilities.resources) serverCapabilities.resources = resources; // abonnements non relayés
  instructions = first.getInstructions();
  for (const b of pool.slice(1)) b.get().catch((err) => log(`processus n°${b.index} :`, err.message));
  // (un échec de démarrage d'un processus secondaire est retenté à sa première requête)
  httpServer.listen(CFG.port, "0.0.0.0", () =>
    log(`${serverInfo.name} ${serverInfo.version} sur :${CFG.port}${CFG.path} ; ${CFG.backends} processus partagés, ` +
        `${CFG.maxSessions} sessions max`));
}

async function stop() {
  if (stopping) return;
  stopping = true;
  httpServer.close();
  for (const id of [...sessions.keys()]) closeSession(id, "arrêt");
  await Promise.all(pool.map((b) => b.close()));
  process.exit(0);
}
process.on("SIGTERM", stop);
process.on("SIGINT", stop);

main().catch((err) => {
  log("démarrage impossible :", err?.message ?? err);
  process.exit(1);
});
