# Policy Algorithm Sources

The v1 functions in this directory are independent, clean-room reimplementations
of small published formulas. No upstream node, selector, tree, or persistence
code is copied.

The stable local surface is deliberately limited to:

- `backpropagated_value_statistics_v1`;
- `uct_exploration_v1`;
- `swe_search_depth_terms_v1`;
- `trailing_low_reward_streak_v1`.

## SWE-Search

- Paper: [SWE-Search: Enhancing Software Agents with Monte Carlo Tree Search and
  Iterative Refinement](https://arxiv.org/abs/2410.20285), published at ICLR 2025.
- Reproduction repository:
  [a-antoniades/swe-search](https://github.com/a-antoniades/swe-search) at
  `f61d0cac4e5f33b61ef37d5d59fd2916563f04c0`, MIT license.
- Maintained reference:
  [aorwall/moatless-tree-search](https://github.com/aorwall/moatless-tree-search)
  at `bc66ea64e3fe96cf3c90f197b4bab9aaa15e1eb9`, Apache-2.0 license.
- Classification: `reimplement`.

Both reference licenses permit this independently authored implementation and
are compatible with Goal Plus distribution. This directory contains no copied
upstream implementation code or transitive runtime dependency.

`uct_exploration_v1` and `swe_search_depth_terms_v1` map directly to the paper:

```text
V(s,a) + C * sqrt(ln(N(s)) / N(s,a))
       + alpha * exp(-beta * (d - 1)) - gamma * sqrt(d)
```

Goal Plus uses allocation utility in `[0,1]`, while the reference values use
`[0,100]`. The additive defaults are scaled by `1/100`: `C=0.01`,
`alpha=2.0`, and `gamma=0.008`; `beta=0.8` is dimensionless and unchanged.
`progress_reward` instead has signed `[-1,1]` semantics and is used only by
`trailing_low_reward_streak_v1`.

The pinned reproduction code calculates exploration but omits it from the
returned score. Goal Plus includes exploration in the final UCT score, as
required by the paper. High-value leaf, bad-child, repeated-action, diversity,
and discriminator heuristics are excluded. Goal Plus also owns the
parallel-lane retirement adaptation; it is not part of these algorithms.

Backpropagation and the trailing reward streak are small local compositions of
the documented tree statistics and abandonment rule. They do not copy an
upstream tree container or mutable node behavior.

Golden-vector tests in `tests/adaptive_search/test_policy_algorithms.py` are
derived from the published formulas and hand-calculated tree statistics.
