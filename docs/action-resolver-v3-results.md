# Action Resolver 3.0 — design and verification

## Resolution and execution

- **Deterministic:** the resolver builds an ordered plan from complete target sets, intended action families, shared capability schemas, and action permissions. The backend snapshots it into immutable target bindings and values before the first request. Existing per-step approval remains the default; the optional single `approve_plan` tool accepts just `{ "approved": true | false }` for the entire snapshot.
- **Bounded:** `Wahlweise stell die Leselampe auf 30 Prozent oder schalte die Leselampe aus` offers `choose_action` with `choice: "option_1" | "option_2"`. Options are complete, allowed, non-sensitive actions on the **same** exact or word-joined, unique non-group light, fan, or media player. The optional comfort setting gives two documented choices for an eligible light: 30% brightness or 2700 K. The model cannot supply a service name, target, parameter, or action combination. If either option cannot be established, nothing is offered.
- **Clarification:** unknown, conflicting, or incomplete scope never gets tools. Status and already-satisfied requests have a distinct no-action outcome. For two exact, distinguishable and individually valid target matches, Assist can ask which one without a model call. An ordinal reply or a uniquely grounded “Nein, die andere” is re-resolved against the live catalog. Corrections do not undo previous actions.
- Every chosen step and expanded group is preflighted before any service call. `ActionPolicy` rechecks permission, feature support, parameter bounds and group membership at execution. Parameterized light and fan actions also require the underlying power permission; 0% requires off permission, nonzero settings require on permission. Bulk power commands still report denied members rather than silently implying all devices changed. HA service calls are not transactional: a later service failure stops subsequent steps and is reported, without promising rollback.

## Language, native metadata and memory

- Absolute values include half/quarter, German number words through 99 in measure positions, Kelvin and basic colors including turquoise. Warm/cold white are 2700/6500 K, rejected if outside the actual supported range. For the lowest fan speed the reported `percentage_step` must exist; the first nonzero step is sent as an integer percentage (rounded down). Other qualitative speeds are not guessed.
- Bare brightness/volume comparatives use ±10 percentage points, not a proportional percentage. Explicit brightness percentage points and temperature deltas use the current reported value and HA's configured unit. Out-of-range results, turning off as a side effect of relative dimming, missing data, and changes to the underlying state between planning and execution fail closed. Relative commands inside compounds and unspecified “etwas” are not applied. Explicit switch and value changes must be distinct clauses.
- Target identity, the last successful action and parameters, and actionable clarification choices are retained in memory for five minutes, bounded by conversation, user, device and satellite. A dry run neither reads nor writes that memory. “Jetzt auf 30 Prozent” and “Mach die beiden auch blau” require a complete antecedent; “Und die Lampe im Schlafzimmer auch” repeats the last power action only for a unique, different light in a named room. The bare question “Und die Lampe im Schlafzimmer?” does not repeat an action.
- Registry aliases, device names, effective device area inheritance, floor aliases and official fan, cover, media, climate, valve, vacuum, siren and alarm feature enums come from HA 2026.11.2. Explicit supported `hier`/`here` light commands are scoped to the satellite/device area and checked again at preflight. Location preferences never silently break a name tie. Shared physical-device aliases and the Hue Go compound/reference fix are preserved.

### Native API assessment

- [`async_match_targets()` on HA Core 2026.11.2](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/helpers/intent.py) accepts `MatchTargetsConstraints`, optional `MatchTargetsPreferences`, optional states and an area filter. It supports name/alias, domain, device class, feature, area/floor and assistant exposure constraints. Its area/floor preferences can resolve duplicates, and assistant exposure is not Auralis action authorization. Auralis reuses registry metadata rather than substituting the native matcher's winning target for its own complete-scope and ambiguity checks. No HA intent handler is run for discovery.
- [HassIL](https://github.com/OHF-Voice/hassil) and [HA sentence templates](https://contributors.home-assistant.io/translate/voice/template-syntax/) support reusable expansions and lists. The new grammar uses anchored local patterns; adopting templates is worthwhile once a broader voice corpus requires it. [HA ChatLog](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/conversation/chat_log.py) is session-scoped and supports structured messages and tools. It was not adopted for the small reference memory, so no transcript replay or transcript transfer to the LLM is required.
- [Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling) supports tool requests, including parallel calls on compatible models. The backend does not force `tool_choice` or a structured-output extension: provider capability and correct interpretation have not been demonstrated across endpoints. Single-plan approval needs one call but is not assumed to improve live success or latency.

## Offline evaluation, November 20, 2026

Two suites of 209 synthetic requests each (204 German/5 English each), with distinct names/templates and a shared safety smoke matrix. Six reference cases per suite are scored at resolver level; Assist isolation and clarification are exercised separately in the unit suite. The baseline is the uncommitted working-tree snapshot with the completed Hue Go fix and the initial native feature/metadata milestone, before hybrid or matching changes. These generated cases are a repeatable check, not a measured sample of real voice traffic.

| Held-out metric | Baseline | 3.0 multi | 3.0 single |
| --- | ---: | ---: | ---: |
| Exact complete offers, including parameters | 102/209 | 209/209 | 209/209 |
| Exact parameter plans | 30/135 | 135/135 | 135/135 |
| Complete validated actions (mock provider, policy dry run) | 57/162 | 162/162 | 162/162 |
| Correct clarifications | 17/17 | 17/17 | 17/17 |
| Unsafe offers or unexpected/incomplete dry-run plans | 0 | 0 | 0 |
| Resolver p50/p95, 41 states | 0.90 / 2.76 ms | 0.38 / 0.92 ms | 0.38 / 0.88 ms |
| Offline time to a correct decision p50/p95 | 4.69 / 5.14 ms | 2.52 / 2.94 ms | 2.54 / 2.98 ms |
| Mean first request bytes | 1068 | 1371 | 1617 |

The development split scored 208/209 exact offers; “Glaskere” for “Glaskerze” remained unresolved. The original 14-case benchmark retained 14/14 exact target sets and no unsafe or incomplete dry-run plans. All 96 unit tests passed, including Hue Go, approval completeness, capability and permission revocation, malformed output, and follow-up isolation. Panel JavaScript syntax and Git whitespace checks passed.

With **1,041 states**, resolver p50/p95 fell from **129.51/399.98 ms** to **2.77/9.95 ms** on the same held-out cases. Local catalog construction remained the largest median stage in the new pipeline (37.59 ms p50, 43.38 ms p95). Other p50/p95 stages in ms: state enumeration 0.01/0.01; timed registry lookups 1.91/3.21; group work 1.62/3.04; catalog capability checks 4.49/4.68; alias normalization 1.35/1.54; entity matching 0.85/8.09; clause splitting 0.01/0.01; candidate filtering 0.002/0.004; context/tool build 0.03/0.04; budget serialization 0.04/0.05. Substage instrumentation does not cover all catalog work and percentiles do not add. The optimization removes per-alias regex compilation from exact matching and caches only bounded, pure metadata transformations; state, group membership and permissions stay live. Synthetic timings do not explain a specific 213 ms log from a real installation.

```sh
python3 -m unittest discover -s tests -q
python3 tests/benchmark_resolver.py
python3 tests/benchmark_resolver_v3.py --suite dev
python3 tests/benchmark_resolver_v3.py --suite held-out --strategy single
python3 tests/benchmark_resolver_v3.py --suite held-out --distractors 1000
python3 tests/benchmark_resolver_v3.py --target /path/to/baseline-checkout --distractors 1000
python3 tests/benchmark_resolver_v3.py --endpoint http://127.0.0.1:11434/v1 --model gemma4:e2b --strategy single
```

The Ollama port was inaccessible in this checkout. No live model, cold/warm model latency, token counts, completion-rate comparison, or physical device behavior was verified. The opt-in model benchmark sends only synthetic inventory and dry-runs results; it reports the first and subsequent provider latencies separately, not a verified cold start. The next priority is a paired multi/single run on configured small models with confirmed cold/warm states and independently collected, anonymized voice samples. Broader subjective profiles, mixed relative compounds, and real HA catalog profiling remain follow-ups.
