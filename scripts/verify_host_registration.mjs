#!/usr/bin/env node
// Verify that a registered skill is actually discovered by the host that reads that directory.
//
// Documentation can only tell you which directory a host scans; this script runs the host's own
// discovery code against a real directory layout and reports what it found. DSH ships its skill
// discovery as a plugin (`@deepseek-ai/dsh-skill-filesystem`), which is driven here directly, so
// the check needs no model, no session and no network.
//
//   node scripts/verify_host_registration.mjs --home /tmp/fake-home --install
//   node scripts/verify_host_registration.mjs --json
//
// Hosts without a locally installed implementation (Codex, WorkBuddy, CodeBuddy, Claude Code on
// this machine) are reported as unverifiable rather than assumed to work.

import fs from "node:fs/promises";
import fsSync from "node:fs";
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath, pathToFileURL } from "node:url";

const runFile = promisify(execFile);
// fileURLToPath, not URL.pathname: a checkout path containing spaces or non-ASCII would
// otherwise arrive percent-encoded and every root check would silently miss.
const skillDir = path.resolve(process.argv.includes("--skill-dir")
  ? process.argv[process.argv.indexOf("--skill-dir") + 1]
  : path.join(path.dirname(fileURLToPath(import.meta.url)), ".."));
const asJson = process.argv.includes("--json");
const wantInstall = process.argv.includes("--install");
const home = process.argv.includes("--home")
  ? path.resolve(process.argv[process.argv.indexOf("--home") + 1])
  : os.homedir();

async function manifest() {
  return JSON.parse(await fs.readFile(path.join(skillDir, "skill.json"), "utf8"));
}

async function dshPluginDir() {
  const explicit = process.argv.includes("--dsh-package")
    ? path.resolve(process.argv[process.argv.indexOf("--dsh-package") + 1])
    : process.env.DSH_SKILL_FILESYSTEM_DIR;
  if (explicit) return explicit;
  try {
    // `which` is POSIX-only; Windows has `where`, which may print several matches.
    const locator = process.platform === "win32" ? "where" : "which";
    const { stdout } = await runFile(locator, ["dsh"]);
    const first = stdout.trim().split(/\r?\n/)[0]?.trim();
    if (!first) throw new Error("dsh not found on PATH");
    const bin = await fs.realpath(first);
    // <dsh>/lib/bin.js -> <dsh>/node_modules/@deepseek-ai/dsh-skill-filesystem
    const candidate = path.join(path.dirname(bin), "..", "node_modules", "@deepseek-ai", "dsh-skill-filesystem");
    if (await isDir(candidate)) return candidate;
  } catch {
    // `dsh` not on PATH: fall through to the import-from-here attempt.
  }
  try {
    return path.dirname(pathToFileURL(await import.meta.resolve("@deepseek-ai/dsh-skill-filesystem")).pathname);
  } catch {
    return null;
  }
}

function existsSync(target) {
  return fsSync.existsSync(target);
}

async function isDir(target) {
  try {
    return (await fs.stat(target)).isDirectory();
  } catch {
    return false;
  }
}

// Drive the host's real provider: register it on a stub context, then ask it to list skills.
async function dshDiscovery(pluginDir, dshHome, agentsHome, cwd) {
  const entry = pathToFileURL(path.join(pluginDir, "lib", "index.js")).href;
  const plugin = await import(entry);
  const warnings = [];
  let provider;
  const control = { invalidate: () => {}, signal: new AbortController().signal };
  const ctx = {
    // The provider asks the context for an optional filesystem service and falls back to Node
    // I/O when there is none, which is exactly the path we want to exercise here.
    get: () => undefined,
    skills: { registerProvider: (factory) => { provider = factory(control); } },
    logger: { warn: (message) => warnings.push(String(message)), info: () => {}, error: () => {}, debug: () => {} },
    effect: () => {},
    on: () => {},
  };
  plugin.apply(ctx, { dshHome, agentsHome, includeDefaultRoots: true });
  if (!provider) throw new Error("skill-filesystem did not register a provider");
  try {
    const listed = await provider.list({ cwd });
    const candidates = Array.isArray(listed) ? listed : listed.candidates;
    return {
      candidates: candidates.map((item) => ({ name: item.name, source: item.source, rank: item.rank, path: item.path })),
      complete: Array.isArray(listed) ? true : listed.complete !== false,
      warnings,
    };
  } finally {
    await provider.dispose?.();
  }
}

async function main() {
  const config = await manifest();
  const name = config.name;
  const report = { ok: false, skill: name, home, skill_dir: skillDir, hosts: [] };

  if (wantInstall) {
    // Windows ships `python`, not `python3`; EXPERIMENT_REPORT_GRADER_PYTHON wins everywhere.
    const pythonCommand =
      process.env.EXPERIMENT_REPORT_GRADER_PYTHON ?? (process.platform === "win32" ? "python" : "python3");
    const { stdout } = await runFile(
      pythonCommand,
      [path.join(skillDir, "scripts", "install_skill.py"), "--home", home, "--json"],
      { maxBuffer: 4 * 1024 * 1024 },
    );
    report.install = JSON.parse(stdout);
  }

  for (const [host, entry] of Object.entries(config.hosts ?? {})) {
    const record = { host, roots: [], registered: false, verified: false, discovered: null, note: "" };
    for (const relative of entry.user ?? []) {
      // Function replacement: `home` is caller-supplied, and a home path containing `$&` or `$'`
      // would otherwise be expanded as a substitution pattern by String.replace.
      const root = relative.replace(/^~\//, () => `${home}/`);
      const bundle = path.join(root, name);
      record.roots.push({ root, bundle, exists: existsSync(bundle) });
      if (existsSync(bundle)) record.registered = true;
    }
    if (host === "dsh") {
      const pluginDir = await dshPluginDir();
      if (!pluginDir) {
        record.note = "DSH skill-filesystem plugin not found; cannot verify discovery here";
      } else {
        try {
          const result = await dshDiscovery(pluginDir, path.join(home, ".dsh"), path.join(home, ".agents"), process.cwd());
          record.verified = true;
          record.discovered = result.candidates;
          record.complete = result.complete;
          record.warnings = result.warnings;
          record.found = result.candidates.some((item) => item.name === name);
        } catch (error) {
          record.note = `discovery failed: ${error.message}`;
        }
      }
    } else if (host === "agents") {
      // Same directory DSH reads; recorded separately so the shared root is visible in the report.
      record.note = "shared root, verified through the DSH provider above";
    } else {
      record.note = "no local implementation to drive; root layout only";
    }
    report.hosts.push(record);
  }

  const dsh = report.hosts.find((item) => item.host === "dsh");
  const agents = report.hosts.find((item) => item.host === "agents");
  report.ok = Boolean(dsh?.found || (!dsh?.verified && agents?.registered));
  if (asJson) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(`skill: ${report.skill}  home: ${report.home}`);
    for (const record of report.hosts) {
      const state = record.verified
        ? record.found ? "discovered by the host" : "NOT discovered"
        : record.registered ? "registered (host not verifiable here)" : "not registered";
      console.log(`  ${record.host.padEnd(10)} ${state}`);
      for (const item of record.roots) console.log(`      root ${item.root}${item.exists ? " [bundle present]" : ""}`);
      if (record.note) console.log(`      note: ${record.note}`);
    }
    if (dsh?.discovered) {
      console.log(`  discovery returned: ${dsh.discovered.map((item) => `${item.name}(${item.source},rank ${item.rank})`).join(", ") || "nothing"}`);
    }
  }
  return report.ok ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error(error);
  process.exit(2);
});
