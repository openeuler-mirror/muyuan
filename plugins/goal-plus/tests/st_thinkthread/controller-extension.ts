import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { existsSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const CONFIG_NAME = ".goal-plus-thinkthread-st.json";
const RESULT_NAME = ".goal-plus-thinkthread-st-driver.json";

export default function (pi: ExtensionAPI) {
	pi.on("session_start", async (_event, ctx) => {
		if (process.env.GOAL_PLUS_PI_ROLE !== "main") return;
		const config = join(ctx.cwd, CONFIG_NAME);
		if (!existsSync(config)) return;
		const source = process.env.GOAL_PLUS_SOURCE_PATH;
		const python = process.env.GOAL_PLUS_PYTHON;
		if (!source || !python) {
			writeFileSync(
				join(ctx.cwd, RESULT_NAME),
				JSON.stringify({ ok: false, error: "Goal Plus Root environment is incomplete" }),
			);
			return;
		}
		const driver = join(source, "tests", "st_thinkthread", "driver.py");
		const result = await pi.exec(python, [driver, "--config", config], {
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
