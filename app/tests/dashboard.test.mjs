import assert from "node:assert/strict";
import test from "node:test";
import { createAppServer, loadDashboardData, publishBusinessMetrics } from "../server.mjs";

test("loadDashboardData exposes Scan KPIs, ranking and insights", async () => {
  const data = await loadDashboardData();

  assert.equal(data.summary.products, 365);
  assert.equal(data.summary.brands, 72);
  assert.equal(data.summary.manufacturers, 22);
  assert.equal(data.actionCounts["INCLUIR NO SORTIMENTO"], 54);
  assert.equal(data.actionCounts["REVER PRECO"], 57);
  assert.ok(data.recommendations.length >= 10);
  assert.equal(data.recommendations[0].rank, 1);
  assert.ok(data.recommendations[0].produto.length > 0);
  assert.ok(data.recommendations[0].potencialReceitaMensal > 0);
  assert.ok(data.summary.potential_revenue_total > 0);
  assert.ok(data.insights.includes("Bedrock Insights"));
});

test("static server returns 404 for missing files without crashing", async () => {
  const server = createAppServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();

  try {
    const missing = await fetch(`http://127.0.0.1:${port}/favicon.ico`);
    assert.equal(missing.status, 404);

    const health = await fetch(`http://127.0.0.1:${port}/api/health`);
    assert.equal(health.status, 200);

    const observability = await fetch(`http://127.0.0.1:${port}/api/observability`);
    assert.equal(observability.status, 200);
    const payload = await observability.json();
    assert.equal(payload.service, "scan-opportunity-radar");
    assert.equal(payload.metricsPrefix, "scan.radar");
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("publishBusinessMetrics republishes snapshot gauges for Datadog charts", async () => {
  const emitted = [];
  const fakeTelemetry = {
    gauge(name, value, tags = []) {
      emitted.push({ name, value, tags });
    },
  };

  publishBusinessMetrics(
    {
      products: 365,
      action_counts: { "REVER PRECO": 57 },
      potential_revenue_total: 23793.12,
      potential_revenue_by_action: { "REVER PRECO": 8743.76 },
      comparison_by_action: {
        "REVER PRECO": {
          price_client_avg: 5.5,
          price_competitor_avg: 4.5,
          units_client_avg: 75,
          units_competitor_avg: 125,
          price_gap_pct_avg: 22.5,
        },
      },
    },
    [{}, {}],
    fakeTelemetry,
  );

  assert.deepEqual(emitted, [
    { name: "scan.radar.products", value: 365, tags: [] },
    { name: "scan.radar.recommendations", value: 2, tags: [] },
    { name: "scan.radar.potential_revenue_total", value: 23793.12, tags: [] },
    { name: "scan.radar.action_count", value: 57, tags: ["action:REVER PRECO"] },
    { name: "scan.radar.potential_revenue", value: 8743.76, tags: ["action:REVER PRECO"] },
    { name: "scan.radar.potential_revenue.rever_preco", value: 8743.76, tags: [] },
    { name: "scan.radar.compare.rever_preco.price_client_avg", value: 5.5, tags: [] },
    { name: "scan.radar.compare.rever_preco.price_competitor_avg", value: 4.5, tags: [] },
    { name: "scan.radar.compare.rever_preco.units_client_avg", value: 75, tags: [] },
    { name: "scan.radar.compare.rever_preco.units_competitor_avg", value: 125, tags: [] },
    { name: "scan.radar.compare.rever_preco.price_gap_pct_avg", value: 22.5, tags: [] },
  ]);
});
