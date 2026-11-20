"""Exercise the actual aiohttp transport against a loopback chat endpoint."""

import asyncio
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
except ImportError:
    raise unittest.SkipTest("Install tests/requirements.txt to run the HTTP tests") from None

import test_backend as fixtures
from test_actions import ActionPolicy, Services, States, state
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
        self.response_delay = 0
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
        if self.response_delay:
            await asyncio.sleep(self.response_delay)
        if isinstance(self.reply, str):
            return web.Response(text=self.reply, status=self.status, content_type="application/json")
        return web.json_response(self.reply, status=self.status)

    async def chat(self, prompt=COMMAND, *, dry_run=True):
        self.details = {}
        self.requests.clear()
        return await self.bridge.chat(prompt, "de", SimpleNamespace(user_id=None), None, dry_run=dry_run, details=self.details)

    def setup_voice(self):
        self.areas = {key: SimpleNamespace(id=key, name=name, aliases=(), floor_id=None)
                      for key, name in (("office", "Büro"), ("bedroom", "Schlafzimmer"))}
        self.devices = {"parent": SimpleNamespace(area_id="office"), "office-device": SimpleNamespace(area_id=None, parent="parent"),
                        "bedroom-device": SimpleNamespace(area_id="bedroom"), "mobile": SimpleNamespace(area_id=None)}
        self.registry = {}
        helpers = sys.modules["homeassistant.helpers"]
        self.enterContext(patch.object(helpers, "entity_registry", SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=self.registry.get)), create=True))
        self.enterContext(patch.object(helpers, "area_registry", SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get_area=self.areas.get)), create=True))
        devices = SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=self.devices.get),
                                  async_get_effective_area_id=lambda hass, device: device.area_id or getattr(self.devices.get(getattr(device, "parent", None)), "area_id", None))
        self.enterContext(patch.object(helpers, "device_registry", devices, create=True))
        self.policy = ActionPolicy(SimpleNamespace(states=States(), services=Services()), self.bridge.overrides)
        self.source("assist_satellite.office", device="office-device")
        self.source("assist_satellite.bedroom", device="bedroom-device")
        self.source("assist_satellite.mobile", device="mobile")
        self.device("light.desk", "Schreibtischlampe", device="office-device", supported_color_modes=["rgb"])
        self.device("light.bed", "Nachttischlampe", device="bedroom-device", supported_color_modes=["rgb"])
        self.bridge.settings["llm_plan_approval"] = "single"
        self.reply = calls_response([call("approve_plan", {"approved": True})])

    def source(self, target, *, area=None, device=None, platform="test"):
        entry = SimpleNamespace(id=target, area_id=area, device_id=device, aliases=(), platform=platform)
        self.registry[target] = entry
        return entry

    def device(self, target, name, *, area=None, device=None, platform="test", **attributes):
        self.source(target, area=area, device=device, platform=platform)
        self.policy.hass.states.items[target] = entity = state(target, friendly_name=name, **attributes)
        return entity

    async def voice(self, prompt, origin=("office-device", "assist_satellite.office"), *, dry_run=True, user_id=None, memory_key=None):
        self.requests.clear()
        self.details = {}
        return await self.bridge.chat(prompt, "de", SimpleNamespace(user_id=user_id), None, dry_run=dry_run,
                                      details=self.details, voice_origin=origin, memory_key=(memory_key, user_id, *origin) if memory_key else None)

    async def test_room_pipeline_and_implicit_lights_never_leave_the_input_area(self):
        self.setup_voice()
        self.device("switch.plug", "Radio Steckdose", area="office", device_class="outlet")
        self.device("climate.office", "Radiator", area="office", supported_features=1, min_temp=10, max_temp=30)
        for prompt, origin, expected in (
            ("Schalte das Licht hier aus.", ("office-device", "assist_satellite.office"), [("turn_off", ["light.desk"], {})]),
            ("Mach das Licht aus", (None, "assist_satellite.bedroom"), [("turn_off", ["light.bed"], {})]),
            ("Schalte das Licht aus", ("office-device", None), [("turn_off", ["light.desk"], {})]),
            ("Schalte das Licht hier im Schlafzimmer aus", ("office-device", None), [("turn_off", ["light.bed"], {})]),
            ("Schalte das Licht im Schlafzimmer aus", (None, "assist_satellite.mobile"), [("turn_off", ["light.bed"], {})]),
            ("Schalte die Steckdosen in diesem Raum aus", ("office-device", None), [("turn_off", ["switch.plug"], {})]),
            ("Stell die Heizung hier auf 21 Grad", ("office-device", None), [("climate_temperature", ["climate.office"], {"temperature": 21})]),
            ("Mach hier alles aus", ("office-device", None), [("turn_off", ["light.desk", "switch.plug"], {})]),
            ("Mach im Schlafzimmer alles aus", ("office-device", None), [("turn_off", ["light.bed"], {})]),
        ):
            with self.subTest(prompt=prompt, origin=origin):
                result = await self.voice(prompt, origin)
                self.assert_delivery(["approve_plan"])
                self.assert_dry_run(result, expected)
        self.policy.hass.states.get("light.desk").state = "off"
        result = await self.voice("Mach in diesem Zimmer alle Lampen an.")
        self.assert_dry_run(result, [("turn_on", ["light.desk"], {})])
        self.assertEqual(self.details["voice_origin"]["source"], "parent_device")

    async def test_missing_conflicting_and_manual_areas(self):
        self.setup_voice()
        for origin in ((None, "assist_satellite.mobile"), (None, None), ("bedroom-device", "assist_satellite.office")):
            result = await self.voice("Schalte das Licht bei mir aus", origin)
            self.assertIn("Welchen Raum", result["answer"])
            self.assertFalse(self.requests)
        self.registry["assist_satellite.office"].area_id = "bedroom"
        self.assertIn("Welchen Raum", (await self.voice("Mach hier alles aus"))["answer"])
        result = await self.voice("Schalte die Schreibtischlampe aus", (None, None))
        self.assertFalse(result["tools"])
        self.assertEqual(self.details["action_outcome"], "unverified_voice_input")
        await self.bridge.save({"voice_room_overrides": {"assist_satellite.mobile": "office"}})
        result = await self.voice("Schalte das Licht hier aus", (None, "assist_satellite.mobile"))
        self.assert_dry_run(result, [("turn_off", ["light.desk"], {})])
        self.assertEqual(self.details["voice_origin"]["source"], "manual")
        self.devices["mobile"].area_id = "bedroom"
        result = await self.voice("Schalte das Licht hier aus", (None, "assist_satellite.mobile"))
        self.assert_dry_run(result, [("turn_off", ["light.bed"], {})])
        with self.assertRaises(ValueError):
            await self.bridge.save({"voice_room_overrides": {"mobile": "no-such-room"}})

    async def test_exposure_and_user_permissions_are_live(self):
        self.setup_voice()
        active = True
        allowed = True

        async def get_user(user_id):
            return SimpleNamespace(id=user_id, is_active=active, is_admin=False,
                                   permissions=SimpleNamespace(check_entity=lambda target, permission: allowed or permission == "read"))

        self.bridge.hass.auth = SimpleNamespace(async_get_user=get_user)

        def revoke():
            nonlocal active
            active = False

        self.on_request = revoke
        result = await self.voice("Schalte die Schreibtischlampe aus", user_id="user", dry_run=False)
        self.assert_delivery(["approve_plan"])
        self.assertFalse(result["tools"])
        self.assertEqual(self.details["selection"]["preflight_failed"], "user_not_authorized")
        active = True

        def revoke_control():
            nonlocal allowed
            allowed = False

        self.on_request = revoke_control
        result = await self.voice("Schalte die Schreibtischlampe aus", user_id="user", dry_run=False)
        self.assertFalse(result["tools"])
        self.assertEqual(self.details["action_outcome"], "preflight_rejected")
        self.on_request = lambda: setattr(self.policy.hass, "hidden", {"light.desk"})
        result = await self.voice("Schalte die Schreibtischlampe aus", dry_run=False)
        self.assert_delivery(["approve_plan"])
        self.assertEqual(self.details["action_outcome"], "preflight_rejected")
        self.assertFalse(result["tools"])
        self.on_request = None
        self.reply = calls_response([])
        result = await self.voice("Schalte die Schreibtischlampe aus", dry_run=False)
        payload = self.assert_delivery([])
        self.assertNotIn("light.desk", json.dumps(payload))
        self.assertFalse(result["tools"])
        self.assertFalse(self.policy.hass.services.calls)

    async def test_room_group_and_anonymous_sensitive_actions_cannot_expand_scope(self):
        self.setup_voice()
        self.device("light.combined", "Sammellicht", area="office", platform="group", entity_id=["light.desk", "light.bed"])
        self.device("lock.front", "Haustür", area="office")
        self.bridge.overrides["lock.front:unlock"] = True
        self.reply = calls_response([])
        for prompt in ("Schalte das Sammellicht hier aus", "Entriegle die Haustür"):
            result = await self.voice(prompt, dry_run=False)
            self.assertFalse(result["tools"])
            self.assert_delivery([])
        self.policy.hass.states.get("light.combined").attributes["entity_id"] = ["light.desk"]
        self.reply = calls_response([call("approve_plan", {"approved": True})])
        result = await self.voice("Schalte das Sammellicht hier aus")
        self.assert_dry_run(result, [("turn_off", ["light.desk"], {})])
        self.policy.hass.hidden = {"light.desk"}
        self.reply = calls_response([])
        self.assertFalse((await self.voice("Schalte das Sammellicht hier aus"))["tools"])
        self.assert_delivery([])

    async def test_room_moves_and_new_scope_reject_before_first_service(self):
        self.setup_voice()
        self.on_request = lambda: setattr(self.devices["parent"], "area_id", "bedroom")
        result = await self.voice("Schalte das Licht hier aus", dry_run=False)
        self.assert_delivery(["approve_plan"])
        self.assertFalse(result["tools"])
        self.assertEqual(self.details["selection"]["preflight_failed"], "voice_area_changed")
        self.devices["parent"].area_id = "office"
        self.on_request = lambda: self.device("light.new", "Neue Leuchte", area="office")
        result = await self.voice("Schalte das Licht hier aus", dry_run=False)
        self.assertFalse(result["tools"])
        self.assertEqual(self.details["selection"]["preflight_failed"], "room_scope_changed")
        self.assertFalse(self.policy.hass.services.calls)

    async def test_room_everything_skips_nonexposed_and_sensitive_targets(self):
        self.setup_voice()
        self.device("switch.hidden", "Nicht exponiert", area="office", device_class="outlet")
        self.device("siren.office", "Alarmsirene", area="office", supported_features=3)
        self.bridge.overrides["siren.office:turn_off"] = True
        self.policy.hass.hidden = {"switch.hidden"}
        result = await self.voice("Mach hier alles aus", dry_run=False)
        payload = self.assert_delivery(["approve_plan"])
        self.assertNotIn("switch.hidden", json.dumps(payload))
        self.assertNotIn("siren.office", json.dumps(payload))
        self.assertEqual(result["tools"][0]["result"]["executed"], ["light.desk"])
        self.assertEqual(self.details["action_outcome"], "partial_failure")
        self.assertEqual(len(self.policy.hass.services.calls), 1)

    async def test_storage_failures_never_report_executed_action_as_failed(self):
        self.setup_voice()
        self.bridge.settings["local_fast_path"] = True

        async def fail(_data):
            raise OSError("no disk space")

        self.bridge.store.async_save = self.bridge.history_store.async_save = fail
        with self.assertRaises(OSError):
            await self.bridge.save({"local_fast_path": False})
        self.assertTrue(self.bridge.settings["local_fast_path"])
        with self.assertLogs(fixtures.backend._LOGGER, level="WARNING") as messages:
            result = await self.voice("Schalte das Licht hier aus", dry_run=False)
        self.assertIn("OSError", str(messages.output))
        self.assertNotIn("no disk space", str(messages.output))
        self.assertIn("ausgeschaltet", result["answer"])
        self.assertFalse(self.requests)
        self.assertEqual(len(self.policy.hass.services.calls), 1)

    async def test_mid_execution_area_change_is_partial_and_never_retried(self):
        self.setup_voice()
        self.device("light.second", "Leselampe", area="office")
        services = self.policy.hass.services
        record = services.async_call

        async def move(*args, **kwargs):
            await record(*args, **kwargs)
            self.devices["parent"].area_id = "bedroom"

        services.async_call = move
        result = await self.voice("Schalte das Licht hier aus", dry_run=False)
        self.assert_delivery(["approve_plan"])
        self.assertEqual(self.details["action_outcome"], "partial_failure")
        self.assertEqual(result["tools"][0]["result"]["executed"], ["light.desk"])
        self.assertEqual(len(services.calls), 1)

    async def test_fast_path_and_followup_isolate_satellite_room_and_conversation(self):
        self.setup_voice()
        self.bridge.settings["local_fast_path"] = True
        self.policy.hass.states.get("light.desk").state = "off"
        await self.voice("Mach das Licht hier an", dry_run=False, memory_key="session")
        self.assertFalse(self.requests)
        self.assertEqual(self.details["selection"]["strategy"], "local_fast_path")
        result = await self.voice("Mach es jetzt blau", dry_run=False, memory_key="session")
        self.assert_delivery(["approve_plan"])
        self.assertEqual(result["tools"][0]["result"]["executed"], ["light.desk"])
        self.assertEqual(result["tools"][0]["parameters"], {"rgb_color": [0, 0, 255]})
        self.reply = calls_response([])
        for origin, conversation in (((None, "assist_satellite.bedroom"), "session"), (("office-device", "assist_satellite.office"), "other")):
            self.assertFalse((await self.voice("Mach es jetzt blau", origin, dry_run=False, memory_key=conversation))["tools"])
            self.assert_delivery([])
        async def get_user(user_id):
            return SimpleNamespace(id=user_id, is_active=True, permissions=SimpleNamespace(check_entity=lambda *_: True))

        self.bridge.hass.auth = SimpleNamespace(async_get_user=get_user)
        self.assertFalse((await self.voice("Mach es jetzt blau", dry_run=False, memory_key="session", user_id="other-user"))["tools"])
        self.devices["parent"].area_id = "bedroom"
        self.assertFalse((await self.voice("Mach es jetzt blau", dry_run=False, memory_key="session"))["tools"])
        self.assertEqual(len(self.policy.hass.services.calls), 2)

    async def test_http_timeout_is_not_retried(self):
        self.setup_voice()
        self.response_delay = 0.06
        timeout = fixtures.backend.aiohttp.ClientTimeout(total=0.015)
        with patch.object(fixtures.backend.aiohttp, "ClientTimeout", return_value=timeout):
            with self.assertRaisesRegex(fixtures.backend.AuralisError, "Zeitüberschreitung"):
                await self.voice("Schalte das Licht hier aus", dry_run=False)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([item["function"]["name"] for item in self.requests[0][1]["tools"]], ["approve_plan"])
        self.assertEqual(self.details["action_outcome"], "provider_timeout")
        self.assertFalse(self.policy.hass.services.calls)

    async def test_manual_warmup_only_sends_benign_minimal_request(self):
        self.setup_voice()
        self.bridge.settings["llm_reasoning"] = "off"
        self.reply = {"choices": [{"message": {"content": "OK"}, "finish_reason": "length"}]}
        result = await self.bridge.warm_model()
        self.assertIn("duration_ms", result)
        self.assertEqual(self.requests, [("/v1/chat/completions", {"model": "fixture-model", "messages": [{"role": "user", "content": "OK"}],
                                                               "max_tokens": 1, "reasoning_effort": "none"})])
        self.assertFalse(self.policy.hass.services.calls)

    async def test_unregistered_second_service_cannot_start_plan(self):
        self.setup_voice()
        self.device("climate.office", "Heizkörper", area="office", supported_features=1, min_temp=10, max_temp=30)
        self.policy.hass.services.has_service = lambda domain, service: (domain, service) != ("climate", "set_temperature")
        result = await self.voice("Schalte die Schreibtischlampe aus und stell die Heizung auf 21 Grad", dry_run=False)
        self.assert_delivery(["approve_plan"])
        self.assertFalse(result["tools"])
        self.assertFalse(self.policy.hass.services.calls)
        self.assertEqual(self.details["action_outcome"], "preflight_rejected")
        self.assertEqual(self.details["selection"]["preflight_failed"]["errors"][0]["reason"], "service not registered")

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

        async def change_schema(kind, details, origin, *, payload, **kwargs):
            payload["tools"][1]["function"]["parameters"]["properties"]["rgb_color"]["enum"] = [[255, 0, 0]]
            return await timed_request(kind, details, origin, payload=payload, **kwargs)

        with patch.object(self.bridge, "timed_request", side_effect=change_schema):
            with self.assertRaises(fixtures.backend.AuralisError):
                await self.chat()
        self.assertEqual(self.details["action_outcome"], "required_tools_missing")
        self.assertFalse(self.requests)

    async def test_wrong_model_role_and_false_success_text_never_act(self):
        self.reply = calls_response([call("confirm_action", {"approved": True})])
        self.reply["choices"][0]["message"]["role"] = "user"
        with self.assertRaises(fixtures.backend.AuralisError):
            await self.chat("Schalte die Hue Go Lampe an", dry_run=False)
        self.assert_delivery(["confirm_action"])
        self.reply = {"choices": [{"message": {"role": "assistant", "content": "Erledigt, die Lampe ist an."}}]}
        result = await self.chat("Schalte die Hue Go Lampe nicht an", dry_run=False)
        self.assert_delivery([])
        self.assertEqual(result["answer"], "Nichts wurde geändert.")
        self.assertFalse(self.policy.hass.services.calls)

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
