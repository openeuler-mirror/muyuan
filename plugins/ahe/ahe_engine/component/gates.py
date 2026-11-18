"""keep/revert 接受门禁（可用环境变量切换战役门控）。"""
from __future__ import annotations

import os
from typing import Any, Mapping

from harness_optimizer.schema import HarnessSpec

from .metrics import _objective, _primary_metric
from .util import _as_float

def _acceptable(candidate: Mapping[str, Any], best: Mapping[str, Any],
                candidate_holdout: Mapping[str, Any] | None,
                best_holdout: Mapping[str, Any] | None,
                spec: HarnessSpec) -> bool:
    if not candidate.get("runner_ok"):
        return False
    # AHE_KEEP_MULTI_OBJECTIVE=1：(迭代数升、token 降、accuracy 升)
    # 任一改善即保留，前提 accuracy 不低于下限。使 evolve 可以改进
    # 效率或质量，而不被单一指标的棘轮门禁卡死。
    if os.environ.get("AHE_KEEP_MULTI_OBJECTIVE", "") == "1":
        cand_impr = _as_float(candidate.get("accuracy"))
        best_impr = _as_float(best.get("accuracy"))
        cand_iters = _as_float(candidate.get("f1"))
        best_iters = _as_float(best.get("f1"))
        cand_tokens = _as_float(candidate.get("tokens"))
        best_tokens = _as_float(best.get("tokens"))
        if None in (cand_impr, best_impr, cand_iters, best_iters,
                    cand_tokens, best_tokens):
            return False
        floor = _as_float(os.environ.get("AHE_MULTI_OBJ_IMPR_FLOOR", "20"))
        impr_ok = (cand_impr >= floor) if floor is not None else (cand_impr >= best_impr)
        iters_up = cand_iters > best_iters
        tokens_down = cand_tokens < best_tokens
        accuracy_up = cand_impr > best_impr
        return impr_ok and (iters_up or tokens_down or accuracy_up)
    # AHE_KEEP_ACCURACY_ONLY=1：单一目标晋级。kernel 优化率
    # （accuracy）严格超过当前 best 才保留；runner_ok 已在上游
    # 强制官方 verifier 门禁。
    if os.environ.get("AHE_KEEP_ACCURACY_ONLY", "") == "1":
        cand_impr = _as_float(candidate.get("accuracy"))
        best_impr = _as_float(best.get("accuracy"))
        if cand_impr is None or best_impr is None:
            return False
        return cand_impr > best_impr
    # AHE_KEEP_TRI_METRIC=1：三项效率信号必须同时改善——优化率严格升、
    # 完成迭代数严格升、总 token 严格降。
    # runner_ok 已在上游强制官方 verifier 门禁。
    if os.environ.get("AHE_KEEP_TRI_METRIC", "") == "1":
        cand_iters = _as_float(candidate.get("f1"))
        best_iters = _as_float(best.get("f1"))
        cand_tokens = _as_float(candidate.get("tokens"))
        best_tokens = _as_float(best.get("tokens"))
        cand_impr = _as_float(candidate.get("accuracy"))
        best_impr = _as_float(best.get("accuracy"))
        if None in (cand_iters, best_iters, cand_tokens, best_tokens,
                    cand_impr, best_impr):
            return False
        return (cand_impr > best_impr
                and cand_iters > best_iters
                and cand_tokens < best_tokens)
    # AHE_KEEP_EFFICIENCY_STRICT=1：效率优先晋级。harness 必须让搜索
    # 严格更高效——完成搜索迭代数更多且总 token（Main + workers，由
    # evaluator 的 "tokens" 字段上报）更少——同时 kernel 优化质量
    # （accuracy = 改进百分比）不得回退。runner_ok 已在上游强制
    # 官方 verifier 门禁。
    if os.environ.get("AHE_KEEP_EFFICIENCY_STRICT", "") == "1":
        cand_iters = _as_float(candidate.get("f1"))
        best_iters = _as_float(best.get("f1"))
        cand_tokens = _as_float(candidate.get("tokens"))
        best_tokens = _as_float(best.get("tokens"))
        cand_impr = _as_float(candidate.get("accuracy"))
        best_impr = _as_float(best.get("accuracy"))
        if None in (cand_iters, best_iters, cand_tokens, best_tokens,
                    cand_impr, best_impr):
            return False
        # kernel 质量是绝对下限（默认 5%），不是棘轮：
        # 候选过线后，后续轮次比拼效率。
        floor = _as_float(os.environ.get("AHE_EFFICIENCY_IMPR_FLOOR", "5"))
        impr_ok = (cand_impr >= floor) if floor is not None else (cand_impr >= best_impr)
        # 任一轴严格变好即视为效率改善。
        return (impr_ok
                and (cand_iters > best_iters or cand_tokens < best_tokens))
    # AHE_KEEP_PRODUCT_SCORE=1：主指标（乘积分）严格增加才晋级。
    # evaluator 上报合成主指标（如乘积分）时，
    # 这恰好实现"乘积必须严格增加"的晋级规则；runner_ok 已在上游
    # 强制官方 verifier/精度门禁。
    if os.environ.get("AHE_KEEP_PRODUCT_SCORE", "") == "1":
        _, direction = _objective(spec)
        _, candidate_value = _primary_metric(candidate, spec)
        _, best_value = _primary_metric(best, spec)
        if candidate_value is None or best_value is None:
            return False
        return (candidate_value > best_value if direction == "maximize"
                else candidate_value < best_value)
    # 稀疏 kernel 战役可启用严格双信号晋级门控。
    # evaluator 将测量值映射到规范质量字段：
    #   f1       = 完成搜索迭代数
    #   accuracy = kernel 延迟改进百分比
    # 两者都必须严格超过当前保留候选。该门控先于通用分数/效率启发式
    # 执行，使 wall time 或 token 噪声无法让任一任务信号回退的候选晋级。
    if os.environ.get("AHE_KEEP_SPARSE_METRICS", "") == "1":
        candidate_iterations = _as_float(candidate.get("f1"))
        candidate_improvement = _as_float(candidate.get("accuracy"))
        best_iterations = _as_float(best.get("f1")) or 0.0
        best_improvement = _as_float(best.get("accuracy")) or 0.0
        if candidate_iterations is None or candidate_improvement is None:
            return False
        if candidate_iterations <= best_iterations or candidate_improvement <= best_improvement:
            return False
        if candidate_holdout is not None:
            if not candidate_holdout.get("runner_ok"):
                return False
            hold_iterations = _as_float(candidate_holdout.get("f1"))
            hold_improvement = _as_float(candidate_holdout.get("accuracy"))
            hold_best_iterations = _as_float((best_holdout or {}).get("f1")) or 0.0
            hold_best_improvement = _as_float((best_holdout or {}).get("accuracy")) or 0.0
            if (hold_iterations is None or hold_improvement is None
                    or hold_iterations <= hold_best_iterations
                    or hold_improvement <= hold_best_improvement):
                return False
        return True
    # AHE_EFFICIENCY_KEEP=1：主指标超过 AHE_EFFICIENCY_SCORE_FLOOR，
    # 且候选在至少一个效率参考（token 或 wall time，经环境变量提供）上
    # 更优时才保留。
    if os.environ.get("AHE_EFFICIENCY_KEEP", "") == "1":
        _eff_floor = _as_float(os.environ.get("AHE_EFFICIENCY_SCORE_FLOOR", "4"))
        _, _eff_score = _primary_metric(candidate, spec)
        if _eff_floor is None or _eff_score is None or _eff_score <= _eff_floor:
            return False
        _ref_tokens = _as_float(os.environ.get("AHE_EFFICIENCY_REF_TOKENS", ""))
        _ref_time = _as_float(os.environ.get("AHE_EFFICIENCY_REF_TIME_S", ""))
        _cand_tokens = _as_float(candidate.get("tokens"))
        _cand_time = _as_float(candidate.get("time_s"))
        # 崩溃/快速失败的运行完全不产出 token 用量；token 计数缺失意味着
        # Pi 进程从未正常运行，因此候选绝不能仅凭 wall time 获胜。
        if _cand_tokens is None:
            return False
        _tokens_ok = (_ref_tokens is not None and _cand_tokens is not None
                      and _cand_tokens < _ref_tokens)
        _time_ok = (_ref_time is not None and _cand_time is not None
                    and _cand_time < _ref_time)
        return bool(_tokens_ok or _time_ok)
    _, direction = _objective(spec)
    _, candidate_value = _primary_metric(candidate, spec)
    _, best_value = _primary_metric(best, spec)
    if candidate_value is None or best_value is None:
        return False
    keep_floor = _as_float(os.environ.get("AHE_KEEP_SCORE_FLOOR", ""))
    floor_only = os.environ.get("AHE_KEEP_FLOOR_ONLY", "") == "1"
    if floor_only and keep_floor is not None:
        if direction == "maximize" and candidate_value < keep_floor:
            return False
        if direction != "maximize" and candidate_value > keep_floor:
            return False
    # AHE_LENIENT_KEEP=1：主指标在噪声范围内回退时，搜索产出更多
    # 计时 keep（已验证改进）的候选也算改善。使用 kept_iterations 而非
    # 原始迭代数，忙而坏搜索无法蒙混过关。
    if os.environ.get("AHE_LENIENT_KEEP", "") == "1":
        cand_keeps = _as_float(candidate.get("kept_iterations"))
        best_keeps = _as_float(best.get("kept_iterations"))
        cand_keeps = cand_keeps if cand_keeps is not None else 0.0
        best_keeps = best_keeps if best_keeps is not None else 0.0
        if cand_keeps > best_keeps:
            noise = float(os.environ.get("AHE_LENIENT_KEEP_NOISE", "1.0") or 1.0)
            if direction == "maximize" and candidate_value >= best_value - noise:
                return True
            if direction != "maximize" and candidate_value <= best_value + noise:
                return True
    score_ok = candidate_value >= best_value if direction == "maximize" else candidate_value <= best_value
    if not score_ok:
        return False
    # 即使 adapter 主指标不变，独立上报的质量指标也不得回退。
    # F1 与 accuracy/score 保持区分，仅在 evaluator 输出时才参与判定。
    for quality_key in ("f1", "accuracy"):
        before = _as_float(best.get(quality_key))
        after = _as_float(candidate.get(quality_key))
        if before is not None and after is not None and after < before:
            return False
    if candidate_holdout is not None:
        if not candidate_holdout.get("runner_ok"):
            return False
        if best_holdout is not None:
            _, hold_value = _primary_metric(candidate_holdout, spec)
            _, best_hold_value = _primary_metric(best_holdout, spec)
            if hold_value is None or best_hold_value is None:
                return False
            hold_ok = hold_value >= best_hold_value if direction == "maximize" else hold_value <= best_hold_value
            if not hold_ok:
                return False
            for quality_key in ("f1", "accuracy"):
                before = _as_float(best_holdout.get(quality_key))
                after = _as_float(candidate_holdout.get(quality_key))
                if before is not None and after is not None and after < before:
                    return False
    if floor_only and keep_floor is not None:
        return True
    for quality_key in ("f1", "accuracy"):
        before = _as_float(best.get(quality_key))
        after = _as_float(candidate.get(quality_key))
        if before is not None and after is not None and after > before:
            return True
    score_improved = candidate_value > best_value if direction == "maximize" else candidate_value < best_value
    if score_improved:
        return True
    lower_metrics = ("tokens", "time_s", "tool_calls", "turns", "retries", "cost")
    for key in lower_metrics:
        before = _as_float(best.get(key))
        after = _as_float(candidate.get(key))
        if before is not None and after is not None and after < before:
            return True
    return False

