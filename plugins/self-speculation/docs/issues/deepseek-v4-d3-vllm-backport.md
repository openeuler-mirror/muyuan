# DeepSeek V4 DSML drafts are not injected by the vLLM backport proposer

## Summary

The `deepseek_v4_dsml` Profile is validated for protocol selection, DSML
parsing and formatting, target-tokenizer round trips, native model tool-call
output, Pi configuration transport, and candidate registration. Full D3 target
verification is not yet supported on the tested DeepSeek V4 vLLM backport:
registered candidates never become proposed draft tokens.

This issue tracks the engine-integration compatibility gap. It is not evidence
of a defect in Profile resolution or DSML serialization.

## Current support level

Verified:

- `deepseek-v4-flash` with `actor_profile=auto` resolves to
  `deepseek_v4_dsml`.
- The full-width `U+FF5C` DSML boundary, probe prefix, and closing marker round
  trip through the served tokenizer.
- Native tool calling works; the observed baseline produced four exact calls
  out of five. The remaining call was valid DSML but omitted one model-selected
  argument.
- Correct and incorrect candidates register successfully through the TP8
  service.
- Pi transports the explicit Profile through the sidecar path and preserves the
  authoritative Actor tool call.

Not verified on the affected engine build:

- Non-zero D3 `proposed_tokens`.
- Correct-draft acceptance and incorrect-draft rejection.
- Fork-scoped `cache_policy=required` verification. Global prefix-cache metrics
  show reuse, but this build does not expose per-request cached-token counts.

## Affected environment

- Model: `DeepSeek-V4-Flash-0731`, served as `deepseek-v4-flash`
- vLLM backport revision: `23426dc78`
- Deployment: tensor parallel size 8 with the `custom_class` speculative method
- self-speculation: `82c0146` during the diagnostic run
- Pi: `f662706`

The same proposer integration produced real accepted and rejected draft tokens
with Qwen3.8 on the standard vLLM build, so the failure is specific to the
DeepSeek backport integration path tested here.

## Observed behavior

The proposer hook is installed and invoked. Request-ID routing, batch alignment,
and candidate registration are correct. Store diagnostics recorded 213 boundary
misses and zero proposed tokens.

The available diagnostic patch did not capture the active token slice correctly:
it sampled the end of the preallocated token row rather than
`row[:sequence_length]`, and its three-token observation covered only the first
three global offers. The evidence therefore does not determine whether the DSML
boundary is absent from the active sequence, excluded by `prompt_token_count`,
or represented by different runtime token IDs.

## Expected behavior

After a registered request reaches the DSML tool-call boundary, the custom
proposer should offer the matching candidate suffix. A correct suffix should
produce non-zero accepted draft tokens; a divergent suffix should produce
rejected draft tokens without changing the authoritative Actor output.

## Acceptance criteria

- Capture `sequence_length`, `prompt_token_count`, the active token slice, and
  boundary positions before calling `store.offer`.
- Identify whether the contract mismatch is in the vLLM token buffer, the
  self-speculation prompt slice, or token-ID representation.
- Add a bounded adapter fix without changing Qwen behavior.
- Demonstrate non-zero proposed and accepted tokens for a correct DSML draft.
- Demonstrate rejected tokens for an incorrect DSML draft while the Actor output
  remains unchanged.
- Verify fork-scoped cache reuse when the engine exposes per-request cache
  counters; otherwise report that capability as unavailable.

## Evidence

The retained server artifacts are under:

```text
/home/deploy/spork_test/deepseek-v4-spork-smoke
/home/deploy/spork_test/deepseek-v4-spork-diagnosis
```

The temporary diagnostic patch must not be merged as a fix.
