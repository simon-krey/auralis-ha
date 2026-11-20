"""Resolver and execution regressions against real policy and synthetic HA states."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark_resolver import environment
from bridge_test.conversation_memory import ConversationMemory, TTL
from bridge_test.resolver import resolve
from test_actions import Services, state
import test_backend as fixtures


def calls_response(calls):
    return {"choices": [{"message": {"tool_calls": calls}}]}


def call(name, parameters, call_id="1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(parameters)}}


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.policy = environment()
        self.states = self.policy.hass.states.items

    def resolve(self, prompt, language="de", user=None, reference=()):
        return resolve(prompt, self.policy.catalog(user, for_retrieval=True), language, reference)

    def test_complete_spans_or_no_plan(self):
        result = self.resolve("Schalte die Leselampe und die Sofalambbe aus")
        self.assertEqual(result.steps[0].targets, ["light.reading", "light.sofa"])
        self.assertEqual([span["status"] for span in result.trace["target_spans"]], ["resolved", "resolved"])
        self.assertIn(result.trace["names"][1]["method"], {"edit_distance", "phonetic"})
        result = self.resolve("Turn off the Desk Lamp, Corner Lamp, and Leselampe", "en")
        self.assertCountEqual(result.steps[0].targets, ["light.desk", "light.corner", "light.reading"])
        for prompt in ("Schalte die Leselampe und den Mond aus", "Schalte Leselampe oder Mond aus", "Schalte Leselampe samt Mond aus",
                       "Schalte Leselampe und Leiste 4 aus", "Schalte Leselampe, Mond aus", "Schalte Leselampe und Hall Lamp aus",
                       "Schalte Leselampe im Phantasiezimmer aus", "Schalte die Leselampe aus und auch die andere"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.resolve(prompt).steps, [])

    def test_capability_intersection_not_partial(self):
        result = self.resolve("Stelle alle Lampen im Wohnzimmer auf 40 Prozent")
        self.assertEqual(result.steps[0].parameters, {"brightness_pct": 40})
        self.assertEqual(result.steps[0].targets, ["light.reading", "light.sofa"])
        for prompt in ("Dimme alle Lampen auf 20 Prozent", "Mach die Leselampe und die Sofalampe blau", "Mach die Leselampe heller",
                       "Stelle alle Lampen im Wohnzimmer auf 140 Prozent", "Stelle den Basic Fan auf 40 Prozent",
                       "Stelle die Leselampe auf -40 Prozent", "Stelle die Leselampe auf 40 Prozent oder auf 10 Prozent"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.resolve(prompt).steps, [])
        self.policy.overrides["light.sofa:light_set"] = False
        result = self.resolve("Stelle alle Lampen im Wohnzimmer auf 40 Prozent")
        self.assertEqual(result.steps, [])
        self.assertEqual(result.trace["rejected_actions"][0]["reason"], "policy")

    def test_exclusions_and_conditions(self):
        result = self.resolve("Mach alle Lampen außer der im Büro aus")
        self.assertNotIn("light.desk", result.steps[0].targets)
        self.assertEqual(len(result.steps[0].targets), 7)
        result = self.resolve("Schalte die Leselampe aus, aber lass die Sofalampe an")
        self.assertEqual(result.steps[0].targets, ["light.reading"])
        self.assertEqual(result.trace["excluded"], ["light.sofa"])
        for prompt in ("Mach alle Lampen außer dem Mond aus", "Schalte die Leselampe nicht aus", "Wie hell ist die Leselampe?",
                       "Schalte die Leselampe aus wenn ich gehe", "Schalte Leselampe aus, aber lass die Leselampe an",
                       "Schalte die Leselampe aus, aber lass die Sofalampe aus"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.resolve(prompt).steps, [])

    def test_opposite_switch_states_cannot_be_collapsed_into_one_action(self):
        result = self.resolve("Turn on the Desk Lamp and off the Ceiling Fan", "en")
        self.assertEqual([(item.action, item.targets) for item in result.steps], [("turn_on", ["light.desk"]), ("turn_off", ["fan.ceiling"])])
        for prompt in ("Turn the Desk Lamp on and off", "Switch on and off Desk Lamp", "Schalte die Leselampe an und aus"):
            self.assertFalse(self.resolve(prompt, "en").steps)

    def test_floor_alias_and_shared_device_names(self):
        catalog = self.policy.catalog(for_retrieval=True)
        for entity in catalog:
            if entity["area"] in {"Büro", "Wohnzimmer"}:
                entity.update(floor="Ground Floor", floor_id="ground", floor_aliases=["Downstairs"])
            if entity["id"] in {"light.reading", "light.sofa"}:
                entity["aliases"].append("Shared Device")
        result = resolve("Turn off all lights in Downstairs", catalog, "en")
        self.assertCountEqual(result.steps[0].targets, ["light.reading", "light.sofa", "light.desk"])
        self.assertFalse(resolve("Turn off Shared Device", catalog, "en").steps)
        self.assertFalse(resolve("Turn off Hall Lamp", catalog, "en").steps)

    def test_live_ranges_and_options(self):
        self.states.update({item.entity_id: item for item in (
            state("climate.heat", friendly_name="Heizung", area="Büro", supported_features=1, min_temp=16, max_temp=24, hvac_modes=["off", "heat"]),
            state("select.mode", friendly_name="Device Mode", area="Büro", options=["silent", "turbo"]),
            state("media_player.tv", friendly_name="Fernseher", area="Büro", supported_features=4),
            state("light.white", friendly_name="White Panel", area="Büro", supported_color_modes=["color_temp"], min_color_temp_kelvin=2200, max_color_temp_kelvin=4500),
        )})
        self.policy.overrides.update({"select.mode:select_option": True, "media_player.tv:volume_set": True})
        for prompt, value in (("Stelle die Heizung auf 22,5 Grad", {"temperature": 22.5}),
                              ("Choose Device Mode silent", {"option": "silent"}),
                              ("Stelle den Fernseher auf 30 Prozent", {"volume_level": 0.3}),
                              ("Set White Panel to 3000 kelvin", {"color_temp_kelvin": 3000})):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.resolve(prompt).steps[0].parameters, value)
        for prompt in ("Stelle die Heizung auf 25 Grad", "Choose Device Mode burst", "Set White Panel to 5000 kelvin"):
            self.assertFalse(self.resolve(prompt).steps)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.policy = environment()
        self.services = Services()
        self.policy.hass.services = self.services
        self.bridge = fixtures.BackendTests().bridge()
        self.bridge.policy = lambda: self.policy
        self.context = SimpleNamespace(user_id=None)

    async def chat(self, prompt, *, dry_run=False, key=None):
        return await self.bridge.chat(prompt, "en", self.context, None, dry_run=dry_run, memory_key=key)

    async def test_compound_calls_execute_in_semantic_order(self):
        async def request(_kind, *, payload):
            tools = {tool["function"]["name"]: tool["function"]["parameters"] for tool in payload["tools"]}
            self.assertEqual(list(tools), ["turn_off_1", "set_fan_speed_2"])
            self.assertEqual(tools["set_fan_speed_2"]["properties"]["percentage"], {"type": "number", "minimum": 0, "maximum": 100, "enum": [30]})
            self.assertFalse(tools["set_fan_speed_2"]["additionalProperties"])
            return calls_response([call("set_fan_speed_2", {"percentage": 30}, "2"), call("turn_off_1", {})])
        self.bridge.request = request
        result = await self.chat("Turn the Desk Lamp off and set the Ceiling Fan to 30 percent")
        self.assertEqual([entry[0][:2] for entry in self.services.calls], [("light", "turn_off"), ("fan", "set_percentage")])
        self.assertTrue(all(entry[1]["context"] is self.context for entry in self.services.calls))
        self.assertEqual(len(result["diagnostics"]["requests"]), 1)

    async def test_dependent_plan_keeps_later_step_even_if_initially_noop(self):
        async def request(_kind, *, payload):
            return calls_response([call("turn_off_1", {}), call("turn_on_2", {}, "2"), call("set_light_brightness_3", {"brightness_pct": 40}, "3")])
        self.bridge.request = request
        await self.chat("Turn the Desk Lamp off, then turn it on and dim it to 40 percent")
        self.assertEqual([entry[0][2] for entry in self.services.calls], [
            {"entity_id": "light.desk"}, {"entity_id": "light.desk"}, {"entity_id": "light.desk", "brightness_pct": 40}])

    async def test_bad_second_call_never_executes_first(self):
        first = call("turn_off_1", {})
        bad = [[], [call("set_fan_speed_2", {"percentage": 31}, "2")], [call("set_fan_speed_2", {"percentage": True}, "2")],
               [call("set_fan_speed_2", {"percentage": 30}, "1")], [call("turn_off_1", {}, "2")],
               [call("set_fan_speed_2", {"percentage": 30, "entity_id": "fan.basic"}, "2")]]
        duplicate = call("set_fan_speed_2", {}, "2")
        duplicate["function"]["arguments"] = '{"percentage":90,"percentage":30}'
        bad.append([duplicate])
        for second in bad:
            with self.subTest(second=second):
                async def request(_kind, *, payload):
                    return calls_response([first, *second])
                self.bridge.request = request
                with self.assertRaises(fixtures.backend.AuralisError):
                    await self.chat("Turn the Desk Lamp off and set the Ceiling Fan to 30 percent")
                self.assertEqual(self.services.calls, [])

    async def test_capability_revoked_after_model_request_aborts_all(self):
        async def request(_kind, *, payload):
            self.policy.hass.states.get("fan.ceiling").attributes["supported_features"] = 48
            return calls_response([call("turn_off_1", {}), call("set_fan_speed_2", {"percentage": 30}, "2")])
        self.bridge.request = request
        result = await self.chat("Turn the Desk Lamp off and set the Ceiling Fan to 30 percent")
        self.assertIn("Nothing was changed", result["answer"])
        self.assertIn("preflight_failed", result["diagnostics"]["selection"])
        self.assertFalse(self.services.calls)

    async def test_bulk_101_preflights_all_chunks_before_first_execution(self):
        self.policy.hass.states.items.update({item.entity_id: item for item in (
            state(f"fan.test_{index}", friendly_name=f"Bedroom Fan {index}", area="Schlafzimmer", supported_features=49) for index in range(101)
        )})
        async def request(_kind, *, payload):
            self.policy.overrides["fan.test_100:fan_set_percentage"] = False
            return calls_response([call("set_fan_speed", {"percentage": 50})])
        self.bridge.request = request
        result = await self.chat("Set all fans in Schlafzimmer to 50 percent")
        self.assertFalse(self.services.calls)
        self.assertEqual(len(result["diagnostics"]["selection"]["plan"][0]["targets"]), 101)
        self.assertIn("preflight_failed", result["diagnostics"]["selection"])

    async def test_memory_isolation_and_permission_recheck(self):
        response = fixtures.confirmation_response(approved=True)
        async def request(_kind, *, payload):
            return response if payload.get("tools") else {"choices": [{"message": {"content": "Done"}}]}
        self.bridge.request = request
        key = "conversation", None, "device", "satellite"
        await self.chat("Turn off Desk Lamp", key=key)
        response = calls_response([call("set_light_color", {"rgb_color": [0, 0, 255]})])
        for other_key in [("other", *key[1:]), (key[0], "other-user", *key[2:]), (*key[:2], "other-device", key[-1]), (*key[:3], "other-satellite")]:
            result = await self.chat("Make it blue", key=other_key)
            self.assertEqual(result["diagnostics"]["selection"]["matches"]["reason"], "unresolved_reference")
        result = await self.chat("Make it blue", key=key)
        self.assertEqual(result["tools"][0]["entity_ids"], ["light.desk"])
        self.policy.overrides["light.desk:light_set"] = False
        result = await self.chat("Make it blue", key=key)
        self.assertEqual(result["tools"], [])
        self.assertEqual(len(self.services.calls), 2)

    async def test_non_admin_cannot_approve_ineligible_explicit_target(self):
        user = SimpleNamespace(id="limited", is_active=True, is_admin=False,
                               permissions=SimpleNamespace(check_entity=lambda eid, permission: permission == "read" or eid != "light.corner"))
        async def request(_kind, *, payload):
            self.assertNotIn("tools", payload)
            return calls_response([call("turn_off", {})])
        self.bridge.request = request
        with self.assertRaises(fixtures.backend.AuralisError):
            await self.bridge.chat("Turn off Desk Lamp and Corner Lamp", "en", SimpleNamespace(user_id=user.id), user)
        self.assertFalse(self.services.calls)

    async def test_group_overlap_and_changed_members_fail_closed(self):
        self.policy.hass.states.items["group.office"] = state("group.office", friendly_name="Office Pair", area="Büro", entity_id=["light.desk", "light.corner"])
        async def reject(_kind, *, payload):
            self.assertNotIn("tools", payload)
            return {"choices": [{"message": {"content": "Nothing"}}]}
        self.bridge.request = reject
        await self.chat("Turn off Office Pair and Desk Lamp")
        self.assertFalse(self.services.calls)
        async def request(_kind, *, payload):
            self.policy.hass.states.get("group.office").attributes["entity_id"].append("light.reading")
            return calls_response([call("turn_off", {})])
        self.bridge.request = request
        result = await self.chat("Turn off Office Pair")
        self.assertIn("preflight_failed", result["diagnostics"]["selection"])
        self.assertFalse(self.services.calls)

    async def test_service_failure_stops_remaining_operations_and_reports_partial(self):
        regular_call = self.services.async_call
        async def service_call(domain, service, data, **kwargs):
            if domain == "fan":
                raise ValueError("Unavailable: secret2")
            await regular_call(domain, service, data, **kwargs)
        self.services.async_call = service_call
        async def request(_kind, *, payload):
            return calls_response([call("turn_off_1", {}), call("set_fan_speed_2", {"percentage": 30}, "2"), call("turn_on_3", {}, "3")])
        self.bridge.request = request
        result = await self.chat("Turn the Desk Lamp off and set the Ceiling Fan to 30 percent and turn the Corner Lamp on")
        self.assertEqual(len(self.services.calls), 1)
        self.assertIn("Some operations could not be completed", result["answer"])
        self.assertEqual(result["tools"][2]["result"]["skipped"][0]["reason"], "earlier action failed")
        self.assertNotIn("secret2", str(result))

    def test_memory_expires_and_drops_moved_or_unavailable_targets(self):
        memory = ConversationMemory()
        key = "conversation", "user", "device", "satellite"
        original = {"id": "light.desk", "name": "Desk", "area_id": "office", "state": "on"}
        for change in ({"area_id": "kitchen"}, {"state": "unavailable"}, {"name": "New name"}, {"registry_id": "different"}):
            memory.remember(key, [original["id"]], [original])
            self.assertEqual(memory.get(key, [original | change]), ())
        with patch("bridge_test.conversation_memory.time.monotonic", return_value=10):
            memory.remember(key, [original["id"]], [original])
        with patch("bridge_test.conversation_memory.time.monotonic", return_value=10 + TTL):
            self.assertEqual(memory.get(key, [original]), ())
