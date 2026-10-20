import { createHash } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";

const { SessionManager } = await import(process.env.GP_PI_MODULE);
const lines = createInterface({ input: process.stdin });
const input = JSON.parse((await lines[Symbol.asyncIterator]().next()).value);
lines.close();
const scratch = mkdtempSync(join(input.sessionDir, ".fork-checkpoint-"));
try {
  // The SDK extracts the root-to-entry path before forkFrom can see later entries.
  // This manager only reads the parent; createBranchedSession writes into scratch.
  const source = SessionManager.open(input.sourcePath, scratch);
  if (source.getSessionId() !== input.parentSessionId) throw new Error("fork parent identity mismatch");
  const entry = source.getEntry(input.checkpointId);
  let completed = entry?.type === "message" && entry.message.role === "assistant"
    && entry.message.stopReason === "stop"
    && !(Array.isArray(entry.message.content)
      && entry.message.content.some(item => item.type === "toolCall"));
  if (entry?.type === "message" && entry.message.role === "toolResult") {
    const path = source.getBranch(input.checkpointId);
    const assistantIndex = path.findLastIndex(item => item.type === "message" && item.message.role === "assistant");
    if (assistantIndex >= 0) {
      const content = path[assistantIndex].message.content;
      const calls = new Set((Array.isArray(content) ? content : [])
        .filter(item => item.type === "toolCall" && typeof item.id === "string").map(item => item.id));
      const results = new Set(path.slice(assistantIndex + 1)
        .filter(item => item.type === "message" && item.message.role === "toolResult")
        .map(item => item.message.toolCallId));
      completed = calls.size > 0 && [...calls].every(id => results.has(id));
    }
  }
  if (!completed) {
    throw new Error("fork checkpoint must be a completed assistant or tool-result boundary");
  }
  const snapshot = source.createBranchedSession(input.checkpointId);
  if (!snapshot) throw new Error("fork checkpoint could not be persisted");
  const context = source.buildSessionContext();
  const digest = createHash("sha256").update(JSON.stringify(context)).digest("hex");
  const branch = SessionManager.forkFrom(snapshot, input.targetCwd, input.sessionDir, { id: input.sessionId });
  if (JSON.stringify(branch.buildSessionContext()) !== JSON.stringify(context)) {
    throw new Error("fork context projection changed");
  }
  process.stdout.write(JSON.stringify({ id: input.id, result: {
    sessionId: branch.getSessionId(), sessionFile: branch.getSessionFile(),
    checkpointId: input.checkpointId, contextProjectionDigest: digest,
  } }) + "\n");
} catch (error) {
  process.stdout.write(JSON.stringify({ id: input.id, error: String(error) }) + "\n");
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
