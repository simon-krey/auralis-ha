"""Paired dry-run benchmark for Resolver 3.0, including a separate evaluation corpus."""

import argparse
import asyncio
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch


@dataclass(frozen=True)
class Case:
    text: str
    plan: tuple = ()
    language: str = "de"
    reference: tuple = ()
    clarify: bool = False
    restricted: bool = False
    alternative: tuple = ()
    expected_skips: tuple = ()
    delivery: bool = False
    authenticated: bool = False


def corpus(suite, distractors):
    from test_actions import ActionPolicy, Services, States, state

    development = suite == "dev"
    fixtures = {}
    cases = []

    def add(target, name, area, **attributes):
        fixtures[target] = state(target, friendly_name=name, area=area, **attributes)
        return target

    def light(target, name, area, *modes):
        return add("light." + target, name, area, supported_color_modes=modes or ("rgb", "color_temp"), brightness=102,
                   min_color_temp_kelvin=2000, max_color_temp_kelvin=6500)

    def step(target, action="light_set", **parameters):
        return ((action, (target,) if isinstance(target, str) else tuple(sorted(target)), parameters),)

    names = (
        "Mondglas Korallenschirm Bernsteinbogen Kometenlicht Perlenschale Purpurkelch Nachtkugel Lesebogen Seidenschirm "
        "Feuertulpe Birkenfackel Apfelkelch Achatbogen Sandstrahler Glaskerze Laternenkranz Kieselleuchte Sternblume "
        "Kalkschirm Jadebogen Morgenlicht Veilchenkelch Muschelschein Keramikspot" if development else
        "Ulmenlaterne Mattschein Kupferpilz Blütenstrahler Wolkenkugel Schieferbogen Kirschlaterne Kerzenspot Gelbkelch "
        "Mohnfackel Rosenblende Nadellicht Lavakugel Samtstrahler Salzglas Regenbogenlampe Neonrohr Kronleuchter "
        "Aquaspot Vitrinenschein Lichtkrone Hanfschirm Schilfbogen Mondstein"
    ).split()
    areas = ("Lesezimmer", "Atelier", "Galerie", "Flur") if development else ("Esszimmer", "Werkstatt", "Studio", "Wintergarten")
    for index, name in enumerate(names):
        area = areas[index % len(areas)]
        target = light(f"sample_{index:02}", name, area)
        misspoken = name[:-2] + name[-1] if len(name) >= 8 else name + "h"
        absolute = 25 + index % 45
        if development:
            samples = (
                (f"Stell {name} auf {absolute} Prozent", "light_set", {"brightness_pct": absolute}),
                (f"Mach die {name} blau", "light_set", {"rgb_color": [0, 0, 255]}),
                (f"Schalte die {misspoken} aus", "turn_off", {}),
                (f"Dimme {name} auf ein Viertel", "light_set", {"brightness_pct": 25}),
                (f"Mach {name} heller", "light_set", {"brightness_pct": 50}),
                (f"Stell {name} auf warmweiß", "light_set", {"color_temp_kelvin": 2700}),
                (f"Schalte {name} nicht aus", None, {}),
            )
        else:
            samples = (
                (f"Dimme {name} in der {area} auf {absolute} Prozent" if area == "Werkstatt" else f"Dimme {name} im {area} auf {absolute} Prozent", "light_set", {"brightness_pct": absolute}),
                (f"Stell {name} auf Türkis", "light_set", {"rgb_color": [64, 224, 208]}),
                (f"Mach {misspoken} bitte aus", "turn_off", {}),
                (f"Stelle {name} auf die Hälfte", "light_set", {"brightness_pct": 50}),
                (f"Mach {name} dunkler", "light_set", {"brightness_pct": 30}),
                (f"Ändere die Farbe der {name} auf kaltweiß", "light_set", {"color_temp_kelvin": 6500}),
                (f"Wenn es dunkel ist, schalte {name} aus", None, {}),
            )
        cases.extend(Case(text, step(target, action, **params) if action else ()) for text, action, params in samples)

    first = light("hue_go", "Hue Go", "Büro")
    fixtures[first].state = "off"
    helper = add("button.hue_go_identify", "Hue Go Identify", "Büro")
    number = add("number.hue_go_effect_speed", "Hue Go Effect Speed", "Büro", min=0, max=100)
    option = add("select.hue_go_behavior", "Hue Go Power-on Behavior", "Büro", options=["on", "off"])
    reading = light("reading", "Leselampe", "Wohnzimmer", "brightness")
    sofa = light("sofa", "Sofalampe", "Wohnzimmer", "rgb")
    plain = light("plain", "Einfaches Licht", "Keller", "onoff")
    duplicate = light("duplicate", "Paneel", "Büro")
    downstairs = light("downstairs", "Paneel", "Keller")
    denied = light("denied", "Bilderleuchte", "Büro")
    numbered = light("strip_2", "Leiste 2", "Keller")
    light("strip_1", "Leiste 1", "Keller")
    fan = add("fan.fan", "Ventilator", "Büro", supported_features=49, percentage_step=25)
    heater = add("climate.heater", "Heizung", "Wohnzimmer", supported_features=1, temperature=20, min_temp=12, max_temp=30)
    media = add("media_player.tv", "Fernseher", "Wohnzimmer", supported_features=4, volume_level=0.4)
    group = add("light.living_group", "Wohnzimmergruppe", "Wohnzimmer", entity_id=[reading, sofa])
    lock = add("lock.front", "Haustürschloss", "Flur")
    cases.extend((
        Case("Schalte die Hue Go Lampe an", step(first, "turn_on"), delivery=True),
        Case("Mach die Sofa lampe aus", step(sofa, "turn_off")),
        Case("Ändere die Farbe der Hue Go Lampe auf lila", step(first, rgb_color=[128, 0, 128]), delivery=True),
        Case("Schalte die Hue Go Lampe an und dimme die Sofalampe auf 40 Prozent", step(first, "turn_on") + step(sofa, brightness_pct=40)),
        Case("Dimme alle Lampen im Wohnzimmer auf 40 Prozent", step([reading, sofa], brightness_pct=40), delivery=True),
        Case("Stelle die heligkeit der Sofa lampe auf 45 Prozent", step(sofa, brightness_pct=45)),
        Case("Schalt die Hue Go Lampe an und stell sie auf blau", step(first, "turn_on") + step(first, rgb_color=[0, 0, 255])),
        Case("Mach die Hue Goo Lampe an", clarify=True),
        Case("Mach den Fernseer leiser", clarify=True),
        Case("Mach die Sofa Lampe nicht aus"),
        Case("Schalte die Hue Go Lampe an und ändere ihre Farbe auf lila", step(first, "turn_on") + step(first, rgb_color=[128, 0, 128]), delivery=True),
        Case("Mach die Hue Go an und stell sie auf blau", step(first, "turn_on") + step(first, rgb_color=[0, 0, 255])),
        Case("Schalte Hue Go Identify ein", clarify=True),
        Case("Setze Hue Go Effect Speed auf 50 Prozent", clarify=True),
        Case("Schalte die Hue Go Lampe an und die fehlende Lampe aus", clarify=True),
        Case("Stell die Heizung auf zweiundzwanzig Grad", step(heater, "climate_temperature", temperature=22)),
        Case("Stell die Heizung zwei Grad höher", step(heater, "climate_temperature", temperature=22)),
        Case("Mach den Fernseher leiser", step(media, "volume_set", volume_level=0.3)),
        Case("Stelle den Fernseher auf 30 Prozent", step(media, "volume_set", volume_level=0.3)),
        Case("Stelle den Ventilator auf die niedrigste Geschwindigkeit", step(fan, "fan_set_percentage", percentage=25)),
        Case("Schalte die Wohnzimmerlampen aus außer die Leselampe", step(sofa, "turn_off")),
        Case("Schalte die Paneel Lampe aus", clarify=True),
        Case("Schalte das Paneel im Keller aus", step(downstairs, "turn_off")),
        Case("Schalte alle Lampen im Erdgeschoss aus", step([reading, sofa, duplicate], "turn_off"), expected_skips=(denied,)),
        Case("Schalte das Paneel im Arbeitszimmer aus", step(duplicate, "turn_off")),
        Case("Schalte die Leiste zwei aus", step(numbered, "turn_off")),
        Case("Schalte die Leiste drei aus", clarify=True),
        Case("Schalte die Wohnzimmergruppe aus", step(group, "turn_off")),
        Case("Schalte die Bilderleuchte aus", clarify=True),
        Case("Schalte Leselampe und Sofalampe aus", clarify=True, restricted=True),
        Case("Entriegle das Haustürschlos", clarify=True),
        Case("Entriegle das Haustürschloss", step(lock, "unlock"), authenticated=True),
        Case("Stell das Einfache Licht auf lila", clarify=True),
        Case("Stelle die Leselampe auf kaltweiß", clarify=True),
        Case("Mach die andere Lampe an", clarify=True, reference=(reading, sofa)),
        Case("Mach die beiden auch blau", step([duplicate, downstairs], rgb_color=[0, 0, 255]), reference=(duplicate, downstairs)),
        Case("Jetzt auf 30 Prozent", step(sofa, brightness_pct=30), reference=(sofa,)),
        Case("Erhöhe die Helligkeit um zehn Prozentpunkte", step(sofa, brightness_pct=50), reference=(sofa,)),
        Case("Ändere ihre Farbe auf lila", step(sofa, rgb_color=[128, 0, 128]), reference=(sofa,)),
        Case("Ändere ihre Farbe auf lila", clarify=True),
        Case("Mach es etwas dunkler", clarify=True, reference=(reading,)),
        Case("Ist das Paneel im Keller an?"),
        Case("Mach die Leselampe aus auf die Hälfte", clarify=True),
        Case("Mach die Hue Go Lampe gemütlicher", clarify=True),
        Case("Wahlweise stell die Hue Go Lampe auf 35 Prozent oder schalte die Hue Go Lampe aus", step(first, brightness_pct=35), alternative=step(first, "turn_off"), delivery=True),
        Case("Wahlweise stell die Hue Go Lampe auf 35 Prozent oder schalte die Sofalampe aus", clarify=True),
        Case("Turn the Hue Go lamp on and change its color to purple", step(first, "turn_on") + step(first, rgb_color=[128, 0, 128]), "en"),
        Case("Set the Hue Go lamp to half", step(first, brightness_pct=50), "en"),
        Case("Never turn off the Hue Go lamp", language="en"),
        Case("Turn off all lights in the unknown room", language="en", clarify=True),
        Case("Either set the Hue Go lamp to 35 percent or set the Hue Go lamp to blue", step(first, brightness_pct=35), "en", alternative=step(first, rgb_color=[0, 0, 255])),
    ))
    for index in range(distractors):
        light(f"spare_{index:04}", f"Unbenutzter Testanschluss {index}", f"Lagerraum {index // 20}", "brightness")
    states = States(*fixtures.values())
    overrides = {f"{media}:volume_set": True, f"{lock}:unlock": True, f"{denied}:turn_off": False}
    policy = ActionPolicy(SimpleNamespace(states=states, services=Services()), overrides)
    device_members = {first, helper, number, option}
    policy._registry_entry = lambda eid: SimpleNamespace(id=eid, device_id="hue-go" if eid in device_members else None, aliases=(),
                                                         entity_category="config" if eid in {helper, number, option} else None,
                                                         platform="group" if eid == group else "test")
    floor = SimpleNamespace(name="Erdgeschoss", floor_id="ground", aliases=("Parterre",))
    policy._area = lambda eid: SimpleNamespace(id=fixtures[eid].attributes["area"], name=fixtures[eid].attributes["area"],
                                              aliases=("Arbeitszimmer",) if fixtures[eid].attributes["area"] == "Büro" else ())
    policy._location_metadata = lambda area, entry: ("Hue Go" if entry.device_id else None, floor if area.id in {"Büro", "Wohnzimmer"} else None)
    return tuple(cases), policy


def approved(payload):
    calls = []
    for index, tool in enumerate(payload.get("tools", ())):
        name = tool["function"]["name"]
        properties = tool["function"]["parameters"]["properties"]
        if name in {"confirm_action", "approve_plan"}:
            args = {"approved": True}
        elif name == "run_action":
            args = {"action": properties["action"]["enum"][0], "entity_ids": [properties["entity_ids"]["items"]["enum"][0]], "parameters": {}}
        else:
            args = {key: value["enum"][0] for key, value in properties.items()}
        calls.append({"id": f"call_{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
    return {"choices": [{"message": {"content": "", "tool_calls": calls}}]}


def signature(rows):
    return tuple((row["action"], tuple(sorted(row["targets"] if "targets" in row else row["entity_ids"])), row["parameters"]) for row in rows)


def pairs(plan):
    return {(target, action) for action, targets, _ in plan for target in targets}


def metric(expected, actual):
    correct = len(expected & actual)
    return {"precision": round(correct / max(len(actual), 1), 4), "recall": round(correct / max(len(expected), 1), 4)}


def distribution(samples):
    if not samples:
        return None
    return {"p50": round(statistics.median(samples), 4), "p95": round(statistics.quantiles(samples, n=20, method="inclusive")[18], 4) if len(samples) >= 2 else None}


async def run(args):
    async with AsyncExitStack() as stack:
        if args.endpoint:
            from aiohttp import ClientSession
            session = await stack.enter_async_context(ClientSession())
        from test_backend import BackendTests, backend
        if args.endpoint:
            stack.enter_context(patch.object(backend, "async_get_clientsession", return_value=session))
        await evaluate(args, BackendTests().bridge(), backend)


async def evaluate(args, bridge, backend):
    cases, policy = corpus(args.suite, args.distractors)
    if args.delivery_cases:
        cases = tuple(case for case in cases if case.delivery)[:args.model_cases]
    elif args.endpoint:
        cases = tuple(case for case in cases if not case.reference and (case.plan or case.alternative))[-args.model_cases:]
    bridge.policy = lambda: policy
    bridge.settings.update(llm_plan_approval=args.strategy, history_enabled=False, llm_model=args.model or "offline-test",
                           llm_reasoning=args.reasoning, llm_full_context=args.full_context, local_fast_path=args.fast_path)
    live = bool(args.endpoint)
    if live:
        bridge.settings.update(llm_url=args.endpoint, llm_token=os.environ.get("AURALIS_BENCH_TOKEN", ""))

    async def provider(kind, *, payload):
        return approved(payload)

    if not live:
        bridge.request = provider
    totals = defaultdict(int)
    outcomes = defaultdict(int)
    stages = defaultdict(list)
    action_expected, action_offered = set(), set()
    resolver_ms, decision_ms, fast_decision_ms, rounds, payload_bytes, estimates, provider_ms = [], [], [], [], [], [], []
    usage = defaultdict(list)
    misses = []
    direct = inspect.signature(backend.resolve).parameters
    for index, case in enumerate(cases):
        user = SimpleNamespace(id="limited", is_admin=case.authenticated, is_active=True,
                               permissions=SimpleNamespace(check_entity=lambda eid, permission: permission == "read" or eid != "light.sofa")) if case.restricted or case.authenticated else None
        context = SimpleNamespace(user_id=user.id if user else None)
        async def get_user(user_id):
            return user if user and user_id == user.id else None
        bridge.hass.auth = SimpleNamespace(async_get_user=get_user)
        catalog = policy.catalog(user, for_retrieval=True)
        clock = time.perf_counter()
        resolution = backend.resolve(case.text, catalog, case.language, case.reference, **({"comfort": False} if "comfort" in direct else {}))
        resolver_ms.append((time.perf_counter() - clock) * 1000)
        offer = signature(step.diagnostic() for step in resolution.steps)
        if choices := getattr(resolution, "choices", ()):
            offer = signature(choice.action.diagnostic() for choice in choices)
        expected = pairs(case.plan + case.alternative)
        offered = pairs(offer)
        action_expected.update((index, *pair) for pair in expected)
        action_offered.update((index, *pair) for pair in offered)
        totals["exact_target_sets"] += {target for target, _ in expected} == {target for target, _ in offered}
        totals["exact_offered_plans"] += offer == case.plan + case.alternative
        if any(params for _, _, params in case.plan + case.alternative):
            totals["parameter_cases"] += 1
            totals["exact_parameter_plans"] += offer == case.plan + case.alternative
        totals["unsafe_offers"] += bool(offered - expected)
        totals["clarifications"] += case.clarify
        totals["correct_clarifications"] += case.clarify and not offer and resolution.status in {"clarify", "no_action", "no_match"}
        if case.reference:
            continue
        details = {}
        totals["pipeline_cases"] += 1
        totals["pipeline_action_cases"] += bool(case.plan)
        started = time.perf_counter()
        try:
            async with asyncio.timeout(args.timeout):
                result = await bridge.chat(case.text, case.language, context, user, dry_run=True, details=details)
            rows = [row for row in result["tools"] if row["result"]["planned"]]
            applied = signature(rows)
            accepted = [case.plan] + ([case.alternative] if case.alternative else [])
            skipped = {item["entity_id"] for row in rows for item in row["result"]["skipped"]}
            leaves_match = all(set(row["result"]["planned"]) == {leaf for target in row["entity_ids"]
                               for leaf in policy.hass.states.get(target).attributes.get("entity_id", [target])} for row in rows)
            complete = applied in accepted and leaves_match and skipped == set(case.expected_skips) and all(not row["result"]["errors"] for row in rows)
            totals["complete_validated_decisions"] += complete
            totals["complete_validated_actions"] += complete and bool(applied)
            totals["unexpected_or_incomplete_plans"] += bool(applied and not complete)
            if complete:
                elapsed = (time.perf_counter() - started) * 1000
                decision_ms.append(elapsed)
                if details.get("confirmation", {}).get("state") == "local_fast_path":
                    fast_decision_ms.append(elapsed)
            else:
                misses.append(case.text)
        except (backend.AuralisError, TimeoutError):
            totals["provider_or_validation_errors"] += 1
            misses.append(case.text)
        requests = details.get("requests", [])
        request = requests[0] if requests else {}
        outcome = details.get("action_outcome", "no_diagnostic")
        outcomes[outcome] += 1
        if body := request.get("request", {}).get("body"):
            payload_bytes.append(len(json.dumps(body).encode()))
        totals["first_requests_with_tools"] += bool(request.get("offered_tools"))
        totals["first_requests_with_returned_calls"] += bool(request.get("tool_calls"))
        if case.plan:
            totals["local_fast_path_decisions"] += details.get("confirmation", {}).get("state") == "local_fast_path"
            totals["confirmed_tool_deliveries"] += request.get("tool_delivery", "sent" if request.get("offered_tools") and not request.get("failed") else "unknown") == "sent"
            totals["valid_model_decisions"] += bool(details.get("selection", {}).get("parameters_validated") or details.get("confirmation", {}).get("state") == "rejected")
        if measured := details.get("context_budget", {}).get("rounds"):
            estimates.append(measured[0]["estimate"])
        for name, duration in details.get("local_timings_ms", {}).items():
            stages[name].append(duration)
        rounds.append(len(requests))
        for request in requests:
            provider_ms.append(request.get("provider_ms", request["duration_ms"]))
            for key, count in request.get("usage", {}).items():
                usage[key].append(count)
    assert not policy.hass.services.calls
    expected_count = max(totals["pipeline_action_cases"], 1)
    model_expected_count = max(totals["pipeline_action_cases"] - totals["local_fast_path_decisions"], 1)
    print(json.dumps({"suite": args.suite, "strategy": args.strategy, "fast_path": args.fast_path, "reasoning": args.reasoning, "cases": len(cases), "states": len(policy.hass.states.items),
                      "german_cases": sum(case.language == "de" for case in cases), "live_provider": bool(live),
                      "action_outcomes": outcomes, "tool_delivery_rate": totals["confirmed_tool_deliveries"] / model_expected_count,
                      "valid_tool_call_rate": totals["valid_model_decisions"] / model_expected_count,
                      "complete_action_plan_rate": totals["complete_validated_actions"] / expected_count,
                      **totals, "entity": metric({pair[:2] for pair in action_expected}, {pair[:2] for pair in action_offered}),
                      "action": metric(action_expected, action_offered), "resolver_ms": distribution(resolver_ms),
                      "local_stage_ms": {key: distribution(value) for key, value in stages.items()},
                      "complete_decision_ms": distribution(decision_ms), "local_fast_path_ms": distribution(fast_decision_ms),
                      "first_request_bytes_mean": round(statistics.mean(payload_bytes), 2) if payload_bytes else None,
                      "first_request_budget_estimate_mean": round(statistics.mean(estimates), 2) if estimates else None,
                      "model_rounds_mean": statistics.mean(rounds), "real_provider_ms": distribution(provider_ms) if live else None,
                      "first_request_ms_model_temperature_uncontrolled": provider_ms[0] if live and provider_ms else None,
                      "subsequent_real_provider_ms": distribution(provider_ms[1:]) if live else None,
                      "observed_tokens_mean": {key: round(statistics.mean(value), 2) for key, value in usage.items()},
                      "failed_examples": misses[:12]}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["dev", "held-out"], default="held-out")
    parser.add_argument("--strategy", choices=["multi", "single"], default="multi")
    parser.add_argument("--reasoning", choices=["default", "on", "off"], default="default")
    parser.add_argument("--delivery-cases", action="store_true", help="Use the paired German tool-delivery subset")
    parser.add_argument("--full-context", action="store_true")
    parser.add_argument("--fast-path", action="store_true", help="Enable local approval for eligible light plans")
    parser.add_argument("--distractors", type=int, default=0)
    parser.add_argument("--target", type=Path, help="A baseline checkout or snapshot with tests and custom_components")
    parser.add_argument("--endpoint")
    parser.add_argument("--model")
    parser.add_argument("--model-cases", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=30)
    options = parser.parse_args()
    if bool(options.endpoint) != bool(options.model) or not 0 <= options.distractors <= 4000 or options.model_cases < 1 or options.timeout <= 0:
        parser.error("Use --endpoint and --model together and positive limits (0–4000 distractors).")
    if options.target:
        sys.path.insert(0, str(options.target.resolve() / "tests"))
    asyncio.run(run(options))
