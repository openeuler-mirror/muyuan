"""evolve 步骤：让 LLM 提出结构化的组件 patch 提案。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from harness_optimizer.schema import HarnessSpec

from .commands import _structured_patch_mode
from .context import _component_files, _reference_materials
from .errors import LLMCall
from .patches import (
    _load_persistent_failures,
    _materialize_proposal,
    _parse_change_payload,
    _validate_patch_targets,
)
from .util import _json_value

def _propose(
    api: Mapping[str, str],
    call_llm: LLMCall,
    root: Path,
    spec: HarnessSpec,
    task_prompt: str,
    metrics: Mapping[str, Any],
    history: list[Mapping[str, Any]],
) -> dict[str, Any]:
    manifest = json.dumps(spec.to_dict(), ensure_ascii=False, indent=2, default=str)
    recent = json.dumps(history[-3:], ensure_ascii=False, indent=2, default=str)
    references = _reference_materials(
        spec, root,
        max_chars=int(os.environ.get("AHE_REFERENCE_MAX_CHARS", "24000") or 24000))
    failed_lines: list[str] = _load_persistent_failures()
    for h in history:
        if h.get("action") != "revert":
            continue
        prop = h.get("proposal") or {}
        patches = prop.get("patches") or []
        if not patches:
            continue
        m = h.get("metrics") or {}
        outcome_bits = [f"{k}={m.get(k)}" for k in
                        ("score", "kept_iterations", "iterations")
                        if m.get(k) is not None]
        vf = m.get("verifier_failures")
        if isinstance(vf, dict) and vf:
            outcome_bits.append("verifier_failures=" + json.dumps(vf, ensure_ascii=False))
        outcome = ", ".join(outcome_bits) or "reverted"
        for p in patches:
            old_txt = str(p.get("old", ""))[:180].replace("\n", " ")
            new_txt = str(p.get("new", ""))[:180].replace("\n", " ")
            failed_lines.append(
                f"- [round {h.get('round')}] {p.get('operation', 'edit')}: "
                f"old={old_txt!r} -> new={new_txt!r} | outcome: {outcome}")
    failed_section = ""
    if failed_lines:
        failed_section = (
            "FAILED PATCHES 鈥?these exact edits were already tried and REVERTED. "
            "Do NOT re-propose them verbatim or with cosmetic rewording; "
            "propose a materially different approach:\n"
            + "\n".join(failed_lines[:12]) + "\n\n"
        )
    if _structured_patch_mode(spec):
        mutation_contract = (
            "You MUST propose at least one concrete, reversible patch (mechanism-level patches up to 50KB are encouraged); an empty "
            "changes/patches array is NOT a valid answer. If uncertain, propose the smallest "
            "reversible experiment that could improve the metrics.\n"
            "For existing files, use the real local-patch protocol. Do NOT return a complete file.\n"
            "Each change must use replace_text, replace_range, insert_after, insert_before, or delete_text.\n"
            "Existing-file patches must include an exact unique old snippet or anchor, a new string (except delete),\n"
            "and the expected_file_sha256 shown in the file listing. Each old/new value may be up to 50KB 鈥?large mechanism-level patches are ENCOURAGED.\n"
            "The patch is applied only when the old text occurs exactly once. New files may use add with small content.\n"
            "When component_id names a directory, path must be the actual file under that component (for example assets/pi/extensions/main.ts), not the directory itself.\n"
            "Do not use modify or replace with full-file content for an existing file."
        )
    else:
        mutation_contract = (
            "For every item in changes, content is the complete replacement contents of the declared file, including\n"
            "unchanged lines. Never put a path, diff, patch instruction, or prose in content. If you cannot produce\n"
            "a complete file, return an empty changes array rather than a description."
        )
    prompt = f"""You are a component-level harness optimizer following the AHE paper
(arXiv:2604.25850, Agentic Harness Engineering). The paper's NexAU substrate
exposes SEVEN editable component types, and the manifest below declares which
of them this harness carries:

  1. system prompt        2. tool description      3. tool implementation
  4. middleware           5. skill                 6. sub-agent configuration
  7. long-term memory (ledger/handoff)

The base model and evaluator are immutable. Propose a reversible edit — prefer complete SoL-Pi mechanism implementations over incremental bug fixes
to the declared harness components. The paper's ablation localizes most of the
gain to tool implementation, middleware, and long-term memory (system-prompt-only
edits regress), so prefer those classes when the evidence points there; prompt
text is only one possible component.

Web research: before finalizing a patch you are encouraged to search the web for
reference material — the AHE paper itself, the NexAU substrate, public
coding-agent harnesses (Codex, OpenCode, Terminus), skill libraries, middleware
patterns, and related papers. Adapt the ideas you find into patches for the
current components; every patch anchor must still come from the current files
listed below, never from web content.

Production-universality hard rules for every patch:
- GENERIC: the improvement must hold for ANY task of this harness's kind,
  not only the current eval task. Patches must be production-ready and
  directly mergeable into the upstream harness for real engineering use.
- MECHANISM-LEVEL: every patch needs a causal argument for why it improves
  the metrics. Renaming, single-constant tweaks, or cosmetic micro-edits
  are low-value and must not constitute a whole-round proposal.
- NO GAMING: task-specific special-casing, benchmark hacks, or evaluator
  exploits are rejected even if they raise the current score; improvements
  must generalize to the whole task class.
- BALANCE 效率与性能 (efficiency and performance together): while improving
  the primary metric, also reduce token/time overhead; never trade task
  performance for token savings, and never chase the metric while ignoring
  cost.

Task:
{task_prompt}

Manifest:
{manifest}

Current metrics:
{json.dumps(_json_value(metrics), ensure_ascii=False)}

Recent evidence:
{recent}

Debugger contract:
When a debugger result contains `diagnosis`, it uses the stable
`ahe.diagnosis` schema (version 1). Prefer its root_cause, failure_modes,
general_mechanism, recommended_focus, and evidence_ids over unstructured
debugger text. Treat evidence_ids as references, not proof by themselves.

Reference strategies (read-only background; do not edit these files or copy
their code blindly into the candidate; adapt only ideas supported by evidence):
{references}

Current component files:
{_component_files(root, spec, max_chars=int(os.environ.get("AHE_COMPONENT_FILES_MAX_CHARS", "150000") or 150000))}

Every old/anchor string must be copied exactly from the Current component files above. Do not use function names or snippets from the SoL-Pi references, prior-run summaries, or memory when they are absent from the current files. Before returning JSON, verify that each patch anchor occurs in its stated file or component directory.
{failed_section}
{mutation_contract}

Return JSON only:
{{
  "reason": "root cause and targeted fix",
  "evidence_ids": ["..."],
  "predicted": {{"accuracy": "...", "tokens": "...", "time_s": "..."}},
  "risks": ["..."],
  "changes": [
    {{"component_id": "...", "operation": "replace_text", "old": "...", "new": "...", "expected_file_sha256": "...", "reason": "...", "evidence_ids": ["..."]}}
  ]
}}
"""
    evolve_effort = os.environ.get("EVOLVE_REASONING_EFFORT", "max")
    _evolve_messages = [
        {"role": "system", "content": "Return a valid JSON change manifest only."},
        {"role": "user", "content": prompt},
    ]
    text = None
    try:
        text, _, _ = call_llm(
            api["url"], api["key"], api["evolve_model"], _evolve_messages,
            max_tokens=32768, reasoning_effort=evolve_effort)
    except Exception as _exc:
        if evolve_effort == "max":
            print(f"[evolve] max effort failed ({_exc}), retrying with high", flush=True)
            try:
                text, _, _ = call_llm(
                    api["url"], api["key"], api["evolve_model"], _evolve_messages,
                    max_tokens=32768, reasoning_effort="high")
            except Exception:
                raise
        else:
            raise
    try:
        proposal = _validate_patch_targets(_materialize_proposal(_parse_change_payload(text, spec), root), root)
    except ComponentModeError as _parse_exc:
        # 模型可能返回包在 content 里的等价 patch，或使用过时的操作名。
        # 在本轮被判定失败前，给一次明确的合同修复机会。
        print(f"[evolve] invalid change manifest ({_parse_exc}), retrying with explicit patch contract", flush=True)
        retry_prompt = prompt + (
            f"\n\nThe previous JSON was rejected by the local patch parser: {_parse_exc}. "
            "Return one concrete replace_text/insert_after/delete_text patch with a real file path, "
            "exact old/anchor text, and a string new value. Do not wrap the patch in content. Return JSON only."
        )
        text, _, _ = call_llm(
            api["url"], api["key"], api["evolve_model"],
            [{"role": "system",
              "content": "Return a valid JSON change manifest only. You MUST include one local patch."},
             {"role": "user", "content": retry_prompt}],
            max_tokens=32768,
            reasoning_effort=evolve_effort,
        )
        proposal = _validate_patch_targets(_materialize_proposal(_parse_change_payload(text, spec), root), root)
    if not proposal.get("changes") and not proposal.get("patches"):
        # 空提案不是合法的优化动作：显式要求一个具体 patch 后重试一次。
        retry_prompt = prompt + (
            "\n\nYour previous response contained NO patches, which is not acceptable. "
            "Propose exactly one reversible patch (mechanism-level patches up to 50KB are encouraged) now. Return JSON only."
        )
        text, _, _ = call_llm(
            api["url"], api["key"], api["evolve_model"],
            [{"role": "system",
              "content": "Return a valid JSON change manifest only. You MUST include at least one patch."},
             {"role": "user", "content": retry_prompt}],
            max_tokens=32768,
            reasoning_effort=evolve_effort,
        )
        proposal = _validate_patch_targets(_materialize_proposal(_parse_change_payload(text, spec), root), root)
    return proposal

