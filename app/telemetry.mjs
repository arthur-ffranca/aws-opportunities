import dgram from "node:dgram";
import fs from "node:fs/promises";
import path from "node:path";

export function sanitizeTag(tag) {
  return String(tag)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_:\-.]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
}

export function buildDogStatsDMetric(name, value, type, tags = []) {
  const cleanTags = tags.map(sanitizeTag).filter(Boolean);
  const tagSuffix = cleanTags.length ? `|#${cleanTags.join(",")}` : "";
  return `${name}:${value}|${type}${tagSuffix}`;
}

export function createTelemetry({
  host = "127.0.0.1",
  port = 8125,
  eventsPath,
  enabled = true,
  udp = true,
  defaultTags = [],
} = {}) {
  async function writeEvent(name, payload = {}, tags = []) {
    if (!eventsPath) return;

    await fs.mkdir(path.dirname(eventsPath), { recursive: true });
    const event = {
      ts: new Date().toISOString(),
      name,
      tags: [...defaultTags, ...tags].map(sanitizeTag).filter(Boolean),
      ...payload,
    };
    await fs.appendFile(eventsPath, `${JSON.stringify(event)}\n`, "utf8");
  }

  function sendMetric(name, value, type, tags = []) {
    if (!enabled || !udp) return;
    const line = buildDogStatsDMetric(name, value, type, [...defaultTags, ...tags]);
    const socket = dgram.createSocket("udp4");
    socket.on("error", () => socket.close());
    socket.send(Buffer.from(line), port, host, () => socket.close());
  }

  return {
    increment(name, value = 1, tags = []) {
      sendMetric(name, value, "c", tags);
    },
    gauge(name, value, tags = []) {
      sendMetric(name, value, "g", tags);
    },
    timing(name, value, tags = []) {
      sendMetric(name, value, "ms", tags);
    },
    event: writeEvent,
  };
}
