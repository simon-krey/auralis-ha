"""Policy tests use HA-shaped mocks; no service or device is contacted."""
import asyncio
import importlib.util
from enum import IntFlag
import sys
import types
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("auralis_actions", Path(__file__).parents[1] / "custom_components/auralis/actions.py")
actions = importlib.util.module_from_spec(spec)
# Values copied from HA 2026.11.2 const.py for offline tests only.
_FEATURE_STUBS = {
    "alarm_control_panel": ("AlarmControlPanelEntityFeature", {"ARM_HOME": 1, "ARM_AWAY": 2}),
    "climate": ("ClimateEntityFeature", {"TARGET_TEMPERATURE": 1}),
    "cover": ("CoverEntityFeature", {"OPEN": 1, "CLOSE": 2, "SET_POSITION": 4, "STOP": 8}),
    "fan": ("FanEntityFeature", {"SET_SPEED": 1, "OSCILLATE": 2, "TURN_OFF": 16, "TURN_ON": 32}),
    "media_player": ("MediaPlayerEntityFeature", {"PAUSE": 1, "VOLUME_SET": 4, "PREVIOUS_TRACK": 16, "NEXT_TRACK": 32, "STOP": 4096, "PLAY": 16384}),
    "siren": ("SirenEntityFeature", {"TURN_ON": 1, "TURN_OFF": 2}),
    "valve": ("ValveEntityFeature", {"OPEN": 1, "CLOSE": 2, "SET_POSITION": 4, "STOP": 8}),
    "vacuum": ("VacuumEntityFeature", {"PAUSE": 4, "RETURN_HOME": 16, "START": 8192}),
}
stubs = {}
for domain, (name, values) in _FEATURE_STUBS.items():
    module_name = f"homeassistant.components.{domain}.const"
    module = types.ModuleType(module_name)
    setattr(module, name, IntFlag(name, values))
    stubs[module_name] = module
sys.modules.update(stubs)
spec.loader.exec_module(actions)
ActionPolicy = actions.ActionPolicy


def state(eid, **attrs):
    return SimpleNamespace(entity_id=eid, state="on", attributes=attrs)


class States:
    def __init__(self, *states):
        self.items = {s.entity_id: s for s in states}

    def get(self, entity_id):
        return self.items.get(entity_id)

    def async_all(self):
        return list(self.items.values())


class Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.hass = SimpleNamespace(states=States(
            state("light.one"), state("light.two"), state("lock.front"),
            state("cover.door", device_class="garage", supported_features=1), state("cover.shade", device_class="shade", supported_features=1),
            state("group.room", entity_id=["light.one", "light.two"]),
            state("scene.opaque"), state("script.test"),
            state("scene.native", entity_id=["light.one", "lock.front"]),
        ), services=Services())
        self.overrides = {}
        self.policy = ActionPolicy(self.hass, self.overrides)
        self.policy._registry_entry = lambda eid: SimpleNamespace(platform="homeassistant") if eid == "scene.native" else None
        self.ctx = SimpleNamespace(user_id=None)

    def run_action(self, action, targets, params=None, **kw):
        return asyncio.run(self.policy.run(action, targets, {} if params is None else params, self.ctx, **kw))

    def test_capabilities_and_trace(self):
        rgb = state("light.rgb", supported_color_modes=["rgb"])
        temperature = state("light.temp", supported_color_modes=["color_temp"], min_color_temp_kelvin=2000, max_color_temp_kelvin=6500)
        fan = state("fan.test", supported_features=1)
        self.assertIn("rgb_color", actions._capability(rgb, "light_set"))
        self.assertNotIn("color_temp_kelvin", actions._capability(rgb, "light_set"))
        self.assertEqual(actions._capability(temperature, "light_set")["color_temp_kelvin"], {"type": "number", "minimum": 2000, "maximum": 6500})
        self.assertIsNotNone(actions._capability(fan, "fan_set_percentage"))
        self.assertIsNone(actions._capability(fan, "fan_oscillate"))
        trace = {}
        self.assertEqual(self.policy.catalog(for_retrieval=True), self.policy.catalog(for_retrieval=True, trace=trace))
        self.assertTrue(all(trace[key] >= 0 for key in ("catalog_state_ms", "catalog_registry_ms", "catalog_group_ms")))

    def test_retrieval_location_metadata(self):
        area = SimpleNamespace(id="kitchen", name="Kitchen", aliases={"Galley"})
        floor = SimpleNamespace(floor_id="ground", name="Ground", aliases={"Downstairs"})
        self.policy._area = lambda entity_id: area
        self.policy._location_metadata = lambda area, entry: (None, floor)
        entity = next(item for item in self.policy.catalog(for_retrieval=True) if item["id"] == "light.one")
        self.assertEqual((entity["area_id"], entity["area_aliases"], entity["floor_id"], entity["floor_aliases"]), ("kitchen", {"Galley"}, "ground", {"Downstairs"}))

    def test_device_inherited_area_supplies_floor(self):
        area = SimpleNamespace(id="kitchen", name="Kitchen", floor_id="ground", aliases={"Galley"})
        floor = SimpleNamespace(floor_id="ground", name="Ground", aliases={"Downstairs"})
        device = SimpleNamespace(area_id=None, name_by_user=None, name="Lamp")
        registry = SimpleNamespace(async_get=lambda device_id: device)
        helpers = types.ModuleType("homeassistant.helpers")
        helpers.device_registry = SimpleNamespace(async_get=lambda hass: registry, async_get_effective_area_id=lambda hass, item: "kitchen")
        helpers.area_registry = SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get_area=lambda area_id: area))
        helpers.floor_registry = SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get_floor=lambda floor_id: floor))
        self.policy._registry_entry = lambda entity_id: SimpleNamespace(area_id=None, device_id="child", platform="test", aliases=(), id=entity_id)
        with patch.dict(sys.modules, {"homeassistant.helpers": helpers}):
            entity = next(item for item in self.policy.catalog(for_retrieval=True) if item["id"] == "light.one")
        self.assertEqual((entity["area"], entity["floor"], entity["floor_aliases"]), ("Kitchen", "Ground", {"Downstairs"}))

    def test_dry_run_and_group_revalidation(self):
        self.overrides["light.two:turn_on"] = False
        group = next(entry for entry in self.policy.catalog() if entry["id"] == "group.room")
        self.assertTrue(next(action for action in group["actions"] if action["id"] == "turn_on")["enabled"])
        result = self.run_action("turn_on", ["group.room"], dry_run=True)
        self.assertEqual(result["planned"], ["light.one"])
        self.assertEqual(result["skipped"][0]["entity_id"], "light.two")
        self.assertEqual(self.hass.services.calls, [])
        result = self.run_action("turn_on", ["group.room"])
        self.assertEqual(result["executed"], ["light.one"])
        self.assertEqual(len(self.hass.services.calls), 1)

    def test_defaults_keep_common_controls_and_exclude_helpers(self):
        self.hass.states.items.update({item.entity_id: item for item in (
            state("switch.plug", friendly_name="Steckdose"),
            state("switch.phone_dnd_mode"),
            state("switch.phone", friendly_name="Phone Do Not Disturb"),
            state("switch.mobile", friendly_name="PhoneDnDMode"),
            state("switch.blend", friendly_name="Sandstone"),
            state("switch.config"),
            state("input_boolean.helper"),
        )})
        registry = self.policy._registry_entry
        self.policy._registry_entry = lambda eid: SimpleNamespace(platform="mqtt", entity_category="config") if eid == "switch.config" else registry(eid)
        defaults = {item["id"]: next(action["enabled"] for action in item["actions"] if action["id"] == "turn_on")
                    for item in self.policy.catalog() if item["id"].startswith(("switch.", "input_boolean."))}
        self.assertEqual({target for target, enabled in defaults.items() if enabled}, {"switch.plug", "switch.blend"})
        self.overrides["switch.phone:turn_on"] = True
        self.overrides["switch.plug:turn_on"] = False
        self.assertEqual(self.run_action("turn_on", ["switch.phone", "switch.plug"], dry_run=True)["planned"], ["switch.phone"])
        self.overrides.clear()
        self.assertEqual(self.run_action("turn_on", ["switch.phone", "switch.plug"], dry_run=True)["planned"], ["switch.plug"])

    def test_sensitive_and_live_override(self):
        self.assertEqual(self.run_action("unlock", ["lock.front"])["executed"], [])
        self.overrides["lock.front:unlock"] = True
        self.assertEqual(self.run_action("unlock", ["lock.front"])["executed"], ["lock.front"])
        self.assertEqual(self.run_action("open", ["cover.door"])["executed"], [])
        self.assertEqual(self.run_action("open", ["cover.shade"])["executed"], ["cover.shade"])

    def test_opaque_and_known_sensitive_scene(self):
        self.assertFalse(self.run_action("activate", ["scene.opaque", "script.test"])["executed"])
        self.overrides.update({"scene.opaque:activate": True, "scene.native:activate": True,
                               "script.test:activate": True})
        self.assertEqual(self.run_action("activate", ["scene.native"])["executed"], [])
        self.assertEqual(self.run_action("activate", ["scene.opaque", "script.test"])["executed"], ["scene.opaque", "script.test"])
        self.overrides.update({"lock.front:lock": True, "lock.front:unlock": True})
        self.assertEqual(self.run_action("activate", ["scene.native"])["executed"], ["scene.native"])

    def test_injection_and_context(self):
        self.assertTrue(self.run_action("light_set", ["light.one"], {"entity_id": "lock.front"})["errors"])
        self.assertTrue(self.run_action("light_set", ["light.one"], {"brightness": True})["errors"])
        self.ctx.user_id = "missing"
        self.assertTrue(self.run_action("turn_on", ["light.one"])["errors"])
        self.assertFalse(self.hass.services.calls)

    def test_permissions_and_catalog(self):
        user = SimpleNamespace(id="u", is_active=True, is_admin=False,
                               permissions=SimpleNamespace(check_entity=lambda eid, perm: eid != "light.two" and perm == "read"))
        self.ctx.user_id = "u"
        self.assertNotIn("light.two", [item["id"] for item in self.policy.catalog(user)])
        self.assertFalse(self.run_action("turn_on", ["light.one"], user=user)["executed"])

    def test_cycles_and_unknown_groups_fail_closed(self):
        self.hass.states.items["group.loop"] = state("group.loop", entity_id=["group.loop"])
        self.hass.states.items["group.missing"] = state("group.missing", entity_id=["light.missing"])
        self.assertEqual(len(self.run_action("turn_on", ["group.loop", "group.missing"])["errors"]), 2)
        self.assertFalse(self.hass.services.calls)


if __name__ == "__main__":
    unittest.main()
