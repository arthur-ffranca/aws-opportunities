import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  buildDogStatsDMetric,
  createTelemetry,
  sanitizeTag,
} from "../telemetry.mjs";

test("buildDogStatsDMetric formats metric name, value, type and tags", () => {
  const line = buildDogStatsDMetric("scan.radar.request", 1, "c", [
    "service:scan opportunity radar",
    "route:/api/dashboard",
  ]);

  assert.equal(
    line,
    "scan.radar.request:1|c|#service:scan_opportunity_radar,route:_api_dashboard",
  );
});

test("sanitizeTag keeps Datadog tags stable", () => {
  assert.equal(sanitizeTag("acao:REVER PRECO"), "acao:rever_preco");
  assert.equal(sanitizeTag("produto:CHOC 90G / BR"), "produto:choc_90g_br");
});

test("telemetry writes audit events even when UDP metrics are best-effort", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "scan-telemetry-"));
  const telemetry = createTelemetry({
    eventsPath: path.join(dir, "events.jsonl"),
    enabled: true,
    udp: false,
    defaultTags: ["service:test"],
  });

  await telemetry.event("pipeline.started", { source: "unit-test" });
  const content = await fs.readFile(path.join(dir, "events.jsonl"), "utf8");
  const event = JSON.parse(content.trim());

  assert.equal(event.name, "pipeline.started");
  assert.equal(event.source, "unit-test");
  assert.deepEqual(event.tags, ["service:test"]);
});
