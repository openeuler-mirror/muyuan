// Resolve installed Pi resources without loading extensions or calling a model.
import { readFileSync, realpathSync } from "node:fs";
import { findPackageJSON } from "node:module";
import { pathToFileURL } from "node:url";

const [pi, agentDir, cwd] = process.argv.slice(2);
const packageJson = findPackageJSON(pathToFileURL(realpathSync(pi)));
if (!packageJson) throw new Error(`Cannot locate the Pi package from ${pi}`);
const manifest = JSON.parse(readFileSync(packageJson, "utf8"));
if (manifest.name !== "@earendil-works/pi-coding-agent") {
  throw new Error(`Unexpected Pi package: ${manifest.name ?? "unknown"}`);
}
const entry = manifest.exports?.["."]?.import;
if (typeof entry !== "string" || !entry.startsWith("./")) {
  throw new Error("Pi package does not expose an ESM public entry point");
}
const { SettingsManager, DefaultPackageManager } = await import(
  new URL(entry, pathToFileURL(packageJson)),
);
const settingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted: true });
const manager = new DefaultPackageManager({ cwd, agentDir, settingsManager });
const resources = await manager.resolve(async () => "skip");
console.log(JSON.stringify({ extensions: resources.extensions, skills: resources.skills }));
