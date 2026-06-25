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

async function runPerception(config, signal) {
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
    [cliPath, "perception", signal],
    { cwd: consumerRoot, env, timeout: c.timeoutMs, maxBuffer: 1024 * 1024 },
  );
  const text = (stdout || stderr || "").trim();
  if (!text) throw new Error(`empty response from io_cli.py for signal ${signal}`);
  let parsed;
  try { parsed = JSON.parse(text); }
  catch (error) { throw new Error(`non-JSON response for ${signal}: ${text.slice(0, 500)}`); }
  return parsed;
}

function toolResult(payload) {
  return { content: [{ type: "text", text: JSON.stringify(payload) }] };
}

export default definePluginEntry({
  id: "feedling-io-tools",
  name: "Feedling IO Tools",
  description: "Expose Feedling IO perception CLI as native OpenClaw tools.",
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
  },
});
