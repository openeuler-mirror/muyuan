"""Pure selection of a completed Pi checkpoint on active Pi ancestry.

Fork preparation markers can freeze the selected run boundary across a
later explicit resume turn.
"""

from typing import Any


def active_ancestry(entries: object, leaf_id: object) -> list[dict[str, Any]]:
    """Return the active leaf ancestry in leaf-to-root order."""
    if not isinstance(entries, list) or not isinstance(leaf_id, str) or not leaf_id:
        raise RuntimeError("ForkCheckpointUnavailable: Pi active leaf is missing")
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        if entry["id"] in by_id:
            raise RuntimeError("ForkCheckpointUnavailable: Pi entry ids are ambiguous")
        by_id[entry["id"]] = entry
    if leaf_id not in by_id:
        raise RuntimeError("ForkCheckpointUnavailable: Pi active leaf is unknown")
    path: list[dict[str, Any]] = []
    current: str | None = leaf_id
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise RuntimeError("ForkCheckpointUnavailable: Pi entry ancestry contains a cycle")
        seen.add(current)
        entry = by_id.get(current)
        if entry is None:
            raise RuntimeError("ForkCheckpointUnavailable: Pi entry ancestry is broken")
        path.append(entry)
        parent = entry.get("parentId")
        if parent is None:
            break
        if not isinstance(parent, str) or not parent:
            raise RuntimeError("ForkCheckpointUnavailable: Pi entry parent id is invalid")
        current = parent
    return path


def _completed_checkpoint_from_path(
    path: list[dict[str, Any]], *, include_tool_groups: bool = True,
) -> str:
    """Return the nearest complete boundary on one leaf-to-root path."""
    if not path:
        raise RuntimeError("ForkCheckpointUnavailable: Pi active leaf is missing")
    for leaf_index, leaf_entry in enumerate(path):
        leaf_message = leaf_entry.get("message")
        if leaf_entry.get("type") != "message" or not isinstance(leaf_message, dict):
            continue
        content = leaf_message.get("content")
        if (leaf_message.get("role") == "assistant" and leaf_message.get("stopReason") == "stop"
                and not (isinstance(content, list) and any(
                    isinstance(item, dict) and item.get("type") == "toolCall" for item in content))):
            return str(leaf_entry["id"])
        if not include_tool_groups or leaf_message.get("role") != "toolResult":
            continue
        assistant_index: int | None = None
        for index in range(leaf_index + 1, len(path)):
            message = path[index].get("message")
            if (path[index].get("type") == "message" and isinstance(message, dict)
                    and message.get("role") == "assistant"):
                assistant_index = index
                break
        if assistant_index is not None:
            assistant = path[assistant_index].get("message")
            content = assistant.get("content") if isinstance(assistant, dict) else None
            call_ids = {item.get("id") for item in content or []
                        if isinstance(item, dict) and item.get("type") == "toolCall"
                        and isinstance(item.get("id"), str)}
            result_ids = {(item.get("message") or {}).get("toolCallId")
                          for item in path[leaf_index:assistant_index]
                          if item.get("type") == "message"
                          and isinstance(item.get("message"), dict)
                          and item["message"].get("role") == "toolResult"}
            if call_ids and call_ids <= result_ids:
                return str(leaf_entry["id"])
    raise RuntimeError("ForkCheckpointUnavailable: no completed Pi assistant entry on active leaf")


def _checkpoint_path(
    entries: object,
    leaf_id: object,
    run_id: object,
    *,
    require_preparation_marker: bool = False,
) -> list[dict[str, Any]]:
    """Validate current ancestry before freezing its run-scoped preparation boundary."""
    if require_preparation_marker and not (isinstance(run_id, str) and run_id):
        raise ValueError("A preparation checkpoint marker requires a Search run id")
    path = active_ancestry(entries, leaf_id)
    if isinstance(run_id, str) and run_id:
        for index, entry in enumerate(path):
            if (entry.get("type") == "custom"
                    and entry.get("customType") == "goal-plus-fork-checkpoint"
                    and isinstance(entry.get("data"), dict)
                    and entry["data"].get("run_id") == run_id):
                return path[index:]
        if require_preparation_marker:
            raise RuntimeError(
                "ForkCheckpointUnavailable: Pi active ancestry has no "
                "checkpoint marker for this Search run"
            )
    return path


def select_completed_checkpoint(
    entries: object, leaf_id: object, *, run_id: str | None = None,
    require_preparation_marker: bool = False,
) -> str:
    return _completed_checkpoint_from_path(_checkpoint_path(
        entries, leaf_id, run_id, require_preparation_marker=require_preparation_marker,
    ))


def completed_checkpoint_prefix(
    entries: object,
    leaf_id: object,
    *,
    run_id: str | None = None,
    require_preparation_marker: bool = False,
    include_tool_groups: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    """Return checkpoint ID and its active ancestry prefix in root order.

    Direct Pi can branch at a complete tool group. Providers that seal only a
    completed native turn must exclude those intermediate boundaries.
    """
    path = _checkpoint_path(
        entries, leaf_id, run_id, require_preparation_marker=require_preparation_marker,
    )
    checkpoint_id = _completed_checkpoint_from_path(path, include_tool_groups=include_tool_groups)
    index = next(index for index, entry in enumerate(path) if entry.get("id") == checkpoint_id)
    return checkpoint_id, list(reversed(path[index:]))
