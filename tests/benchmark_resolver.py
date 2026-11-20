"""Offline first-request benchmark. Run with python3 tests/benchmark_resolver.py."""

import asyncio
import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
from urllib.request import HTTPRedirectHandler, Request, build_opener

import test_backend as fixtures
from test_actions import ActionPolicy, Services, States, state


def environment():
    devices = (
        ("light.reading", "Leselampe", "Wohnzimmer", "brightness"),
        ("light.sofa", "Sofalampe", "Wohnzimmer", "rgb"),
        ("light.desk", "Desk Lamp", "Büro", "rgb"),
        ("light.corner", "Corner Lamp", "Schlafzimmer", "onoff"),
        ("light.one", "Leiste 1", "Schlafzimmer", "brightness"),
        ("light.two", "Leiste 2", "Schlafzimmer", "brightness"),
        ("fan.ceiling", "Ceiling Fan", "Wohnzimmer", 49),
        ("fan.basic", "Basic Fan", "Büro", 48),
        ("light.hall", "Hall Lamp", "Flur", "brightness"),
        ("light.other", "Hall Lamp", "Keller", "brightness"),
    )
    states = States(*(state(eid, friendly_name=name, area=area, **(
        {"supported_color_modes": [cap]} if isinstance(cap, str) else {"supported_features": cap}
    )) for eid, name, area, cap in devices))
    policy = ActionPolicy(SimpleNamespace(states=states, services=Services()), {})
    policy._area = lambda eid: SimpleNamespace(id=states.get(eid).attributes["area"], name=states.get(eid).attributes["area"])
    return policy


CASES = (
    ("de", "Schalte die Leselampe und die Sofalambbe aus", {"light.reading": "turn_off", "light.sofa": "turn_off"}, None),
    ("de", "Schalte die Leselampe und den Kronleuchter aus", {}, None),
    ("de", "Stelle alle Lampen im Wohnzimmer auf 40 Prozent", {"light.reading": "light_set", "light.sofa": "light_set"}, {"brightness_pct": 40}),
    ("en", "Set the Ceiling Fan to 30 percent", {"fan.ceiling": "fan_set_percentage"}, {"percentage": 30}),
    ("en", "Set the Basic Fan to 30 percent", {}, None),
    ("en", "Turn the Desk Lamp off and set the Ceiling Fan to 30 percent", {"light.desk": "turn_off", "fan.ceiling": "fan_set_percentage"}, {"percentage": 30}),
    ("en", "Turn off the Hall Lamp", {}, None),
    ("en", "Turn off the Hall Lamp in the Flur", {"light.hall": "turn_off"}, None),
    ("de", "Schalte Leiste 3 aus", {}, None),
    ("de", "Schalte alle Lampen im Schlaf Zimmer aus", {"light.corner": "turn_off", "light.one": "turn_off", "light.two": "turn_off"}, None),
    ("en", "Which lights are on?", {}, None),
    ("en", "Turn off the Desk Lamp if the Ceiling Fan is on", {}, None),
    ("en", "Turn off the Desk Lamp and the Corner Lamp", {"light.desk": "turn_off", "light.corner": "turn_off"}, None),
    ("de", "Mach die Sofalampe blau", {"light.sofa": "light_set"}, {"rgb_color": [0, 0, 255]}),
)


def _case_pairs(results, result_key):
    return {(target, tool["action"]) for tool in results for target in tool["result"][result_key]}


def _p95(samples):
    return statistics.quantiles(samples, n=100)[94]


def _approval(payload):
    calls = []
    for index, tool in enumerate(payload.get("tools", [])):
        name = tool["function"]["name"]
        props = tool["function"]["parameters"]["properties"]
        if name == "confirm_action":
            args = {"approved": True}
        elif name == "run_action":
            args = {"action": props["action"]["enum"][0], "entity_ids": [props["entity_ids"]["items"]["enum"][0]], "parameters": {}}
        else:
            args = {key: schema["enum"][0] for key, schema in props.items()}
        calls.append({"id": f"test-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
    return {"choices": [{"message": {"tool_calls": calls}}]}


async def measure():
    policy = environment()
    bridge = fixtures.BackendTests().bridge()
    bridge.settings.update(history_enabled=False)
    bridge.policy = lambda: policy
    timings, resolver_timings, sizes, estimates = [], [], [], []
    expected, actual = set(), set()
    exact = unsafe = correct_params = clarify = 0

    async def capture(_kind, *, payload):
        body = {key: payload.get(key, []) for key in ("messages", "tools")}
        sizes.append(len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()))
        return {"choices": [{"message": {"content": ""}}]}

    bridge.request = capture
    catalog = policy.catalog(for_retrieval=True)
    for repeat in range(30):
        for index, (language, prompt, wanted, parameters) in enumerate(CASES):
            started = time.perf_counter_ns()
            if hasattr(fixtures.backend, "resolve"):
                fixtures.backend.resolve(prompt, catalog, language)
            else:
                fixtures.backend.select_context(prompt, catalog, trace={}, language=language)
            resolver_timings.append((time.perf_counter_ns() - started) / 1e6)
            started = time.perf_counter_ns()
            result = await bridge.chat(prompt, language, SimpleNamespace(user_id=None), None, dry_run=True)
            timings.append((time.perf_counter_ns() - started) / 1e6)
            if repeat:
                continue
            details = result["diagnostics"]
            selection = details["selection"]
            if "estimate" in details.get("context_budget", {}):
                estimates.append(details["context_budget"]["estimate"])
            plan = selection.get("plan", [])
            if plan:
                pairs = {(target, step["action"]) for step in plan for target in step["targets"]}
            else:
                targets = selection["resolved_targets"] or [item["id"] for item in selection["entities"]]
                pairs = {(target, action) for target in targets for action in selection.get("offered_actions", [])}
            expected.update((index, *pair) for pair in wanted.items())
            actual.update((index, *pair) for pair in pairs)
            exact += {target for target, _ in pairs} == wanted.keys()
            unsafe += bool(pairs - wanted.items())
            correct_params += parameters is not None and any(step["parameters"] == parameters for step in plan)
            clarify += not wanted and selection["status"] in {"clarify", "no_action"}

    def score(wanted, offered):
        return {"recall": len(wanted & offered) / len(wanted), "precision": len(wanted & offered) / max(1, len(offered))}

    async def approve(_kind, *, payload):
        if len(payload["messages"]) > 2:
            return {"choices": [{"message": {"content": ""}}]}
        return _approval(payload)

    bridge.request = approve
    false_positive = partial = 0
    for language, prompt, wanted, _ in CASES:
        try:
            result = await bridge.chat(prompt, language, SimpleNamespace(user_id=None), None, dry_run=True)
        except fixtures.backend.AuralisError:
            continue
        pairs = _case_pairs(result["tools"], "planned")
        false_positive += bool(pairs - wanted.items())
        partial += bool(pairs and pairs < wanted.items())
    assert not policy.hass.services.calls
    print(json.dumps(dict(cases=len(CASES), entity=score({pair[:2] for pair in expected}, {pair[:2] for pair in actual}),
                          action=score(expected, actual), target_set_exact=exact, correct_fixed_parameters=correct_params,
                          parameter_cases=sum(params is not None for _, _, _, params in CASES), appropriate_clarifications=clarify,
                          unsafe_offers=unsafe, false_positive_dry_run=false_positive, incomplete_dry_run=partial,
                          resolver_p50_ms=statistics.median(resolver_timings), resolver_p95_ms=_p95(resolver_timings),
                          offline_chat_p50_ms=statistics.median(timings), offline_chat_p95_ms=_p95(timings),
                          first_request_bytes_mean=statistics.mean(sizes), first_request_estimated_tokens_mean=statistics.mean(estimates),
                          real_model_rounds=None, end_to_end_model_ms=None), indent=2))


async def measure_model(endpoint, model):
    bridge = fixtures.BackendTests().bridge()
    bridge.settings.update(llm_model=model, llm_url=endpoint, history_enabled=False)
    policy = environment()
    bridge.policy = lambda: policy
    headers = {"Content-Type": "application/json"}
    if (token := os.environ.get("AURALIS_BENCH_TOKEN")):
        headers["Authorization"] = f"Bearer {token}"
    url = fixtures.backend.llm_route(fixtures.backend.llm_base_url(endpoint), "chat/completions")

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = build_opener(NoRedirect())

    def request(payload):
        http_request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with opener.open(http_request, timeout=60) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("Provider response exceeded 1 MiB")
            return json.loads(body)

    async def provider(_kind, *, payload):
        return await asyncio.to_thread(request, payload)

    bridge.request = provider
    durations, rounds = [], []
    correct = errors = false_positive = 0
    for language, prompt, wanted, parameters in CASES:
        details = {}
        began = time.perf_counter()
        try:
            result = await bridge.chat(prompt, language, SimpleNamespace(user_id=None), None, dry_run=True, details=details)
            pairs = _case_pairs(result["tools"], "planned")
            parameter_ok = parameters is None or any(tool["parameters"] == parameters and tool["result"]["planned"] for tool in result["tools"])
            if pairs == wanted.items() and parameter_ok:
                correct += 1
                durations.append((time.perf_counter() - began) * 1000)
            false_positive += bool(pairs - wanted.items())
        except fixtures.backend.AuralisError:
            errors += 1
        rounds.append(len(details["requests"]))
    assert not policy.hass.services.calls
    print(json.dumps({"cases": len(CASES), "correct_dry_run_decisions": correct, "errors": errors,
                      "false_positive_dry_run": false_positive, "model_rounds_mean": statistics.mean(rounds),
                      "correct_decision_p50_ms": statistics.median(durations) if durations else None,
                      "correct_decision_p95_ms": _p95(durations) if len(durations) >= 2 else None}, indent=2))


def at_revision(ref, model_args):
    with tempfile.TemporaryDirectory() as folder:
        archive = Path(folder) / "baseline.tar"
        root = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "archive", f"--output={archive}", ref, "custom_components"], cwd=root, check=True)
        with tarfile.open(archive) as source:
            source.extractall(folder, filter="data")
        tests = Path(folder) / "tests"
        tests.mkdir()
        for name in ("benchmark_resolver.py", "test_actions.py", "test_backend.py"):
            shutil.copy(root / "tests" / name, tests)
        subprocess.run([sys.executable, str(tests / Path(__file__).name), *model_args], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="compare a Git revision using the same fixture")
    parser.add_argument("--endpoint", help="opt in to real OpenAI-compatible model requests")
    parser.add_argument("--model", help="model ID at --endpoint")
    args = parser.parse_args()
    if bool(args.endpoint) != bool(args.model):
        parser.error("--endpoint and --model must be used together")
    if args.ref:
        at_revision(args.ref, ["--endpoint", args.endpoint, "--model", args.model] if args.endpoint else [])
    elif args.endpoint:
        asyncio.run(measure_model(args.endpoint, args.model))
    else:
        asyncio.run(measure())
