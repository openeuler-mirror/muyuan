"""Message-only Child execution and exact ownership recovery for TT roles."""

import math
from typing import Any

from goal_plus.execution.thinkthread_rpc import MESSAGE_RECEIVE_LIMIT, registration_matches
from goal_plus.ports.clock import Clock
from goal_plus.ports.thinkthread import (
    AgentPosixBridgeError, ChildBinding, ThinkThreadClient, capability_ids,
)

# Retained Pi can briefly finish a wake before its automatic provider retry begins.
TURN_QUIESCENCE_SECONDS = 20.0


class ChildDriver:
    def __init__(self, client: ThinkThreadClient, clock: Clock) -> None:
        self.client = client
        self.clock = clock

    @staticmethod
    def binding(
        result: dict[str, Any], *, model: dict[str, Any] | None = None,
    ) -> ChildBinding:
        child_id = result.get("thinkthreadId")
        attachment = result.get("fs")
        branch_id = attachment.get("fsBranchId") if isinstance(attachment, dict) else None
        if not isinstance(child_id, str) or not child_id.startswith("tt-"):
            raise RuntimeError("thinkthread.spawn omitted thinkthreadId")
        if (
            not isinstance(attachment, dict) or attachment.get("kind") != "private"
            or not isinstance(branch_id, str) or not branch_id.startswith("fsbranch-")
        ):
            raise RuntimeError("thinkthread.spawn omitted a private branch")
        if capability_ids(result.get("capabilities")) != {"thinkthread.message"}:
            raise RuntimeError("ThinkThread Child grant is not Message-only")
        if model is not None and result.get("model") != model:
            raise RuntimeError("ThinkThread candidate model does not match its intent")
        message_id = result.get("initialMessageId")
        if message_id is not None and not isinstance(message_id, str):
            raise RuntimeError("thinkthread.spawn returned an invalid initialMessageId")
        return ChildBinding(child_id, branch_id, message_id)

    def spawn(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.client.invoke("thinkthread.spawn", params, timeout_seconds=120.0)

    def wake(self, child_id: str, message: str) -> str:
        result = self.client.invoke("message.send", {
            "recipientThinkthreadId": child_id, "text": message, "wake": True,
        })
        message_id = result.get("messageId")
        if not isinstance(message_id, str):
            raise TypeError("ThinkThread wake omitted messageId")
        return message_id

    def recovery_matches(
        self, *, before: set[str], bound: set[str], nonce: str,
        root_id: str | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        listed = self.client.invoke("thinkthread.list").get("children")
        if not isinstance(listed, list):
            raise AgentPosixBridgeError("thinkthread.list omitted children")
        matches = []
        for child in listed:
            if not isinstance(child, dict):
                continue
            child_id, attachment = child.get("thinkthreadId"), child.get("fs")
            if (
                not isinstance(child_id, str) or child_id in before or child_id in bound
                or (root_id is not None and child.get("parentThinkthreadId") != root_id)
                or capability_ids(child.get("capabilities")) != {"thinkthread.message"}
                or not isinstance(attachment, dict) or attachment.get("kind") != "private"
            ):
                continue
            page = self.client.invoke("message.receive", {
                "senderThinkthreadId": child_id, "limit": MESSAGE_RECEIVE_LIMIT,
            })
            messages = page.get("messages")
            if not isinstance(messages, list):
                raise AgentPosixBridgeError("recovery received an invalid Message page")
            if any(
                registration_matches(message, nonce)
                for message in messages if isinstance(message, dict)
            ):
                matches.append((child, page))
        return matches

    def observe(
        self, child_id: str, *, root_id: str, branch_id: str | None,
    ) -> dict[str, Any] | None:
        try:
            child = self.client.invoke("thinkthread.get", {"id": child_id})
        except AgentPosixBridgeError as exc:
            if exc.code != "ThinkThreadNotFound":
                raise
            return None
        if (
            child.get("thinkthreadId") != child_id
            or child.get("parentThinkthreadId") != root_id
            or not isinstance(child.get("fs"), dict)
            or child["fs"].get("kind") != "private"
            or child["fs"].get("fsBranchId") != branch_id
        ):
            raise PermissionError("ThinkThread Child ownership mismatch")
        return child

    def turn_terminal(self, child: dict[str, Any], active_message: object) -> str | None:
        agent_state = child.get("agentState")
        if agent_state not in {"ready", "failed"} or child.get("pendingWake") is True:
            return None
        wake = child.get("lastWakeOutcome")
        if isinstance(wake, dict) and wake.get("messageId") == active_message:
            outcome = wake.get("outcome")
            if outcome in {"completed", "interrupted", "failed", "cancelled"}:
                finished = wake.get("finishedAtUnixMs")
                if (
                    not isinstance(finished, (int, float)) or isinstance(finished, bool)
                    or not math.isfinite(finished)
                    or self.clock.now() * 1000 < float(finished) + TURN_QUIESCENCE_SECONDS * 1000
                ):
                    return None
                return str(outcome)
        if active_message is None and child.get("executionState") == "absent" and child.get("completion") is not None:
            return "completed" if agent_state == "ready" else "failed"
        return None

    def interrupt(self, child_id: str, timeout_seconds: float) -> dict[str, Any] | None:
        for signal in ("INT", "TERM"):
            try:
                self.client.invoke("thinkthread.signal", {"id": child_id, "signal": signal})
            except AgentPosixBridgeError as exc:
                if exc.code == "ThinkThreadNotFound":
                    return None
                if not exc.completion_unknown:
                    raise
            try:
                waited = self.client.invoke(
                    "thinkthread.wait",
                    {"id": child_id, "timeoutMs": max(0, int(timeout_seconds * 1000))},
                    timeout_seconds=timeout_seconds + 5.0,
                )
            except AgentPosixBridgeError as exc:
                if exc.code != "WaitTimeout":
                    raise
                continue
            child = waited.get("child")
            if isinstance(child, dict) and child.get("executionState") == "absent":
                return child
        return None

    def destroy(self, child_id: str) -> None:
        try:
            self.client.invoke("thinkthread.destroy", {"id": child_id})
        except AgentPosixBridgeError as exc:
            if not exc.completion_unknown:
                raise
            try:
                self.client.invoke("thinkthread.get", {"id": child_id})
            except AgentPosixBridgeError as observed:
                if observed.code != "ThinkThreadNotFound":
                    raise exc
            else:
                raise

    def reset_branch(self, child_id: str, *, root_id: str, branch_id: str, snapshot_id: str) -> None:
        child = self.observe(child_id, root_id=root_id, branch_id=branch_id)
        if child is None:
            raise RuntimeError("ThinkThread Child disappeared before branch reset")
        if child.get("executionState") != "absent":
            self.interrupt(child_id, 5.0)
            child = self.observe(child_id, root_id=root_id, branch_id=branch_id)
            if child is None or child.get("executionState") != "absent":
                raise RuntimeError("ThinkThread Child must stop before branch reset")
        branch = self.client.invoke("fs.branch.stat", {"branchId": branch_id})
        if branch.get("branchId") != branch_id or branch.get("thinkthreadId") != child_id:
            raise PermissionError("ThinkThread branch ownership mismatch")
        if branch.get("baseSnapshotId") == snapshot_id:
            return
        generation = branch.get("controlGeneration")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise RuntimeError("ThinkThread branch omitted controlGeneration")
        try:
            self.client.invoke("fs.branch.reset", {
                "branchId": branch_id, "toSnapshotId": snapshot_id, "ifGeneration": generation,
            }, timeout_seconds=60.0)
        except AgentPosixBridgeError as exc:
            if not exc.completion_unknown:
                raise
        observed = self.client.invoke("fs.branch.stat", {"branchId": branch_id})
        if observed.get("baseSnapshotId") != snapshot_id:
            raise RuntimeError("ThinkThread branch reset did not establish the exact snapshot")

    def release(self, child_id: str, *, root_id: str, branch_id: str, timeout: float) -> None:
        child = self.observe(child_id, root_id=root_id, branch_id=branch_id)
        if child is not None and child.get("executionState") != "absent":
            self.interrupt(child_id, timeout)
            child = self.observe(child_id, root_id=root_id, branch_id=branch_id)
            if child is not None and child.get("executionState") != "absent":
                raise RuntimeError("ThinkThread Child remained active during resource release")
        try:
            branch = self.client.invoke("fs.branch.stat", {"branchId": branch_id})
        except AgentPosixBridgeError as exc:
            if exc.code != "FsBranchNotFound":
                raise
            branch = None
        if branch is not None:
            if branch.get("branchId") != branch_id or branch.get("thinkthreadId") != child_id:
                raise PermissionError("ThinkThread branch ownership mismatch during resource release")
            if branch.get("state") != "removed":
                generation = branch.get("controlGeneration")
                if not isinstance(generation, int) or isinstance(generation, bool):
                    raise RuntimeError("ThinkThread branch omitted controlGeneration")
                try:
                    self.client.invoke("fs.branch.remove", {"branchId": branch_id, "ifGeneration": generation})
                except AgentPosixBridgeError as exc:
                    if not exc.completion_unknown:
                        raise
                try:
                    removed = self.client.invoke("fs.branch.stat", {"branchId": branch_id})
                except AgentPosixBridgeError as exc:
                    if exc.code != "FsBranchNotFound":
                        raise
                else:
                    if removed.get("state") != "removed":
                        raise RuntimeError("ThinkThread branch removal is not confirmed")
        if child is not None:
            self.destroy(child_id)
            if self.observe(child_id, root_id=root_id, branch_id=branch_id) is not None:
                raise RuntimeError("ThinkThread Child destruction is not confirmed")
