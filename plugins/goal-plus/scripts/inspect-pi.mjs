// Resolve installed Pi resources without loading extensions or calling a model.
import { realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const [pi, agentDir, cwd] = process.argv.slice(2);
const core = join(dirname(realpathSync(pi)), "core");
const { SettingsManager } = await import(pathToFileURL(join(core, "settings-manager.js")));
const { DefaultPackageManager } = await import(pathToFileURL(join(core, "package-manager.js")));
const settingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted: true });
const manager = new DefaultPackageManager({ cwd, agentDir, settingsManager });
const resources = await manager.resolve(async () => "skip");
console.log(JSON.stringify({ extensions: resources.extensions, skills: resources.skills }));
