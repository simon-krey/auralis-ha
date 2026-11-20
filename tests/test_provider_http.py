"""Exercise the actual aiohttp transport against a loopback chat endpoint."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
except ImportError:
    raise unittest.SkipTest("Install tests/requirements.txt to run the HTTP tests") from None

import test_backend as fixtures
from test_actions import state
from test_hue_compound import COMMAND, LIGHT, PURPLE, hue_policy
from test_resolver_v2 import call, calls_response


class ProviderHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.policy = hue_policy()
        self.bridge = fixtures.BackendTests().bridge()
        self.bridge.policy = lambda: self.policy
        self.requests = []
        self.details = {}
        self.status = 200
        self.on_request = None
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.completions)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        session = ClientSession()
        self.addAsyncCleanup(session.close)
        self.enterContext(patch.object(fixtures.backend, "async_get_clientsession", return_value=session))
        self.bridge.settings.update(llm_url=str(server.make_url("/v1")), llm_model="fixture-model")

    async def completions(self, request):
        self.requests.append((request.path, json.loads(await request.read())))
        if self.on_request:
            self.on_request()
        if isinstance(self.reply, str):
            return web.Response(text=self.reply, status=self.status, content_type="application/json")
        return web.json_response(self.reply, status=self.status)

    async def chat(self, prompt=COMMAND, *, dry_run=True):
        self.details = {}
        self.requests.clear()
        return await self.bridge.chat(prompt, "de", SimpleNamespace(user_id=None), None, dry_run=dry_run, details=self.details)

    def add_light(self, target, name):
        self.policy.hass.states.items[target] = state(target, friendly_name=name, supported_color_modes=["rgb"])
        self.policy._location_metadata = lambda area, entry: ("Hue Go" if entry.id.startswith(("light.hue_go", "button.", "number.", "select.")) else None, None)

    def assert_delivery(self, names):
        self.assertEqual(len(self.requests), 1)
        path, payload = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual([tool["function"]["name"] for tool in payload.get("tools", [])], names)
        self.assertEqual(self.details["requests"][0]["request"]["body"], payload)
        self.assertEqual(self.details["requests"][0]["tool_count"], len(names))
        self.assertEqual(self.details["requests"][0]["tool_names"], names)
        if not names:
            self.assertNotIn("tools", payload)
        return payload

    def assert_dry_run(self, result, expected):
        self.assertEqual(self.details["action_outcome"], "dry_run_validated")
        self.assertEqual([(row["action"], row["result"]["planned"], row["parameters"]) for row in result["tools"]], expected)
        self.assertFalse(self.policy.hass.services.calls)

    async def test_hue_compound_tools_reach_http_and_order_is_preserved(self):
        self.reply = calls_response([call("set_light_color_2", PURPLE, "color"), call("turn_on_1", {}, "power")])
        self.reply["choices"][0]["finish_reason"] = "tool_calls"
        result = await self.chat()
        payload = self.assert_delivery(["turn_on_1", "set_light_color_2"])
        self.assertEqual(payload["tools"][1]["function"]["parameters"]["properties"]["rgb_color"]["enum"], [[128, 0, 128]])
        self.assertNotIn("hue_go_identify", json.dumps(payload))
        request = self.details["requests"][0]
        self.assertEqual(request["tool_delivery"], "sent")
        self.assertEqual(request["finish_reason"], "tool_calls")
        self.assertEqual(request["selected_tools"], ["set_light_color_2", "turn_on_1"])
        self.assert_dry_run(result, [("turn_on", [LIGHT], {}), ("light_set", [LIGHT], PURPLE)])

    async def test_power_and_color_single_actions(self):
        for prompt, current, action in (("Schalte die Hue Go Lampe an", "off", "turn_on"), ("Schalte die Hue Go Lampe aus", "on", "turn_off")):
            with self.subTest(action=action):
                self.policy.hass.states.get(LIGHT).state = current
                self.reply = calls_response([call("confirm_action", {"approved": True})])
                result = await self.chat(prompt)
                self.assert_delivery(["confirm_action"])
                self.assert_dry_run(result, [(action, [LIGHT], {})])
        self.reply = calls_response([call("set_light_color", PURPLE)])
        result = await self.chat("Ändere die Farbe der Hue Go Lampe auf lila")
        self.assert_delivery(["set_light_color"])
        self.assert_dry_run(result, [("light_set", [LIGHT], PURPLE)])

    async def test_multi_device_compound_and_parameterized_bulk(self):
        desk = "light.desk"
        self.add_light(desk, "Schreibtischspot")
        brightness = {"brightness_pct": 40}
        self.reply = calls_response([call("turn_on_1", {}, "on"), call("set_light_brightness_2", brightness, "dim")])
        result = await self.chat("Schalte die Hue Go Lampe an und dimme den Schreibtischspot auf 40 Prozent")
        self.assert_delivery(["turn_on_1", "set_light_brightness_2"])
        self.assert_dry_run(result, [("turn_on", [LIGHT], {}), ("light_set", [desk], brightness)])
        self.reply = calls_response([call("set_light_brightness", brightness)])
        result = await self.chat("Dimme alle Lampen auf 40 Prozent")
        payload = self.assert_delivery(["set_light_brightness"])
        self.assertEqual(payload["tools"][0]["function"]["parameters"]["properties"]["brightness_pct"]["enum"], [40])
        self.assert_dry_run(result, [("light_set", [LIGHT, desk], brightness)])

    async def test_single_approval_and_bounded_choice_use_different_contracts(self):
        self.bridge.settings["llm_plan_approval"] = "single"
        self.reply = calls_response([call("approve_plan", {"approved": True})])
        result = await self.chat()
        payload = self.assert_delivery(["approve_plan"])
        self.assertEqual(payload["tools"][0]["function"]["parameters"]["properties"], {"approved": {"type": "boolean"}})
        self.assert_dry_run(result, [("turn_on", [LIGHT], {}), ("light_set", [LIGHT], PURPLE)])
        self.reply = calls_response([call("choose_action", {"choice": "option_1"})])
        result = await self.chat("Wahlweise stell die Hue Go Lampe auf 30 Prozent oder schalte die Hue Go Lampe aus.")
        payload = self.assert_delivery(["choose_action"])
        self.assertEqual(payload["tools"][0]["function"]["parameters"]["properties"]["choice"]["enum"], ["option_1", "option_2"])
        self.assert_dry_run(result, [("light_set", [LIGHT], {"brightness_pct": 30})])

    async def test_full_context_fallback_preserves_package_and_filtered_overflow_stops(self):
        self.reply = calls_response([call("turn_on_1", {}, "power"), call("set_light_color_2", PURPLE, "color")])
        await self.chat()
        filtered = self.assert_delivery(["turn_on_1", "set_light_color_2"])
        self.bridge.settings["llm_full_context"] = True
        for index in range(100):
            self.add_light(f"light.distractor_{index}", f"Lange zusätzliche Leuchtenbezeichnung für die Katalogansicht {index}" * 2)
        result = await self.chat()
        self.assertTrue(self.details["context_budget"]["fallback"])
        self.assertEqual(self.assert_delivery(["turn_on_1", "set_light_color_2"]), filtered)
        self.assert_dry_run(result, [("turn_on", [LIGHT], {}), ("light_set", [LIGHT], PURPLE)])
        with self.assertRaisesRegex(fixtures.backend.AuralisError, "Kontextbudget"):
            await self.chat("x" * 16000)
        self.assertFalse(self.requests)
        self.assertEqual(self.details["action_outcome"], "context_budget_exceeded")

    async def test_missing_malformed_and_incomplete_model_approvals(self):
        self.reply = {"choices": [{"finish_reason": "stop", "message": {"content": "Erledigt!"}}]}
        result = await self.chat()
        self.assert_delivery(["turn_on_1", "set_light_color_2"])
        self.assertFalse(result["tools"])
        self.assertNotIn("Erledigt", result["answer"])
        self.assertEqual(self.details["action_outcome"], "model_no_tool_calls")
        self.reply["choices"][0]["message"]["content"] = '{"approved":true}'
        self.assertFalse((await self.chat(dry_run=False))["tools"])
        self.assert_delivery(["turn_on_1", "set_light_color_2"])
        self.assertEqual(self.details["action_outcome"], "model_no_tool_calls")
        self.assertFalse(self.policy.hass.services.calls)
        original = [call("turn_on_1", {}, "on"), call("set_light_color_2", PURPLE, "color")]
        malformed = {"id": "color", "type": "function", "function": {"name": "set_light_color_2", "arguments": '{"rgb_color":[128,0,128],'}}
        cases = (([original[0]], "stop"), ([original[0], malformed], "tool_calls"),
                 ([original[0], call("set_light_color_2", PURPLE, "on")], "tool_calls"),
                 ([original[0], call("turn_on_1", {}, "duplicate")], "tool_calls"),
                 ([original[0], call("set_light_color_2", {"rgb_color": [0, 0, 255]}, "color")], "tool_calls"),
                 ([], "tool_calls"), (original, "length"))
        for returned, reason in cases:
            with self.subTest(returned=returned, reason=reason):
                self.reply = calls_response(returned)
                self.reply["choices"][0]["finish_reason"] = reason
                with self.assertRaises(fixtures.backend.AuralisError):
                    await self.chat(dry_run=False)
                self.assert_delivery(["turn_on_1", "set_light_color_2"])
                self.assertEqual(self.details["action_outcome"], "model_output_truncated" if reason == "length" else "model_invalid_tool_calls")
                self.assertFalse(self.details["selection"].get("parameters_validated"))
                self.assertFalse(self.policy.hass.services.calls)

    async def test_single_approval_keeps_large_parameterized_bulk_within_budget(self):
        self.bridge.settings.update(llm_plan_approval="single", llm_full_context=True)
        targets = [LIGHT]
        for index in range(700):
            target = f"light.extra_brightness_test_fixture_{index}"
            self.add_light(target, f"Zusätzliche dimmbare Testlampe {index}")
            targets.append(target)
        self.reply = calls_response([call("approve_plan", {"approved": True})])
        result = await self.chat("Dimme alle Lampen auf 40 Prozent")
        self.assert_delivery(["approve_plan"])
        self.assertTrue(self.details["context_budget"]["fallback"])
        self.assert_dry_run(result, [("light_set", targets, {"brightness_pct": 40})])

    async def test_service_failure_after_first_step_reports_partial_execution(self):
        self.reply = calls_response([call("turn_on_1", {}, "power"), call("set_light_color_2", PURPLE, "color")])
        services = self.policy.hass.services
        record = services.async_call

        async def fail_color(domain, service, data, **kwargs):
            if "rgb_color" in data:
                raise ValueError("color service failed")
            await record(domain, service, data, **kwargs)

        services.async_call = fail_color
        result = await self.chat(dry_run=False)
        self.assert_delivery(["turn_on_1", "set_light_color_2"])
        self.assertEqual(self.details["action_outcome"], "partial_failure")
        self.assertEqual(len(services.calls), 1)
        self.assertEqual(result["tools"][0]["result"]["executed"], [LIGHT])
        self.assertTrue(result["tools"][1]["result"]["errors"])

    async def test_permission_or_capability_revoked_during_http_aborts_all_actions(self):
        self.reply = calls_response([call("turn_on_1", {}, "power"), call("set_light_color_2", PURPLE, "color")])
        for change in (lambda: self.policy.overrides.update({f"{LIGHT}:light_set": False}),
                       lambda: self.policy.hass.states.get(LIGHT).attributes.update(supported_color_modes=["brightness"])):
            with self.subTest(change=change):
                self.on_request = change
                result = await self.chat(dry_run=False)
                self.assert_delivery(["turn_on_1", "set_light_color_2"])
                self.assertEqual(self.details["action_outcome"], "preflight_rejected")
                self.assertTrue(self.details["selection"]["parameters_validated"])
                self.assertFalse(result["tools"])
                self.assertFalse(self.policy.hass.services.calls)
                self.policy.overrides.clear()

    async def test_ambiguous_restricted_negated_unsupported_and_read_only_suppress_tools(self):
        self.add_light("light.other", "Hue Go")
        self.reply = {"choices": [{"message": {"content": "Unverändert."}}]}
        self.assertFalse((await self.chat("Schalte die Hue Go Lampe an"))["tools"])
        self.assert_delivery([])
        del self.policy.hass.states.items["light.other"]
        self.policy.overrides[f"{LIGHT}:turn_on"] = False
        self.assertFalse((await self.chat("Schalte die Hue Go Lampe an"))["tools"])
        self.assertEqual(self.details["action_outcome"], "capability_or_policy_rejected")
        self.assert_delivery([])
        self.policy.overrides.clear()
        self.policy.hass.states.get(LIGHT).attributes["supported_color_modes"] = ["brightness"]
        self.assertFalse((await self.chat("Ändere die Farbe der Hue Go Lampe auf lila"))["tools"])
        self.assertEqual(self.details["action_outcome"], "capability_or_policy_rejected")
        self.assert_delivery([])
        for prompt in ("Schalte die Hue Go Lampe nicht an", "Ist die Hue Go Lampe an?"):
            self.assertFalse((await self.chat(prompt))["tools"])
            self.assert_delivery([])
        self.reply = calls_response([call("confirm_action", {"approved": True})])
        with self.assertRaises(fixtures.backend.AuralisError):
            await self.chat("Ist die Hue Go Lampe an?", dry_run=False)
        self.assert_delivery([])
        self.assertFalse(self.policy.hass.services.calls)

    async def test_internal_contract_violations_never_reach_http(self):
        with patch.object(fixtures.backend, "_plan_tools", return_value={}):
            with self.assertRaises(fixtures.backend.AuralisError):
                await self.chat()
        self.assertEqual(self.details["action_outcome"], "tool_generation_failed")
        self.assertFalse(self.requests)

        timed_request = self.bridge.timed_request

        async def strip_tools(kind, details, origin, *, payload, **kwargs):
            payload.pop("tools")
            return await timed_request(kind, details, origin, payload=payload, **kwargs)

        with patch.object(self.bridge, "timed_request", side_effect=strip_tools):
            with self.assertRaises(fixtures.backend.AuralisError):
                await self.chat()
        self.assertEqual(self.details["action_outcome"], "required_tools_missing")
        self.assertFalse(self.requests)

    async def test_provider_error_and_invalid_json_are_distinct_and_redacted(self):
        self.status = 400
        self.reply = {"error": {"message": "Cannot load tool schema (Bearer secret2)"}}
        with self.assertRaisesRegex(fixtures.backend.AuralisError, "HTTP 400"):
            await self.chat()
        self.assert_delivery(["turn_on_1", "set_light_color_2"])
        self.assertEqual(self.details["action_outcome"], "provider_rejected")
        self.assertEqual(self.details["requests"][0]["http_status"], 400)
        self.assertNotIn("secret2", json.dumps(self.details))
        self.status, self.reply = 200, "{ invalid json"
        with self.assertRaises(fixtures.backend.AuralisError):
            await self.chat()
        self.assert_delivery(["turn_on_1", "set_light_color_2"])
        self.assertEqual(self.details["action_outcome"], "model_invalid_response")
        self.assertEqual(self.details["requests"][0]["tool_delivery"], "sent")
        self.reply = '{"choices":null,' + json.dumps(calls_response([call("turn_on_1", {}, "power"), call("set_light_color_2", PURPLE, "color")]))[1:]
        with self.assertRaises(fixtures.backend.AuralisError):
            await self.chat(dry_run=False)
        self.assertEqual(self.details["action_outcome"], "model_invalid_response")
        self.assertFalse(self.policy.hass.services.calls)

    async def test_filtered_finish_never_approves_action(self):
        self.reply = calls_response([call("turn_on_1", {}, "power"), call("set_light_color_2", PURPLE, "color")])
        self.reply["choices"][0]["finish_reason"] = "content_filter"
        with self.assertRaises(fixtures.backend.AuralisError):
            await self.chat()
        self.assertEqual(self.details["action_outcome"], "model_invalid_tool_calls")
        self.assertFalse(self.details["selection"].get("preflight_passed"))
        self.assertFalse(self.policy.hass.services.calls)
