// Generate dist/LICENSES.txt: an aggregated notice of every bundled runtime
// package with its licence, and fail the build if any licence is outside the
// allowed set. Only production dependencies are traversed, because only those
// end up in the shipped bundle. Dev tooling (vite, playwright, typescript) is
// not bundled and is not listed.
//
// stdlib only; no network. Runs after `vite build`.
import { readFileSync, existsSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const nodeModules = join(root, "node_modules");
const distDir = join(root, "dist");

const ALLOWED = new Set([
  "MIT",
  "ISC",
  "BSD-2-Clause",
  "BSD-3-Clause",
  "Apache-2.0",
  "0BSD",
  "CC0-1.0",
]);

function readPkg(dir) {
  const p = join(dir, "package.json");
  if (!existsSync(p)) return null;
  try {
    return JSON.parse(readFileSync(p, "utf8"));
  } catch {
    return null;
  }
}

// Resolve a dependency directory using node's nesting rules: prefer a nested
// copy under the requester, else fall back to the top-level install.
function resolveDep(name, fromDir) {
  const nested = join(fromDir, "node_modules", name);
  if (existsSync(join(nested, "package.json"))) return nested;
  const top = join(nodeModules, name);
  if (existsSync(join(top, "package.json"))) return top;
  return null;
}

function licenseOf(pkg) {
  if (!pkg) return "UNKNOWN";
  if (typeof pkg.license === "string") return pkg.license;
  if (pkg.license && typeof pkg.license === "object" && pkg.license.type) return pkg.license.type;
  if (Array.isArray(pkg.licenses) && pkg.licenses[0] && pkg.licenses[0].type) {
    return pkg.licenses.map((l) => l.type).join(" OR ");
  }
  return "UNKNOWN";
}

function findLicenseText(dir) {
  for (const name of [
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "license",
    "LICENCE",
    "LICENSE-MIT",
    "MIT-LICENSE",
    "MIT-LICENSE.txt",
  ]) {
    const p = join(dir, name);
    if (existsSync(p)) {
      try {
        return readFileSync(p, "utf8").trim();
      } catch {
        /* ignore */
      }
    }
  }
  return null;
}

const rootPkg = readPkg(root);
const runtimeDeps = Object.keys(rootPkg.dependencies || {});

const seen = new Map(); // name -> { version, license, dir }
const queue = runtimeDeps.map((name) => ({ name, from: root }));

while (queue.length) {
  const { name, from } = queue.shift();
  if (seen.has(name)) continue;
  const dir = resolveDep(name, from);
  if (!dir) continue;
  const pkg = readPkg(dir);
  if (!pkg) continue;
  seen.set(name, { version: pkg.version, license: licenseOf(pkg), dir });
  for (const dep of Object.keys(pkg.dependencies || {})) {
    if (!seen.has(dep)) queue.push({ name: dep, from: dir });
  }
}

const offenders = [];
for (const [name, info] of seen) {
  // Normalise "(MIT OR X)" / "MIT AND X" style expressions leniently: every
  // named token must be individually allowed.
  const parts = info.license
    .replace(/[()]/g, " ")
    .split(/\s+(?:OR|AND)\s+/i)
    .map((t) => t.trim())
    .filter(Boolean);
  const ok = parts.length > 0 && parts.every((t) => ALLOWED.has(t));
  if (!ok) offenders.push(`${name}@${info.version}: ${info.license}`);
}

if (offenders.length) {
  console.error("Disallowed licence(s) in the runtime bundle:");
  for (const o of offenders) console.error("  " + o);
  console.error("Allowed set: " + [...ALLOWED].join(", "));
  process.exit(1);
}

const names = [...seen.keys()].sort();
const lines = [];
lines.push("Third-party notices for the bundled Grogu plan workspace");
lines.push("=========================================================");
lines.push("");
lines.push(
  "This file is generated at build time by scripts/licenses.mjs. It lists every",
);
lines.push(
  "runtime package bundled into dist/app.js together with its licence. The build",
);
lines.push(
  "fails if any bundled package carries a licence outside the allowed set:",
);
lines.push("  " + [...ALLOWED].join(", "));
lines.push("");
lines.push("React Flow (@xyflow/react) attribution is displayed in the running UI.");
lines.push("");
for (const name of names) {
  const info = seen.get(name);
  lines.push("-".repeat(72));
  lines.push(`${name}@${info.version}  —  ${info.license}`);
  const text = findLicenseText(info.dir);
  if (text) {
    lines.push("");
    lines.push(text);
  }
  lines.push("");
}

if (!existsSync(distDir)) {
  console.error("dist/ does not exist; run `vite build` first.");
  process.exit(1);
}
// End with exactly one trailing newline: the per-package loop leaves a blank
// separator line, and `join + "\n"` would otherwise emit a blank line at EOF,
// which `git diff --check` rejects as a whitespace error.
const body = lines.join("\n").replace(/\s+$/, "") + "\n";
writeFileSync(join(distDir, "LICENSES.txt"), body, "utf8");
console.log(`Wrote dist/LICENSES.txt covering ${names.length} runtime package(s).`);
