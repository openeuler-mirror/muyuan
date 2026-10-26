import { appendFileSync, readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function mainInstructionsObserver(pi: ExtensionAPI) {
  pi.on("before_agent_start", (event) => {
    const overview = readFileSync(process.env.GP_ST_OVERVIEW_FILE!, "utf8").trim();
    appendFileSync(process.env.GP_ST_INSTRUCTIONS_LOG!, JSON.stringify({
      overview_present: event.systemPrompt.includes(overview),
      tools: pi.getAllTools().map((tool) => tool.name),
    }) + "\n");
  });
}
