# Action Resolver 2.0

## Milestones

1. **Baseline:** `7e825008` passed all 51 original offline tests and the panel syntax check. The benchmark reproduced dropped explicit targets and two unauthorized-by-request offers before resolver changes.
2. **Resolution:** Named list expressions are matched independently (exact, joined, bounded edit distance, German phonetics). Diagnostics retain normalized-text offsets, per-expression candidates, techniques, scope, and rejections. Unknown targets and conflicting interpretations never get an executable subset. Exact names and aliases constrain fuzzy recovery; device names, area inheritance, and floor aliases come from HA registries. Exclusions need a positively resolved target, and overlapping explicit group operations require clarification.
3. **Planning and authorization:** Each clause resolves to typed, local action–target bindings. Live capabilities and configured policy are separate; the shared parameter range or enum must work for every target. Simple lights keep their boolean confirmation; unchanged single, parameterless actions keep the legacy tool. Parameterized and compound plans use typed, step-bound tools. The provider cannot supply new targets or change values. All calls and all batches are validated before the first service call, then state, capabilities, permissions, and group membership are rechecked at execution. Independent and dependent steps run in spoken order, not model order. Native HA services receive the original context; dry-run uses the same checks without calling them.
4. **Context and delivery:** Follow-up memory is in-process, bounded to 128 conversations / 256 referents each, and expires after five minutes. It is keyed by Assist conversation, HA user, device, and satellite, never by transcript or timing. Explicit targets take priority; missing or changed entities invalidate references. Existing action overrides, UI, reasoning, context budget, and overflow fallback are unchanged. Expanded diagnostics are redacted and are never read as an authority source.

### Fail-closed behavior

- An incomplete explicit list, incompatible parameterized group, unsupported value, ambiguous reference, or incomplete model response never starts an action. Simple bulk on/off can skip policy-disabled targets and reports those skips; already-matching states are no-ops. Automatic bulk never expands named groups.
- The model may approve at most eight steps in one response. An approved plan cannot contain more than 2,048 leaf operations, and each policy batch has at most 100 top-level targets. Plans that exceed a limit are rejected rather than truncated.
- Home Assistant service calls are not transactional. A failure after a prior service returns stops the remaining work and is reported as partial; there is no rollback. A successful service return does not prove physical device state. Existing explicit-admin/userless handling of opaque actions is unchanged; arbitrary downstream effects are not inspectable.
- Relative requests without a configured step, conditions, scheduling, complex cross-room target lists, and vague corrections such as “the other one” are not guessed. Satellite IDs isolate context; implicit satellite-area scope is not inferred. HA native target matching was considered: this resolver uses registry metadata without invoking intent handlers or adding a second language matcher. The checked API shapes and feature flags are from HA Core `2026.11.2`; there was no running HA instance for integration verification.

## Reproduce the benchmark

```sh
python3 tests/benchmark_resolver.py --ref 7e825008
python3 tests/benchmark_resolver.py
python3 -m unittest discover -s tests -q
node --check custom_components/auralis/panel.js
```

`--ref` extracts only the integration from the named Git revision into a temporary directory and uses the same current fixtures. No checkout or live device is changed. The offline benchmark uses 14 fixed German/English requests (30 repetitions per revision for latency), one synthetic catalog, and no LLM. A separate scripted approval probe measures false or incomplete **dry-run** plans, not actual model behavior or physical execution. Timing, bytes and estimated input tokens are machine- and fixture-specific; the estimate includes budget framing and the output reserve.

| Offline metric | 1.6.0 baseline | Resolver 2.0 |
| --- | ---: | ---: |
| Entity precision / recall | 81.8% / 64.3% | 100% / 100% |
| Entity–action precision / recall | 81.8% / 64.3% | 100% / 100% |
| Exact target set | 9 / 14 | 14 / 14 |
| Locally bound correct parameters | 0 / 4 | 4 / 4 |
| Appropriate clarifications | 2 / 4 | 4 / 4 |
| Unsafe offers | 2 / 14 | 0 / 14 |
| False-positive / incomplete dry-runs under scripted approval | 1 / 14; 1 / 14 | 0 / 14; 0 / 14 |
| Resolver-only p50 / p95 | 0.29 / 1.02 ms | 0.48 / 1.06 ms |
| Offline first request p50 / p95 | 1.33 / 2.03 ms | 1.37 / 1.88 ms |
| Mean first-request bytes / estimated tokens | 1,213 / 1,631 | 1,235 / 1,642 |

These measurements show better retrieval on this fixture, not a latency or accuracy guarantee for an actual voice pipeline. The legacy schema could accept model-generated values; its `0 / 4` counts locally bound values, not model performance. More fixtures, especially real transcripts and larger inventories, are needed before tuning caches or adjusting language heuristics.

Delivery checks: 67 offline tests, panel JavaScript syntax, and `git diff --check` passed.

For real tool-calling measurements, run `python3 tests/benchmark_resolver.py --endpoint http://127.0.0.1:11434/v1 --model YOUR_MODEL`, with `--ref 7e825008` for the old resolver. The optional bearer token is read from `AURALIS_BENCH_TOKEN`. This sends only the synthetic fixture to the chosen model and runs policy dry-runs; it measures correct decisions, round counts, and end-to-end decision latency. It does not contact HA. No real provider, live HA, or physical hardware was used for the results above. A real local-model comparison and a native Assist smoke test are the next validation steps.
