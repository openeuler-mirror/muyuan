import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const CONFIG_NAME = ".goal-plus-thinkthread-st.json";
const RESULT_NAME = ".goal-plus-thinkthread-st-driver.json";

export default function (pi: ExtensionAPI) {
	pi.on("session_start", async (_event, ctx) => {
		if (process.env.GOAL_PLUS_PI_ROLE !== "main") return;
		const config = join(ctx.cwd, CONFIG_NAME);
		if (!existsSync(config)) return;
		const settings = JSON.parse(readFileSync(config, "utf8"));
		settings.native_session_id = ctx.sessionManager.getSessionId();
		writeFileSync(config, JSON.stringify(settings));
		const source = process.env.GOAL_PLUS_SOURCE_PATH;
		const python = process.env.GOAL_PLUS_PYTHON;
		if (!source || !python) {
			writeFileSync(
				join(ctx.cwd, RESULT_NAME),
				JSON.stringify({ ok: false, error: "Goal Plus Root environment is incomplete" }),
			);
			return;
		}
		const driver = join(dirname(fileURLToPath(import.meta.url)), "driver.py");
		const result = await pi.exec(python, ["-E", "-s", driver, "--config", config], {
			cwd: ctx.cwd,
			timeout: 1_200_000,
		});
		writeFileSync(
			join(ctx.cwd, RESULT_NAME),
			JSON.stringify({
				ok: result.code === 0,
				code: result.code,
				stdout: result.stdout.slice(-16_000),
				stderr: result.stderr.slice(-16_000),
			}),
		);
	});
}
