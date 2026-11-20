# Resolver 3.0: tool delivery and approval

The integration manifest identifies this update as 1.7.0. No release was published.

## What changed

- The resolver mode and plan determine the required tool count. The tool package is built and validated once, then reused unchanged when the full inventory exceeds the context budget. The outbound payload is checked again before the HTTP request; an inconsistent package or an oversized filtered request fails closed. Large single-approval bulk plans use a compact count and scope summary; execution still uses the complete local target bindings.
- Action approval no longer starts with instructions to answer in spoken text. The configured conversation prompt still applies to requests without tools. Model approval requires a real tool call, not text or JSON in the message content. Missing, duplicate, malformed, incomplete, refused, filtered, and truncated approvals cannot reach preflight.
- The entire plan is checked against live policy and capabilities before the first action. Device service failures can still produce partial results; no rollback or automatic retry is attempted. Dry-run and live summaries come from the actual policy results, without a second model call.
- Admin diagnostics distinguish generated tools, the redacted HTTP body and names, provider rejection or unconfirmed delivery, returned tool names and finish reason, validation, and execution. Timings separate catalog, resolver, schemas, context building, budget serialization, provider round trip, preflight, and execution. Catalog generation computes the default permission classification once per state per build; overrides and execution checks stay live.

## Verification

- **110 tests**, including an actual loopback `/v1/chat/completions` server. The tests cover order, parameters, shared aliases, bounded selection, a 701-light fallback, tool suppression, permission/capability revocation, malformed replies, and partial service failure. HA state, services, and provider replies are fixtures in these tests.
- Offline mock-provider benchmark: **219/219** exact held-out offers and **169/169** complete executable decisions in both strategies; no unsafe offers or unintended plans. Development: **218/219** exact offers and **168/169** complete executable decisions; “Glaskere” for “Glaskerze” still does not resolve. The original 14-case benchmark kept 14/14 exact target sets.
- Synthetic full-context benchmark with 1,041 states, against `2df1842` on the same corpus: catalog p50/p95 improved from **38.54/44.12 ms** to **25.04/31.89 ms**; complete offline decision p50/p95 went from **46.80/56.72 ms** to **32.77/45.41 ms**. Schema plus context-build p50 went from 1.95 to 1.92 ms, and budget serialization remained about 0.40 ms. Resolution accuracy was unchanged; this is not a live Home Assistant registry measurement.

## Real provider dry-run

Five paired German cases against local Ollama 0.40.16, `gemma4:e2b` (Q4_K_M, CPU), 8,192 context, filtered inventory, `reasoning_effort: "none"`. All requests used the integration's real HTTP transport. No HA services or devices were contacted. The model was already loaded; these are not cold-start numbers.

| Metric | Multi-tool | Single-plan |
| --- | ---: | ---: |
| First HTTP payloads with all required tools | 5/5 | 5/5 |
| Provider replies confirming delivery | 4/5 | 4/5 |
| Valid tool decisions / correct complete plans | 4/5 | 4/5 |
| Provider latency p50 / p95, including timeouts | 43.18 / 58.72 s | 39.95 / 57.37 s |
| Time to a correct decision p50 / p95 | 43.01 / 50.32 s | 39.95 / 44.52 s |
| Mean reported tokens, input / output | 268.75 / 18.25 | 261 / 14 |
| Requests per command | 1 | 1 |

Multi timed out on the Hue Go turn-on + purple compound; single approved that full ordered plan. Single timed out on the standalone purple request. Prefer **single-plan approval** for this compound on this setup; the sample is too small to justify changing everyone's default. A separate baseline run with provider-default reasoning timed out on all eight different cases tested; it is not a controlled before/after comparison.

Compatibility probes confirmed that this endpoint emits OpenAI-style JSON-string tool arguments and `finish_reason: "tool_calls"`. It accepted `tool_choice: "required"` but also called a tool under `"none"` and when a nonexistent function was forced, so Auralis does not rely on that setting. JSON-schema `response_format` produced structured text in an isolated probe, not a tool approval. No forced tool choice, structured-text execution, or recovery retry was enabled. The local context budget remains an estimate, not an endpoint tokenizer or context-window guarantee.

## Reproduce

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m unittest discover -s tests -q
python3 tests/benchmark_resolver_v3.py --suite held-out --strategy multi
python3 tests/benchmark_resolver_v3.py --suite held-out --strategy single
python3 tests/benchmark_resolver_v3.py --distractors 1000 --full-context
python3 tests/benchmark_resolver_v3.py --delivery-cases --endpoint http://127.0.0.1:11434/v1 --model gemma4:e2b --reasoning off --strategy multi --timeout 70
python3 tests/benchmark_resolver_v3.py --delivery-cases --endpoint http://127.0.0.1:11434/v1 --model gemma4:e2b --reasoning off --strategy single --timeout 70
```
