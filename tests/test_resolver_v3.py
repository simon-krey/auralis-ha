"""Hybrid resolution, voice values and whole-plan execution regressions."""

from types import SimpleNamespace
import unittest

import test_backend as fixtures
from test_actions import state
from test_hue_compound import COMMAND, LIGHT, PURPLE, hue_policy
from test_resolver_v2 import call, calls_response
from bridge_test.resolver import ResolutionMode, resolve


CHOICES = "Wahlweise stell die Hue Go Lampe auf 30 Prozent oder schalte die Hue Go Lampe aus."


def voice_policy():
    policy = hue_policy()
    policy.hass.states.get(LIGHT).attributes.update(supported_color_modes=["rgb", "color_temp"],
                                                    min_color_temp_kelvin=2000, max_color_temp_kelvin=6500, brightness=128)
    policy.hass.states.get(LIGHT).state = "on"
    for added in (state("media_player.tv", friendly_name="Fernseher", supported_features=4, volume_level=0.55),
                  state("climate.heater", friendly_name="Heizung", min_temp=10, max_temp=30, supported_features=1, temperature=20),
                  state("fan.ceiling", friendly_name="Ventilator", supported_features=1, percentage_step=100/3),
                  state("light.sofa", friendly_name="Sofalampe", supported_color_modes=["rgb"], brightness=40)):
        policy.hass.states.items[added.entity_id] = added
    registry = policy._registry_entry
    policy._registry_entry = lambda eid: registry(eid) if "hue_go" in eid else None
    policy._location_metadata = lambda area, entry: ("Hue Go" if entry else None, None)
    policy.overrides["media_player.tv:volume_set"] = True
    return policy


class HybridTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.policy = voice_policy()
        self.bridge = fixtures.BackendTests().bridge()
        self.bridge.policy = lambda: self.policy
        self.context = SimpleNamespace(user_id=None)
        self.requests = []

    def resolve(self, prompt, reference=(), **kwargs):
        return resolve(prompt, self.policy.catalog(for_retrieval=True), "de", reference, **kwargs)

    async def chat(self, prompt, response, *, dry_run=True, key=None, origin=None):
        async def request(kind, *, payload):
            self.requests.append(payload)
            return response(payload) if callable(response) else response

        self.bridge.request = request
        return await self.bridge.chat(prompt, "de", self.context, None, dry_run=dry_run, memory_key=key, voice_origin=origin)

    def test_absolute_and_relative_values(self):
        for prompt, reference, action, target, parameters in (
            ("Stelle das Hue Go Licht auf die Hälfte.", (), "light_set", LIGHT, {"brightness_pct": 50}),
            ("Set the Hue Go light to a quarter.", (), "light_set", LIGHT, {"brightness_pct": 25}),
            ("Dimme auf ein Viertel.", (LIGHT,), "light_set", LIGHT, {"brightness_pct": 25}),
            ("Mach die Helligkeit auf 50 Prozent.", (LIGHT,), "light_set", LIGHT, {"brightness_pct": 50}),
            ("Stell den Fernseher auf 30 Prozent.", (), "volume_set", "media_player.tv", {"volume_level": 0.3}),
            ("Stell die Heizung auf zweiundzwanzig Grad.", (), "climate_temperature", "climate.heater", {"temperature": 22}),
            ("Stelle den Ventilator auf die niedrigste Geschwindigkeit.", (), "fan_set_percentage", "fan.ceiling", {"percentage": 33}),
            ("Mach das Hue Go Licht heller.", (), "light_set", LIGHT, {"brightness_pct": 60}),
            ("Stell die Heizung zwei Grad höher.", (), "climate_temperature", "climate.heater", {"temperature": 22}),
            ("Mach den Fernseher leiser.", (), "volume_set", "media_player.tv", {"volume_level": 0.45}),
            ("Erhöhe die Helligkeit um zehn Prozentpunkte.", (LIGHT,), "light_set", LIGHT, {"brightness_pct": 60}),
            ("Mach die Hue Go Lampe auf warmweiß.", (), "light_set", LIGHT, {"color_temp_kelvin": 2700}),
            ("Ändere die Farbe der Hue Go Lampe auf kaltweiß.", (), "light_set", LIGHT, {"color_temp_kelvin": 6500}),
            ("Mach die Hue Go Lampe türkis.", (), "light_set", LIGHT, {"rgb_color": [64, 224, 208]}),
            ("Jetzt auf 30 Prozent.", (LIGHT,), "light_set", LIGHT, {"brightness_pct": 30}),
        ):
            with self.subTest(prompt=prompt):
                result = self.resolve(prompt, reference)
                self.assertEqual(result.mode, ResolutionMode.DETERMINISTIC, result.trace)
                self.assertEqual([(step.action, step.targets, step.parameters) for step in result.steps], [(action, [target], parameters)])

    def test_compound_and_dual_reference(self):
        result = self.resolve("Mach die Hue Go Lampe auf 40 Prozent und den Ventilator auf 20 Prozent.")
        self.assertEqual([(item.targets, item.parameters) for item in result.steps], [([LIGHT], {"brightness_pct": 40}),
                                                                                      (["fan.ceiling"], {"percentage": 20})], result.trace)
        result = self.resolve("Mach die beiden auch blau.", (LIGHT, "light.sofa"))
        self.assertEqual(set(result.steps[0].targets), {LIGHT, "light.sofa"})

    def test_missing_context_and_unsupported_values_never_plan(self):
        for prompt, reference in (
            ("Mach es etwas dunkler.", (LIGHT,)), ("Mach es dunkler.", ()),
            ("Erhöhe die Helligkeit um zehn Prozent.", (LIGHT,)),
            ("Mach die Sofalampe auf kaltweiß.", ()), ("Mach die Hue Go Lampe gemütlicher.", ()),
            ("Mach die Hue Go Lampe heller und dann die Sofalampe an.", ()),
            ("Stell die Heizung um zweiundzwanzig Grad höher.", ()),
            ("Mach die beiden auch blau.", (LIGHT,)),
            ("Mach die Hue Go Lampe nicht heller.", ()),
            ("Mach die Hue Go Lampe aus auf die Hälfte.", ()),
            ("Schalte die Hue Go Lampe an auf 40 Prozent.", ()),
            ("Mach die andere Lampe an.", (LIGHT,)),
        ):
            with self.subTest(prompt=prompt):
                result = self.resolve(prompt, reference)
                self.assertFalse(result.steps or result.choices, result.trace)

    def test_choices_require_one_exact_authorized_target(self):
        for prompt in (
            "Wahlweise stell die Hue Go Lampe auf 30 Prozent oder schalte die Sofalampe aus.",
            "Wahlweise stell die unbekannte Lampe auf 30 Prozent oder schalte die Hue Go Lampe aus.",
            "Wahlweise stell Hue Goo Lampe auf 30 Prozent oder schalte Hue Goo Lampe aus.",
            "Wahlweise stell die Hue Go Lampe nicht auf 30 Prozent oder schalte die Hue Go Lampe aus.",
            "Wahlweise stell die Hue Go Lampe auf 130 Prozent oder schalte die Hue Go Lampe aus.",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(self.resolve(prompt).choices)
        self.policy.overrides[f"{LIGHT}:light_set"] = False
        self.assertFalse(self.resolve(CHOICES).choices)

    async def test_bounded_choice_can_only_select_original_action(self):
        result = await self.chat(CHOICES, calls_response([call("choose_action", {"choice": "option_1"})]))
        self.assertEqual(len(self.requests), 1)
        tool = self.requests[0]["tools"][0]["function"]
        self.assertEqual(tool["parameters"], {"type": "object", "properties": {"choice": {"type": "string", "enum": ["option_1", "option_2"]}}, "required": ["choice"], "additionalProperties": False})
        self.assertEqual(result["diagnostics"]["selection"]["resolution_mode"], "bounded")
        self.assertEqual([(item["action"], item["entity_ids"], item["parameters"]) for item in result["tools"]], [("light_set", [LIGHT], {"brightness_pct": 30})])
        for returned in ([call("choose_action", {"choice": "option_3"})], [call("choose_action", {"choice": "option_1", "entity_id": "light.sofa"})],
                         [call("choose_action", {"choice": "option_1"}), call("choose_action", {"choice": "option_2"}, "2")],
                         [call("run_action", {"action": "turn_off", "entity_ids": [LIGHT], "parameters": {}})]):
            with self.assertRaises(fixtures.backend.AuralisError):
                await self.chat(CHOICES, calls_response(returned), dry_run=False)
        self.assertFalse(self.policy.hass.services.calls)

    async def test_configured_comfort_can_offer_bounded_choices(self):
        prompt = "Mach die Hue Go Lampe gemütlicher."
        self.assertEqual(self.resolve(prompt).mode, ResolutionMode.CLARIFICATION)
        options = self.resolve(prompt, comfort=True)
        self.assertEqual([choice.action.parameters for choice in options.choices], [{"brightness_pct": 30}, {"color_temp_kelvin": 2700}])
        self.bridge.settings["llm_light_comfort"] = True
        result = await self.chat(prompt, calls_response([call("choose_action", {"choice": "option_2"})]))
        self.assertEqual(result["tools"][0]["parameters"], {"color_temp_kelvin": 2700})
        self.assertFalse(self.policy.hass.services.calls)

    async def test_single_approval_is_whole_ordered_plan(self):
        self.bridge.settings["llm_plan_approval"] = "single"
        result = await self.chat(COMMAND, calls_response([call("approve_plan", {"approved": True})]))
        self.assertEqual([item["function"]["name"] for item in self.requests[0]["tools"]], ["approve_plan"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([(item["action"], item["parameters"]) for item in result["tools"]], [("turn_on", {}), ("light_set", PURPLE)])
        self.assertTrue(result["diagnostics"]["selection"]["preflight_passed"])

        def revoke_second(payload):
            self.policy.overrides[f"{LIGHT}:light_set"] = False
            return calls_response([call("approve_plan", {"approved": True})])

        self.assertFalse((await self.chat(COMMAND, revoke_second, dry_run=False))["tools"])
        self.assertFalse(self.policy.hass.services.calls)

    async def test_single_approval_rejects_partial_approval_or_modified_plan(self):
        self.bridge.settings["llm_plan_approval"] = "single"
        self.assertFalse((await self.chat(COMMAND, calls_response([call("approve_plan", {"approved": False})]), dry_run=False))["tools"])
        for returned in ([call("turn_on_1", {})], [call("approve_plan", {"approved": 1})],
                         [call("approve_plan", {"approved": True, "steps": ["turn_on"]})],
                         [call("approve_plan", {"approved": True}), call("approve_plan", {"approved": True}, "2")]):
            with self.assertRaises(fixtures.backend.AuralisError):
                await self.chat(COMMAND, calls_response(returned), dry_run=False)
        self.assertFalse(self.policy.hass.services.calls)

    async def test_relative_value_is_rechecked_after_model_round(self):
        def changed(payload):
            self.policy.hass.states.get(LIGHT).attributes["brightness"] = 230
            return calls_response([call("set_light_brightness", {"brightness_pct": 60})])

        result = await self.chat("Mach die Hue Go Lampe heller.", changed, dry_run=False)
        self.assertEqual(result["diagnostics"]["selection"]["preflight_failed"], "live_value_changed")
        self.assertFalse(self.policy.hass.services.calls)

    async def test_power_overrides_apply_to_parameterized_service_effects(self):
        self.policy.overrides[f"{LIGHT}:turn_off"] = False
        self.assertFalse(self.resolve("Stell die Hue Go Lampe auf 0 Prozent.").steps)
        self.policy.overrides[f"{LIGHT}:turn_off"] = True
        self.assertTrue(self.resolve("Stell die Hue Go Lampe auf 0 Prozent.").steps)

        def revoke_power(payload):
            self.policy.overrides[f"{LIGHT}:turn_on"] = False
            return calls_response([call("set_light_brightness", {"brightness_pct": 40})])

        result = await self.chat("Stell die Hue Go Lampe auf 40 Prozent.", revoke_power, dry_run=False)
        self.assertFalse(result["tools"])
        self.assertFalse(self.policy.hass.services.calls)

    async def test_pending_choice_is_isolated_and_revalidated(self):
        key = ("one", "user", "device", "satellite")
        other = (*key[:-1], "another-satellite")
        response = calls_response([])
        await self.chat(CHOICES, response, dry_run=False, key=key)
        self.assertFalse((await self.chat("Die erste", response, dry_run=False, key=other))["tools"])
        before = len(self.requests)
        result = await self.chat("Schalte die Sofalampe aus", response, dry_run=False, key=key)
        self.assertIn("abbrechen", result["answer"])
        self.assertEqual(len(self.requests), before)
        result = await self.chat("Die erste", calls_response([call("set_light_brightness", {"brightness_pct": 30})]), dry_run=False, key=key)
        self.assertEqual(result["tools"][0]["entity_ids"], [LIGHT])
        self.assertEqual(len(self.policy.hass.services.calls), 1)

        await self.chat(CHOICES, response, dry_run=False, key=key)
        self.policy.overrides[f"{LIGHT}:light_set"] = False
        result = await self.chat("Die erste", response, dry_run=False, key=key)
        self.assertIn("geändert", result["answer"])
        self.assertEqual(len(self.policy.hass.services.calls), 1)

    async def test_target_clarification_and_grounded_correction(self):
        other = "light.office_hue"
        self.policy.hass.states.items[other] = state(other, friendly_name="Hue Go", supported_color_modes=["rgb"])
        areas = {LIGHT: SimpleNamespace(id="living", name="Wohnzimmer"), other: SimpleNamespace(id="office", name="Büro")}
        self.policy._area = areas.get
        key = ("one", "user", "device", "satellite")
        result = await self.chat("Stell die Hue Go Lampe auf 30 Prozent", calls_response([]), dry_run=False, key=key)
        self.assertEqual(self.requests, [])
        self.assertIn("Büro", result["answer"])
        self.assertIn("Wohnzimmer", result["answer"])
        args = calls_response([call("set_light_brightness", {"brightness_pct": 30})])
        result = await self.chat("Die erste", args, dry_run=False, key=key)
        self.assertEqual(result["tools"][0]["entity_ids"], [LIGHT])
        result = await self.chat("Nein, die andere", args, dry_run=False, key=key)
        self.assertEqual(result["tools"][0]["entity_ids"], [other])
        self.assertEqual(len(self.policy.hass.services.calls), 2)

    async def test_explicit_voice_area_never_infers_another_room(self):
        areas = {LIGHT: SimpleNamespace(id="office", name="Büro"), "light.sofa": SimpleNamespace(id="living", name="Wohnzimmer")}
        self.policy._area = areas.get
        self.policy.voice_area_id = lambda device, satellite: "office" if satellite == "office-satellite" else None
        response = calls_response([call("confirm_action", {"approved": True})])
        prompt = "Schalte das Licht hier aus."
        result = await self.chat(prompt, calls_response([]))
        self.assertEqual(result["diagnostics"]["selection"]["matches"]["reason"], "unknown_voice_area")
        result = await self.chat(prompt, response, origin=("device", "office-satellite"))
        self.assertEqual(result["tools"][0]["result"]["planned"], [LIGHT])

        def moved(payload):
            areas[LIGHT] = areas["light.sofa"]
            return response

        result = await self.chat(prompt, moved, origin=("device", "office-satellite"))
        self.assertEqual(result["diagnostics"]["selection"]["preflight_failed"], "voice_area_changed")
        self.assertFalse(self.policy.hass.services.calls)

    def test_room_compound_with_exclusion_keeps_scope(self):
        area = SimpleNamespace(id="living", name="Wohnzimmer")
        self.policy._area = lambda target: area if target in {LIGHT, "light.sofa"} else None
        result = self.resolve("Schalte die Wohnzimmerlampen aus, außer die Sofalampe.")
        self.assertEqual(result.steps[0].targets, [LIGHT])
        self.assertEqual(result.trace["excluded"], ["light.sofa"])

    async def test_repeat_explicitly_requests_also_in_another_room(self):
        self.policy._area = lambda target: SimpleNamespace(id="bedroom", name="Schlafzimmer") if target == "light.sofa" else None
        key = ("dialog", "user", "device", "satellite")
        response = calls_response([call("confirm_action", {"approved": True})])
        await self.chat("Schalte die Hue Go Lampe aus", response, dry_run=True, key=key)
        result = await self.chat("Und die Lampe im Schlafzimmer auch.", calls_response([]), dry_run=False, key=key)
        self.assertFalse(result["tools"])
        await self.chat("Schalte die Hue Go Lampe aus", response, dry_run=False, key=key)
        result = await self.chat("Und die Lampe im Schlafzimmer auch.", response, dry_run=False, key=key)
        self.assertEqual(result["tools"][0]["entity_ids"], ["light.sofa"])
        result = await self.chat("Und die Lampe im Schlafzimmer?", calls_response([]), dry_run=False, key=key)
        self.assertFalse(result["tools"])
