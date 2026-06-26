import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFileSync, existsSync } from "node:fs";
import path from "node:path";

const execFileAsync = promisify(execFile);
const SIGNALS = ["now","location","weather","motion","calendar","steps","sleep","workout","vitals","focus","audio_route","reminders","activity","body","metabolic","cycle","mood"];

// Declared in code so the gateway recognizes and forwards the configured config
// to register(). (Declaring it only in openclaw.plugin.json was not enough — the
// runtime definePluginEntry defaulted to an empty schema and config arrived empty.)
const CONFIG_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    consumerRoot: { type: "string" },
    serviceEnvPath: { type: "string" },
    pythonPath: { type: "string" },
    timeoutMs: { type: "integer", minimum: 1000 },
  },
};

function parseEnvFile(filePath) {
  const raw = readFileSync(filePath, "utf8");
  const env = {};
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    env[key] = value;
  }
  return env;
}

// No hardcoded deployment paths. config (openclaw.json) first, then env
// (per-host deployment config), then fail loudly — never guess a path.
function resolveConfig(config) {
  const cfg = config || {};
  const consumerRoot = cfg.consumerRoot || process.env.FEEDLING_CONSUMER_ROOT;
  const serviceEnvPath = cfg.serviceEnvPath || process.env.FEEDLING_SERVICE_ENV;
  if (!consumerRoot || !serviceEnvPath) {
    throw new Error(
      "feedling-io-tools not configured: set plugins.entries['feedling-io-tools'].config.{consumerRoot,serviceEnvPath} in openclaw.json, or FEEDLING_CONSUMER_ROOT / FEEDLING_SERVICE_ENV in the environment.",
    );
  }
  return {
    consumerRoot,
    serviceEnvPath,
    pythonPath: cfg.pythonPath || process.env.FEEDLING_PYTHON || "python3",
    timeoutMs: cfg.timeoutMs || 20000,
  };
}

// Generic io_cli.py invoker. `args` is the full argv after the script path
// (e.g. ["perception", "now"] or ["memory-index", "--limit", "5"]).
async function runCli(config, args) {
  const c = resolveConfig(config);
  const consumerRoot = path.resolve(c.consumerRoot);
  const serviceEnvPath = path.resolve(c.serviceEnvPath);
  const cliPath = path.join(consumerRoot, "tools", "io_cli.py");

  if (!existsSync(serviceEnvPath)) throw new Error(`service env file not found: ${serviceEnvPath}`);
  if (!existsSync(cliPath)) throw new Error(`io_cli.py not found: ${cliPath}`);

  const fileEnv = parseEnvFile(serviceEnvPath);
  const env = {
    ...process.env,
    FEEDLING_API_URL: fileEnv.FEEDLING_API_URL,
    FEEDLING_API_KEY: fileEnv.FEEDLING_API_KEY,
    FEEDLING_ENCLAVE_URL: fileEnv.FEEDLING_ENCLAVE_URL,
  };

  const { stdout, stderr } = await execFileAsync(
    c.pythonPath,
    [cliPath, ...args],
    { cwd: consumerRoot, env, timeout: c.timeoutMs, maxBuffer: 1024 * 1024 },
  );
  const text = (stdout || stderr || "").trim();
  if (!text) throw new Error(`empty response from io_cli.py for ${args.join(" ")}`);
  let parsed;
  try { parsed = JSON.parse(text); }
  catch (error) { throw new Error(`non-JSON response for ${args.join(" ")}: ${text.slice(0, 500)}`); }
  return parsed;
}

async function runPerception(config, signal) {
  return runCli(config, ["perception", signal]);
}

// Build io_cli argv from a tool's structured params. Order: positional first,
// then flags. Booleans become store_true flags only when true.
function flagsFromParams(params, spec) {
  const out = [];
  for (const [key, flag, kind] of spec) {
    const v = params ? params[key] : undefined;
    if (v === undefined || v === null || v === "") continue;
    if (kind === "bool") { if (v) out.push(flag); }
    else { out.push(flag, String(v)); }
  }
  return out;
}

function toolResult(payload) {
  return { content: [{ type: "text", text: JSON.stringify(payload) }] };
}

export default definePluginEntry({
  id: "feedling-io-tools",
  name: "Feedling IO Tools",
  description: "Expose Feedling IO perception/memory/screen CLI as native OpenClaw tools.",
  configSchema: CONFIG_SCHEMA,
  register(api, config = {}) {
    for (const signal of SIGNALS) {
      api.registerTool({
        name: `perception_${signal}`,
        description: `Read Feedling perception signal: ${signal} (provider-safe tool name for perception.${signal})`,
        parameters: { type: "object", properties: {}, additionalProperties: false },
        async execute() {
          try {
            const payload = await runPerception(config, signal);
            return toolResult(payload);
          } catch (err) {
            return toolResult({ ok: false, error: (err && err.message) ? err.message : String(err), signal });
          }
        },
      });
    }

    // Helper: register a tool that maps structured params → io_cli argv.
    const registerCli = ({ name, description, parameters, build }) => {
      api.registerTool({
        name,
        description,
        parameters,
        async execute(params = {}) {
          try {
            return toolResult(await runCli(config, build(params || {})));
          } catch (err) {
            return toolResult({ ok: false, error: (err && err.message) ? err.message : String(err), tool: name });
          }
        },
      });
    };

    // memory.index — compact readside index (plaintext-safe).
    registerCli({
      name: "memory_index",
      description: "Read a compact index of the user's memory cards (provider-safe name for memory.index). Returns id/summary/bucket/threads/score — use memory_fetch for verbatim content.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          limit: { type: "integer", minimum: 1, maximum: 200, description: "max cards (default 50)" },
          bucket: { type: "string", description: "filter by bucket name" },
          thread: { type: "string", description: "filter by thread/dimension tag" },
          query: { type: "string", description: "free-text relevance query" },
          ambient: { type: "boolean", description: "ambient/background selection mode" },
          include_sensitive: { type: "boolean", description: "include sensitive-classed cards" },
        },
      },
      build: (p) => ["memory-index", ...flagsFromParams(p, [
        ["limit", "--limit", "value"],
        ["bucket", "--bucket", "value"],
        ["thread", "--thread", "value"],
        ["query", "--query", "value"],
        ["ambient", "--ambient", "bool"],
        ["include_sensitive", "--include-sensitive", "bool"],
      ])],
    });

    // memory.fetch — verbatim decrypted cards by id (plaintext-safe).
    registerCli({
      name: "memory_fetch",
      description: "Fetch verbatim decrypted memory cards by id (provider-safe name for memory.fetch). Pass ids from memory_index.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["ids"],
        properties: {
          ids: { type: "array", items: { type: "string" }, minItems: 1, description: "memory card ids" },
          limit: { type: "integer", minimum: 1, maximum: 100, description: "max cards (default 20)" },
          include_archived: { type: "boolean" },
          include_superseded: { type: "boolean" },
        },
      },
      build: (p) => ["memory-fetch", ...(Array.isArray(p.ids) ? p.ids.map(String) : []), ...flagsFromParams(p, [
        ["limit", "--limit", "value"],
        ["include_archived", "--include-archived", "bool"],
        ["include_superseded", "--include-superseded", "bool"],
      ])],
    });

    // screen.recent — recent frame metadata (no pixels).
    registerCli({
      name: "screen_recent",
      description: "List recent screen frame metadata (provider-safe name for screen.recent). No pixels; use screen_read for a caption.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: { limit: { type: "integer", minimum: 1, maximum: 100, description: "max frames (default 10)" } },
      },
      build: (p) => ["screen-recent", ...flagsFromParams(p, [["limit", "--limit", "value"]])],
    });

    // screen.read — decrypted caption/ocr for a frame (latest by default).
    registerCli({
      name: "screen_read",
      description: "Read the decrypted caption/ocr of a screen frame (provider-safe name for screen.read). Defaults to the latest frame; pixels off unless include_image.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          frame_id: { type: "string", description: "frame id; default = latest" },
          include_image: { type: "boolean", description: "include base64 JPEG (large)" },
        },
      },
      build: (p) => ["screen-read", ...flagsFromParams(p, [
        ["frame_id", "--frame-id", "value"],
        ["include_image", "--include-image", "bool"],
      ])],
    });
  },
});
