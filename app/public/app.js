const state = {
  data: null,
  action: "",
  query: "",
};

const kpiGrid = document.querySelector("#overview");
const rankingBody = document.querySelector("#rankingBody");
const actionFilter = document.querySelector("#actionFilter");
const searchInput = document.querySelector("#searchInput");
const insightsBody = document.querySelector("#insightsBody");
const healthStatus = document.querySelector("#healthStatus");
const runButton = document.querySelector("#runButton");
const runState = document.querySelector("#runState");
const runLog = document.querySelector("#runLog");
const bedrockToggle = document.querySelector("#bedrockToggle");
const observabilityBody = document.querySelector("#observabilityBody");

function formatNumber(value) {
  if (typeof value === "string") return value;
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 }).format(value);
}

function formatCurrency(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat("pt-BR", {
    style: "currency",
    currency: "BRL",
    maximumFractionDigits: 0,
  }).format(value);
}

function actionClass(action) {
  if (action.includes("INCLUIR")) return "include";
  if (action.includes("PRECO")) return "price";
  if (action.includes("GIRO")) return "speed";
  if (action.includes("DISTRIBUICAO")) return "distribution";
  return "monitor";
}

function renderMarkdown(markdown) {
  return markdown
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .split(/\n/)
    .map((line) => {
      if (line.startsWith("# ")) return `<h1>${line.slice(2)}</h1>`;
      if (line.startsWith("## ")) return `<h2>${line.slice(3)}</h2>`;
      if (line.startsWith("- ")) return `<li>${line.slice(2)}</li>`;
      if (!line.trim()) return "";
      return `<p>${line}</p>`;
    })
    .join("")
    .replace(/(<li>.*<\/li>)/gs, "<ul>$1</ul>");
}

function renderKpis(data) {
  const counts = data.actionCounts;
  const cards = [
    ["Produtos", data.summary.products, "itens analisados"],
    ["Marcas", data.summary.brands, "no sortimento"],
    ["Fabricantes", data.summary.manufacturers, "mapeados"],
    ["Incluir", counts["INCLUIR NO SORTIMENTO"] ?? 0, "gap de sortimento"],
    ["Rever preco", counts["REVER PRECO"] ?? 0, "risco competitivo"],
    ["Potencial", formatCurrency(data.summary.potential_revenue_total ?? 0), "R$/mes loja-equiv."],
  ];

  kpiGrid.innerHTML = cards
    .map(
      ([title, value, subtitle]) => `
        <article class="kpi-card">
          <span>${title}</span>
          <strong>${formatNumber(value)}</strong>
          <span>${subtitle}</span>
        </article>
      `,
    )
    .join("");
}

function populateActions(data) {
  const actions = Object.keys(data.actionCounts);
  actionFilter.innerHTML = '<option value="">Todas as acoes</option>';
  for (const action of actions) {
    const option = document.createElement("option");
    option.value = action;
    option.textContent = `${action} (${data.actionCounts[action]})`;
    actionFilter.appendChild(option);
  }
}

function renderRanking() {
  const query = state.query.trim().toLowerCase();
  const rows = state.data.recommendations
    .filter((row) => !state.action || row.acao === state.action)
    .filter((row) => {
      if (!query) return true;
      return `${row.produto} ${row.marca} ${row.fabricante}`.toLowerCase().includes(query);
    })
    .slice(0, 80);

  rankingBody.innerHTML = rows
    .map(
      (row) => `
        <tr>
          <td class="score">#${row.rank}</td>
          <td class="product-cell">
            <strong>${row.produto}</strong>
            <span>${row.marca} · ${row.fabricante}</span>
            <p>${row.racional}</p>
          </td>
          <td><span class="action-badge ${actionClass(row.acao)}">${row.acao}</span></td>
          <td class="score">${formatNumber(row.score)}</td>
          <td>
            <strong>${formatNumber(row.gapUnidades)}</strong>
            <div class="muted">unid/loja/mes</div>
          </td>
          <td>
            <strong>${formatCurrency(row.potencialReceitaMensal)}</strong>
            <div class="muted">mensal</div>
          </td>
        </tr>
      `,
    )
    .join("");
}

async function loadDashboard() {
  const response = await fetch("/api/dashboard");
  if (!response.ok) throw new Error("Dashboard indisponivel");
  state.data = await response.json();
  renderKpis(state.data);
  populateActions(state.data);
  renderRanking();
  insightsBody.innerHTML = renderMarkdown(state.data.insights);
  renderRunState(state.data.run);
}

async function loadHealth() {
  const response = await fetch("/api/health");
  const health = await response.json();
  healthStatus.textContent = health.ok ? "Ready" : "Check";
  healthStatus.className = `status-pill ${health.ok ? "ok" : "warn"}`;
}

function renderObservability(payload) {
  const agent = payload.datadogAgent;
  const status = agent.ok ? `Agent ${agent.version}` : "Agent indisponivel";
  const events = payload.recentEvents.slice(-5).reverse();

  observabilityBody.innerHTML = `
    <div class="datadog-status ${agent.ok ? "ok" : "warn"}">
      <strong>${status}</strong>
      <span>DogStatsD ${payload.dogstatsd.host}:${payload.dogstatsd.port}</span>
      <span>Metric prefix: ${payload.metricsPrefix}</span>
      <span>Potential metric: scan.radar.potential_revenue_total</span>
    </div>
    <div class="event-list">
      ${
        events.length
          ? events
              .map(
                (event) => `
                  <article>
                    <strong>${event.name}</strong>
                    <span>${event.ts}</span>
                  </article>
                `,
              )
              .join("")
          : "<p>Nenhum evento local registrado ainda.</p>"
      }
    </div>
  `;
}

async function loadObservability() {
  const response = await fetch("/api/observability");
  if (!response.ok) throw new Error("Observabilidade indisponivel");
  renderObservability(await response.json());
}

function renderRunState(run) {
  if (run?.running) {
    runState.textContent = "Running";
    runButton.disabled = true;
    runLog.textContent = run.currentRun?.output || "Processando...";
    return;
  }

  runButton.disabled = false;
  if (run?.last) {
    runState.textContent = run.last.status === "completed" ? "Completed" : "Failed";
    runLog.textContent = [run.last.output, run.last.error].filter(Boolean).join("\n");
  } else {
    runState.textContent = "Idle";
  }
}

async function pollRun() {
  const response = await fetch("/api/run");
  const run = await response.json();
  renderRunState(run);
  if (run.running) {
    window.setTimeout(pollRun, 2000);
  } else {
    await loadDashboard();
    await loadObservability();
  }
}

runButton.addEventListener("click", async () => {
  runButton.disabled = true;
  runState.textContent = "Starting";
  runLog.textContent = "Disparando pipeline...";

  await fetch("/api/run", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ useBedrock: bedrockToggle.checked }),
  });

  pollRun();
});

actionFilter.addEventListener("change", () => {
  state.action = actionFilter.value;
  renderRanking();
});

searchInput.addEventListener("input", () => {
  state.query = searchInput.value;
  renderRanking();
});

await Promise.all([loadDashboard(), loadHealth(), loadObservability()]);
