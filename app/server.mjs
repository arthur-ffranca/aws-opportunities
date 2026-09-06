import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { createReadStream } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createTelemetry } from "./telemetry.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const demoRoot = path.resolve(__dirname, "..");
const publicRoot = path.join(__dirname, "public");
const curatedRoot = path.join(demoRoot, "run-state", "curated");
const summaryPath = path.join(curatedRoot, "scan_demo_summary.json");
const recommendationsPath = path.join(curatedRoot, "scan_product_recommendations.csv");
const insightsPath = path.join(curatedRoot, "bedrock_insights.md");
const runScriptPath = path.join(demoRoot, "run-scan-demo.ps1");
const observabilityDir = path.join(demoRoot, "run-state", "observability");
const eventsPath = path.join(observabilityDir, "events.jsonl");
const telemetry = createTelemetry({
  eventsPath,
  defaultTags: ["service:scan-opportunity-radar", "env:localstack"],
});

let currentRun = null;
let lastRun = null;

function parseCsvLine(line) {
  const values = [];
  let current = "";
  let quoted = false;

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    const next = line[index + 1];

    if (char === '"' && quoted && next === '"') {
      current += '"';
      index += 1;
    } else if (char === '"') {
      quoted = !quoted;
    } else if (char === "," && !quoted) {
      values.push(current);
      current = "";
    } else {
      current += char;
    }
  }

  values.push(current);
  return values;
}

function parseCsv(text) {
  const normalized = text.replace(/^\uFEFF/, "").trim();
  if (!normalized) return [];

  const lines = normalized.split(/\r?\n/);
  const headers = parseCsvLine(lines[0]);

  return lines.slice(1).filter(Boolean).map((line) => {
    const values = parseCsvLine(line);
    const row = {};
    headers.forEach((header, index) => {
      row[header] = values[index] ?? "";
    });
    return row;
  });
}

function number(value) {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(String(value).replace(",", "."));
  return Number.isFinite(parsed) ? parsed : null;
}

function metricSuffix(value) {
  return String(value)
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function normalizeRecommendation(row) {
  return {
    rank: number(row.rank),
    acao: row.acao_recomendada,
    score: number(row.score_oportunidade),
    produto: row.produto,
    marca: row.marca,
    fabricante: row.fabricante,
    racional: row.racional,
    importanciaCliente: number(row.importancia_cliente_pct),
    importanciaConcorrencia: number(row.importancia_concorrencia_pct),
    unidadesCliente: number(row.unidades_cliente),
    unidadesConcorrencia: number(row.unidades_concorrencia),
    precoCliente: number(row.preco_cliente),
    precoConcorrencia: number(row.preco_concorrencia),
    lojasCliente: number(row.lojas_cliente_pct),
    lojasConcorrencia: number(row.lojas_concorrencia_pct),
    gapUnidades: number(row.gap_unidades),
    gapLojas: number(row.gap_lojas_pp),
    gapPreco: number(row.gap_preco_pct),
    potencialReceitaMensal: number(row.potencial_receita_mensal),
  };
}

export function publishBusinessMetrics(summary, recommendations, publisher = telemetry) {
  publisher.gauge("scan.radar.products", summary.products ?? 0);
  publisher.gauge("scan.radar.recommendations", recommendations.length);
  publisher.gauge("scan.radar.potential_revenue_total", summary.potential_revenue_total ?? 0);
  for (const [action, count] of Object.entries(summary.action_counts ?? {})) {
    publisher.gauge("scan.radar.action_count", count, [`action:${action}`]);
  }
  for (const [action, potential] of Object.entries(summary.potential_revenue_by_action ?? {})) {
    publisher.gauge("scan.radar.potential_revenue", potential, [`action:${action}`]);
    publisher.gauge(`scan.radar.potential_revenue.${metricSuffix(action)}`, potential);
  }
  for (const [action, comparison] of Object.entries(summary.comparison_by_action ?? {})) {
    const suffix = metricSuffix(action);
    for (const [name, value] of Object.entries(comparison)) {
      if (name === "count" || value === null || value === undefined) continue;
      publisher.gauge(`scan.radar.compare.${suffix}.${name}`, value);
    }
  }
}

export async function loadDashboardData() {
  const started = performance.now();
  const [summaryText, csvText, insights] = await Promise.all([
    fs.readFile(summaryPath, "utf8"),
    fs.readFile(recommendationsPath, "utf8"),
    fs.readFile(insightsPath, "utf8"),
  ]);

  const summary = JSON.parse(summaryText);
  const recommendations = parseCsv(csvText).map(normalizeRecommendation);
  publishBusinessMetrics(summary, recommendations);
  telemetry.timing("scan.radar.dashboard.load_ms", Math.round(performance.now() - started));
  await telemetry.event("dashboard.loaded", {
    products: summary.products ?? 0,
    recommendations: recommendations.length,
    potentialRevenueTotal: summary.potential_revenue_total ?? 0,
  });

  return {
    generatedAt: new Date().toISOString(),
    summary,
    actionCounts: summary.action_counts ?? {},
    top10: summary.top_10 ?? recommendations.slice(0, 10),
    recommendations,
    insights,
    files: {
      summary: summaryPath,
      recommendations: recommendationsPath,
      insights: insightsPath,
    },
    run: {
      running: Boolean(currentRun),
      last: lastRun,
    },
    datadog: {
      service: "scan-opportunity-radar",
      metricsPrefix: "scan.radar",
      eventsPath,
    },
  };
}

async function readJsonBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  if (chunks.length === 0) return {};
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify(payload, null, 2));
}

async function serveStatic(request, response) {
  const url = new URL(request.url, "http://localhost");
  const requestedPath = url.pathname === "/" ? "/index.html" : url.pathname;
  const safePath = path.normalize(requestedPath).replace(/^(\.\.[/\\])+/, "");
  const filePath = path.join(publicRoot, safePath);

  if (!filePath.startsWith(publicRoot)) {
    response.writeHead(403);
    response.end("Forbidden");
    return;
  }

  const ext = path.extname(filePath).toLowerCase();
  const contentType = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
  }[ext] ?? "application/octet-stream";

  try {
    const stat = await fs.stat(filePath);
    if (!stat.isFile()) {
      response.writeHead(404);
      response.end("Not found");
      return;
    }

    response.writeHead(200, { "content-type": contentType });
    const stream = createReadStream(filePath);
    stream.on("error", () => {
      if (!response.headersSent) response.writeHead(404);
      response.end("Not found");
    });
    stream.pipe(response);
  } catch {
    response.writeHead(404);
    response.end("Not found");
  }
}

function startPipeline({ useBedrock = false } = {}) {
  if (currentRun) return { accepted: false, running: true };

  const args = [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    runScriptPath,
  ];

  if (useBedrock) args.push("-UseBedrock");

  const startedAt = new Date().toISOString();
  currentRun = {
    startedAt,
    output: "",
    error: "",
    status: "running",
    exitCode: null,
  };
  telemetry.increment("scan.radar.run.started", 1, [`bedrock:${useBedrock}`]);
  telemetry.event("pipeline.started", { useBedrock });

  const child = spawn("powershell", args, {
    cwd: demoRoot,
    windowsHide: true,
  });

  child.stdout.on("data", (chunk) => {
    currentRun.output += chunk.toString();
  });

  child.stderr.on("data", (chunk) => {
    currentRun.error += chunk.toString();
  });

  child.on("close", (code) => {
    const finishedAt = new Date().toISOString();
    const durationMs = Date.parse(finishedAt) - Date.parse(startedAt);
    lastRun = {
      ...currentRun,
      status: code === 0 ? "completed" : "failed",
      exitCode: code,
      finishedAt,
      durationMs,
    };
    telemetry.increment(`scan.radar.run.${code === 0 ? "completed" : "failed"}`);
    telemetry.timing("scan.radar.run.duration_ms", durationMs);
    telemetry.event("pipeline.finished", {
      status: lastRun.status,
      exitCode: code,
      durationMs,
    });
    currentRun = null;
  });

  return { accepted: true, running: true, startedAt };
}

export function createAppServer() {
  return createServer(async (request, response) => {
    const started = performance.now();
    try {
      const url = new URL(request.url, "http://localhost");
      telemetry.increment("scan.radar.http.request", 1, [`route:${url.pathname}`]);

      if (request.method === "GET" && url.pathname === "/api/dashboard") {
        sendJson(response, 200, await loadDashboardData());
        return;
      }

      if (request.method === "GET" && url.pathname === "/api/run") {
        sendJson(response, 200, {
          running: Boolean(currentRun),
          currentRun,
          last: lastRun,
          lastRun,
        });
        return;
      }

      if (request.method === "POST" && url.pathname === "/api/run") {
        const body = await readJsonBody(request);
        sendJson(response, 202, startPipeline({ useBedrock: Boolean(body.useBedrock) }));
        return;
      }

      if (request.method === "GET" && url.pathname === "/api/health") {
        sendJson(response, 200, {
          ok: true,
          demoRoot,
          hasRunScript: await exists(runScriptPath),
          hasSummary: await exists(summaryPath),
          hasRecommendations: await exists(recommendationsPath),
          hasInsights: await exists(insightsPath),
          running: Boolean(currentRun),
        });
        return;
      }

      if (request.method === "GET" && url.pathname === "/api/observability") {
        sendJson(response, 200, await loadObservabilityStatus());
        return;
      }

      if (request.method === "GET") {
        await serveStatic(request, response);
        return;
      }

      sendJson(response, 405, { error: "Method not allowed" });
    } catch (error) {
      telemetry.increment("scan.radar.http.error");
      telemetry.event("http.error", { message: error.message });
      sendJson(response, 500, { error: error.message });
    } finally {
      const url = new URL(request.url, "http://localhost");
      telemetry.timing("scan.radar.http.duration_ms", Math.round(performance.now() - started), [
        `route:${url.pathname}`,
      ]);
    }
  });
}

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function loadObservabilityStatus() {
  const recentEvents = await readRecentEvents();
  const agent = await readDatadogAgentInfo();
  await telemetry.event("observability.checked", {
    agentOk: Boolean(agent.ok),
    version: agent.version ?? null,
  });
  return {
    datadogAgent: agent,
    dogstatsd: {
      host: "127.0.0.1",
      port: 8125,
      protocol: "udp",
    },
    service: "scan-opportunity-radar",
    metricsPrefix: "scan.radar",
    eventsPath,
    recentEvents,
  };
}

async function readRecentEvents() {
  try {
    const content = await fs.readFile(eventsPath, "utf8");
    return content
      .trim()
      .split(/\r?\n/)
      .filter(Boolean)
      .slice(-20)
      .map((line) => JSON.parse(line));
  } catch {
    return [];
  }
}

async function readDatadogAgentInfo() {
  try {
    const response = await fetch("http://127.0.0.1:8126/info");
    if (!response.ok) {
      return { ok: false, status: response.status };
    }
    const info = await response.json();
    return {
      ok: true,
      version: info.version,
      apmPort: info.config?.receiver_port ?? 8126,
      dogstatsdPort: info.config?.statsd_port ?? 8125,
    };
  } catch (error) {
    return { ok: false, error: error.message };
  }
}

async function publishCurrentSnapshot() {
  try {
    const [summaryText, csvText] = await Promise.all([
      fs.readFile(summaryPath, "utf8"),
      fs.readFile(recommendationsPath, "utf8"),
    ]);
    publishBusinessMetrics(JSON.parse(summaryText), parseCsv(csvText));
  } catch (error) {
    telemetry.increment("scan.radar.metrics_heartbeat.error");
    telemetry.event("metrics_heartbeat.error", { message: error.message });
  }
}

export function startMetricsHeartbeat({ intervalMs = 15000 } = {}) {
  publishCurrentSnapshot();
  const timer = setInterval(publishCurrentSnapshot, intervalMs);
  timer.unref?.();
  return timer;
}

if (process.argv[1] === __filename) {
  const port = Number(process.env.PORT || 4177);
  createAppServer().listen(port, () => {
    console.log(`Scan Opportunity Radar rodando em http://localhost:${port}`);
  });
  startMetricsHeartbeat({ intervalMs: Number(process.env.METRICS_HEARTBEAT_MS || 15000) });
}
