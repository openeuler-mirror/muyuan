// Called only by the bounded session archive helper, never registered as a tool.
import { pathToFileURL } from "node:url";

try {
    const sdkPath = process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY;
    if (!sdkPath) throw new Error("missing_sdk");
    const sdk = await import(sdkPath.startsWith("file:") ? sdkPath : pathToFileURL(sdkPath).href);
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const client = sdk.AgentPosixClient.fromEnv();
    const self = await client.invoke("self", {});
    if (typeof self.parentThinkthreadId !== "string" || self.parentThinkthreadId !== input.parentId
        || self.parentThinkthreadId !== process.env.THINKTHREAD_PARENT_ID) throw new Error("parent_mismatch");
    for (const message of input.messages) {
        await client.invoke("message.send", {
            recipientThinkthreadId: self.parentThinkthreadId, wake: false, text: JSON.stringify(message),
        });
    }
} catch {
    process.exitCode = 1;
}
