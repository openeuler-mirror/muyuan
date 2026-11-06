/** Real Pi wiring probe. Every provider request is forbidden. */
import assert from "node:assert/strict";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const extension = resolve(dirname(fileURLToPath(import.meta.url)), "../../extensions/meta-tool.ts");
const source = fileURLToPath(import.meta.url);

export default function probe(pi: any) {
	pi.on("before_provider_request", () => { throw new Error("Provider calls forbidden by read-batch probe"); });
	pi.on("input", async (_event: any, ctx: any) => {
		try {
			const disabled = process.env.BATE_READ_BATCH_PROBE_DISABLED === "1";
			const active = pi.getActiveTools();
			assert.equal(active.includes("meta_tool_read_batch"), !disabled);
			assert((process.env.META_TOOL_PI_EXTRA_EXTENSIONS ?? "").split(":").includes(extension),
				"Main extension did not configure worker propagation");
			const text = (r: any) => r.content.filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n");
			const ranges = (size: number) => Array.from({ length: size }, (_, i) => ({ path: source, offset: 1 + i * 3, limit: 3 }));
			const sizes: number[] = [];
			if (disabled) {
				let blocked = false;
				try { blocked = (await pi.callTool("meta_tool_read_batch", { ranges: ranges(2) })).isError === true; }
				catch { blocked = true; }
				assert(blocked, "Explicit opt-out did not block execution");
			} else {
				for (const size of [2, 3, 4]) {
					const originals: string[] = [];
					for (const range of ranges(size)) {
						const r = await pi.callTool("read", range);
						assert.equal(r.isError, false);
						originals.push(text(r));
					}
					const r = await pi.callTool("meta_tool_read_batch", { ranges: ranges(size) });
					assert.equal(r.isError, false, text(r));
					for (const original of originals) assert(text(r).includes(original), "Batch lost primitive read output");
					sizes.push(size);
				}
				const bad = await pi.callTool("meta_tool_read_batch", { ranges: [
					{ path: source, offset: 1, limit: 301 }, { path: source, offset: 1, limit: 3 },
				] });
				assert.equal(bad.isError, true);
			}
			// A fresh workspace must not require a seeded user catalog or traces.
			console.log(JSON.stringify({ read_batch_probe: true, passed: true, model_calls: 0,
				cwd: ctx.cwd, disabled, batch_sizes_executed: sizes, worker_propagation: true }));
		} catch (error) {
			console.log(JSON.stringify({ read_batch_probe: true, passed: false, model_calls: 0, error: String(error) }));
			process.exitCode = 1;
		}
		ctx.shutdown();
		return { action: "handled" };
	});
}
