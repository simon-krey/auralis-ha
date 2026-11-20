"""Shared device names and ordered color changes through the Assist test pipeline."""

from types import SimpleNamespace
import unittest

import test_backend as fixtures
from test_actions import ActionPolicy, Services, States, state
from test_resolver_v2 import call, calls_response
from bridge_test.resolver import resolve


COMMAND = "Schalte die Hue Go Lampe an und ändere ihre farbe auf lila"
LIGHT = "light.hue_go"
PURPLE = {"rgb_color": [128, 0, 128]}


def hue_policy():
    states = States(
        state("button.hue_go_identify", friendly_name="Hue Go Identify"),
        state(LIGHT, friendly_name="Hue Go", supported_color_modes=["rgb"]),
        state("number.hue_go_effect_speed", friendly_name="Hue Go Effect speed", min=0, max=100),
        state("select.hue_go_power_on_behavior", friendly_name="Hue Go Power on behavior", options=["on", "off"]),
    )
    states.get(LIGHT).state = "off"
    policy = ActionPolicy(SimpleNamespace(states=states, services=Services()), {})
    policy._registry_entry = lambda target: SimpleNamespace(id=target, device_id="hue-go-device", aliases=(), platform="hue", entity_category=None if target.startswith("light.") else "config")
    policy._location_metadata = lambda area, entry: ("Hue Go", None)
    return policy


class HueCompoundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.policy = hue_policy()
        self.bridge = fixtures.BackendTests().bridge()
        self.bridge.policy = lambda: self.policy
        self.context = SimpleNamespace(user_id=None)

    async def chat(self, prompt=COMMAND, **kwargs):
        return await self.bridge.chat(prompt, "de", self.context, None, **kwargs)

    def resolve(self, prompt, reference=()):
        return resolve(prompt, self.policy.catalog(for_retrieval=True), "de", reference)

    async def test_reported_command_has_two_bound_tools_and_one_llm_round(self):
        requests = []

        async def request(_kind, *, payload):
            requests.append(payload)
            return calls_response([call("set_light_color_2", PURPLE, "color"), call("turn_on_1", {}, "power")])

        self.bridge.request = request
        details = {}
        try:
            result = await self.chat(dry_run=True, details=details)
        finally:
            self.assertEqual(len(requests), 1)
            self.assertEqual([item["function"]["name"] for item in requests[0].get("tools", [])], ["turn_on_1", "set_light_color_2"], details["selection"]["matches"])

        power, color = [item["function"]["parameters"] for item in requests[0]["tools"]]
        system_prompt = requests[0]["messages"][0]["content"]
        self.assertIn("Werkzeugaufrufe werden geprüft", system_prompt)
        self.assertNotIn("Beschreibe geplante Aktionen als Vorschau", system_prompt)
        self.assertEqual(power, {"type": "object", "properties": {}, "required": [], "additionalProperties": False})
        self.assertEqual(color, {"type": "object", "required": ["rgb_color"], "additionalProperties": False, "properties": {
            "rgb_color": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 255}, "minItems": 3, "maxItems": 3, "enum": [[128, 0, 128]]}}})
        self.assertEqual([item["id"] for item in details["selection"]["entities"]], [LIGHT])
        self.assertEqual([(tool["action"], tool["result"]["planned"], tool["parameters"]) for tool in result["tools"]], [
            ("turn_on", [LIGHT], {}), ("light_set", [LIGHT], PURPLE)])
        self.assertFalse(self.policy.hass.services.calls)

    def test_spoken_variants(self):
        variants = (
            ("Schalte die Hue Go Lampe an.", [("turn_on", {})]),
            ("Ändere die Farbe der Hue Go Lampe auf lila.", [("light_set", PURPLE)]),
            ("Mach die Hue Go an und stell sie auf blau.", [("turn_on", {}), ("light_set", {"rgb_color": [0, 0, 255]})]),
            ("Schalte das Hue Go Licht ein und ändere die Farbe auf rot.", [("turn_on", {}), ("light_set", {"rgb_color": [255, 0, 0]})]),
            ("Schalte die Hue Go Lampe an und verändere ihre Farbe auf lila.", [("turn_on", {}), ("light_set", PURPLE)]),
            ("Schalte die Hue Go Lampe an und ändere deren Farbe auf lila.", [("turn_on", {}), ("light_set", PURPLE)]),
            ("Schalte die Hue Go Lampe an und setze ihre Farbe auf lila.", [("turn_on", {}), ("light_set", PURPLE)]),
            ("Turn the Hue Go lamp on and change its color to purple.", [("turn_on", {}), ("light_set", PURPLE)]),
        )
        for prompt, expected in variants:
            with self.subTest(prompt=prompt):
                result = self.resolve(prompt)
                self.assertEqual([(step.action, step.parameters) for step in result.steps], expected)
                self.assertTrue(all(step.targets == [LIGHT] for step in result.steps))
        self.policy.hass.states.get(LIGHT).state = "on"
        self.assertEqual(self.resolve("Schalte die Hue Go Lampe aus.").steps[0].action, "turn_off")

    def test_references_and_other_entities_do_not_authorize_a_substitute(self):
        followup = "Ändere ihre Farbe auf lila."
        self.assertEqual(self.resolve(followup).trace.get("reason"), "unresolved_reference")
        self.assertEqual(self.resolve(followup, (LIGHT,)).steps[0].parameters, PURPLE)
        self.assertEqual(self.resolve(followup, (LIGHT, "button.hue_go_identify")).trace.get("reason"), "ambiguous_reference")
        self.assertEqual(self.resolve(followup, ("light.removed",)).trace.get("reason"), "unresolved_reference")
        for prompt in ("Schalte Hue Go Identify ein.", "Stelle Hue Go Effect speed auf einen Wert.",
                       "Schalte die Hue Go Lampe an und ändere die Farbe der unbekannten Lampe auf lila.",
                       "Schalte die Hue Go Lampe an und ändere ihre Farbe irgendwo auf lila.",
                       "Schalte die Hue Go Lampe an und ändere ihre Farbe auf lila sofort an.",
                       "Schalte die Hue Go Lampe an und andere ihre Farbe auf lila.",
                       "Ändere die Farbe auf lila."):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.resolve(prompt).steps, [])

    def test_more_specific_helper_name_is_not_replaced_by_shared_alias(self):
        result = self.resolve("Schalte die Hue Go Identify Lampe ein.")
        self.assertEqual(result.trace["reason"], "target_type_mismatch")
        self.assertEqual([entity["id"] for entity in result.entities], ["button.hue_go_identify"])
        self.assertFalse(result.steps)
        self.assertFalse(self.resolve("Schalte die Hue Go 2 Lampe an.").steps)

    def test_two_lights_sharing_device_alias_are_ambiguous_even_if_one_is_disabled(self):
        other = "light.hue_go_right"
        self.policy.hass.states.items[other] = state(other, friendly_name="Hue Go Right", supported_color_modes=["rgb"])
        self.policy.overrides[f"{other}:turn_on"] = False
        for prompt in ("Schalte die Hue Go Lampe an.", "Schalte Hue Go an."):
            with self.subTest(prompt=prompt):
                result = self.resolve(prompt)
                self.assertEqual(result.trace["reason"], "ambiguous_target")
                self.assertEqual({entity["id"] for entity in result.entities}, {LIGHT, other})
                self.assertFalse(result.steps)
        self.assertEqual(self.resolve("Schalte Hue Go Right aus.").steps[0].targets, [other])
        self.policy.overrides.clear()
        result = self.resolve("Schalte light.hue_go und Hue Go Right an und ändere ihre Farbe auf lila.")
        self.assertEqual(result.trace["clauses"][-1]["reason"], "ambiguous_reference")
        self.assertFalse(result.steps)

    def test_display_name_collision_is_not_evidence_of_same_physical_device(self):
        registry_entry = self.policy._registry_entry

        def different_device(target):
            entry = registry_entry(target)
            if target == "button.hue_go_identify":
                entry.device_id = "different-device"
            return entry

        self.policy._registry_entry = different_device
        result = self.resolve("Schalte Hue Go an.")
        self.assertEqual(result.trace["reason"], "ambiguous_target")
        self.assertFalse(result.steps)
        self.assertEqual(self.resolve("Schalte die Hue Go Lampe an.").steps[0].targets, [LIGHT])

    def test_type_qualifiers_disambiguate_two_controllable_domains(self):
        plug = "switch.hue_go_outlet"
        self.policy.hass.states.items[plug] = state(plug, friendly_name="Hue Go Outlet", device_class="outlet")
        registry_entry = self.policy._registry_entry

        def enabled_outlet(target):
            entry = registry_entry(target)
            if target == plug:
                entry.entity_category = None
            return entry

        self.policy._registry_entry = enabled_outlet
        self.assertEqual(self.resolve("Schalte die Hue Go Steckdose an.").steps[0].targets, [plug])
        self.assertEqual(self.resolve("Schalte das Hue Go Licht an.").steps[0].targets, [LIGHT])
        self.assertEqual(self.resolve("Schalte Hue Go an.").trace["reason"], "ambiguous_target")

    async def test_no_tools_if_either_operation_is_not_allowed_or_not_supported(self):
        requests = []

        async def request(_kind, *, payload):
            requests.append(payload)
            return {"choices": [{"message": {"content": "No action"}}]}

        self.bridge.request = request
        for denied, color_mode in (("turn_on", "rgb"), ("light_set", "rgb"), (None, "onoff"), (None, "brightness"), (None, "rgb")):
            with self.subTest(denied=denied, color_mode=color_mode):
                self.policy.overrides.clear()
                self.policy.hass.states.get(LIGHT).attributes["supported_color_modes"] = [color_mode]
                if denied:
                    self.policy.overrides[f"{LIGHT}:{denied}"] = False
                prompt = COMMAND if denied or color_mode != "rgb" else "Schalte die Hue Go Lampe an und ändere die Farbe der unbekannten Lampe auf lila."
                result = await self.chat(prompt)
                self.assertFalse(requests[-1].get("tools"))
                self.assertFalse(result["tools"])
                self.assertFalse(self.policy.hass.services.calls)

    async def test_second_action_policy_revoked_after_tool_exposure_aborts_whole_plan(self):
        async def request(_kind, *, payload):
            self.assertEqual(len(payload["tools"]), 2)
            self.policy.overrides[f"{LIGHT}:light_set"] = False
            return calls_response([call("turn_on_1", {}, "power"), call("set_light_color_2", PURPLE, "color")])

        self.bridge.request = request
        result = await self.chat()
        self.assertIn("preflight_failed", result["diagnostics"]["selection"])
        self.assertFalse(result["tools"])
        self.assertFalse(self.policy.hass.services.calls)

    async def test_possessive_followup_requires_conversation_memory(self):
        async def request(_kind, *, payload):
            tools = payload.get("tools", [])
            if not tools:
                return {"choices": [{"message": {"content": "No action"}}]}
            name = tools[0]["function"]["name"]
            return calls_response([call(name, {"approved": True} if name == "confirm_action" else PURPLE)])

        self.bridge.request = request
        key = "conversation", None, "device", "satellite"
        await self.chat("Schalte die Hue Go Lampe an.", memory_key=key)
        result = await self.chat("Ändere ihre Farbe auf lila.", memory_key=("other", *key[1:]))
        self.assertFalse(result["tools"])
        result = await self.chat("Ändere ihre Farbe auf lila.", memory_key=key)
        self.assertEqual([(tool["action"], tool["result"]["executed"], tool["parameters"]) for tool in result["tools"]], [("light_set", [LIGHT], PURPLE)])
        self.assertEqual([args for args, _ in self.policy.hass.services.calls], [
            ("light", "turn_on", {"entity_id": LIGHT}), ("light", "turn_on", {"entity_id": LIGHT, **PURPLE})])
