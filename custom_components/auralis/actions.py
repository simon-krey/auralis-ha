"""Scoped Auralis actions for Home Assistant.

Overrides are a live mapping of ``f'{entity_id}:{action_id}'`` to bool.
Only an administrator should be allowed to edit this mapping.
The caller must resolve context.user_id and pass the matching HA user to run;
None is reserved for HA's userless contexts (for example voice satellites).
"""

from collections.abc import Mapping
import math
import re

_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
MAX_ACTION_TARGETS = 100

# Parameters are deliberately service-specific: target, area and service names
# never come from model-supplied arguments.
def _number(low, high):
    return {"type": "number", "minimum": low, "maximum": high}


def _enum(*values):
    return {"type": "string", "enum": list(values)}


def _string():
    return {"type": "string", "minLength": 1, "maxLength": 128}


LIGHT = {"brightness_pct": _number(0, 100), "brightness": _number(0, 255),
         "color_temp_kelvin": _number(1000, 40000), "rgb_color": {"type": "array", "items": _number(0, 255), "minItems": 3, "maxItems": 3},
         "hs_color": {"type": "array", "items": _number(0, 360), "minItems": 2, "maxItems": 2}}
# action id -> (label, supported domains, service, parameter schema)
_ACTIONS = {
    "turn_on": ("Turn on", {"light", "switch", "fan", "humidifier", "input_boolean", "siren"}, "turn_on", {}),
    "turn_off": ("Turn off", {"light", "switch", "fan", "humidifier", "input_boolean", "siren"}, "turn_off", {}),
    "light_set": ("Set light", {"light"}, "turn_on", LIGHT),
    "activate": ("Activate", {"scene", "script"}, "turn_on", {}),
    "fan_set_percentage": ("Set fan speed", {"fan"}, "set_percentage", {"percentage": _number(0, 100)}),
    "fan_oscillate": ("Set oscillation", {"fan"}, "oscillate", {"oscillating": {"type": "boolean"}}),
    "open": ("Open", {"cover", "valve"}, "open", {}),
    "close": ("Close", {"cover", "valve"}, "close", {}),
    "stop": ("Stop", {"cover", "valve"}, "stop", {}),
    "set_position": ("Set position", {"cover", "valve"}, "set_position", {"position": _number(0, 100)}),
    "lock": ("Lock", {"lock"}, "lock", {}),
    "unlock": ("Unlock", {"lock"}, "unlock", {}),
    "alarm_arm_away": ("Arm away", {"alarm_control_panel"}, "alarm_arm_away", {}),
    "alarm_arm_home": ("Arm home", {"alarm_control_panel"}, "alarm_arm_home", {}),
    "alarm_disarm": ("Disarm", {"alarm_control_panel"}, "alarm_disarm", {}),
    "climate_temperature": ("Set temperature", {"climate"}, "set_temperature", {"temperature": _number(-100, 200)}),
    "climate_hvac_mode": ("Set HVAC mode", {"climate"}, "set_hvac_mode", {"hvac_mode": _enum("off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only")}),
    "media_play": ("Play", {"media_player"}, "media_play", {}),
    "media_pause": ("Pause", {"media_player"}, "media_pause", {}),
    "media_stop": ("Stop playback", {"media_player"}, "media_stop", {}),
    "media_next_track": ("Next track", {"media_player"}, "media_next_track", {}),
    "media_previous_track": ("Previous track", {"media_player"}, "media_previous_track", {}),
    "volume_set": ("Set volume", {"media_player"}, "volume_set", {"volume_level": _number(0, 1)}),
    "vacuum_start": ("Start vacuum", {"vacuum"}, "start", {}),
    "vacuum_pause": ("Pause vacuum", {"vacuum"}, "pause", {}),
    "vacuum_return_to_base": ("Return vacuum to base", {"vacuum"}, "return_to_base", {}),
    "press": ("Press button", {"button", "input_button"}, "press", {}),
    "select_option": ("Select option", {"select", "input_select"}, "select_option", {"option": _string()}),
    "set_value": ("Set value", {"number", "input_number"}, "set_value", {"value": _number(-1e9, 1e9)}),
}


def _valid_value(value, schema):
    kind = schema["type"]
    if kind == "number":
        return type(value) in (int, float) and math.isfinite(value) and schema["minimum"] <= value <= schema["maximum"]
    if kind == "boolean":
        return type(value) is bool
    if kind == "string":
        return isinstance(value, str) and (value in schema["enum"] if "enum" in schema else schema["minLength"] <= len(value) <= schema["maxLength"])
    return isinstance(value, list) and schema["minItems"] <= len(value) <= schema["maxItems"] and all(_valid_value(item, schema["items"]) for item in value)


def _permission(user, entity_id, permission):
    if user is None:
        return True
    try:
        return bool(user.is_admin or user.permissions.check_entity(entity_id, permission))
    except (AttributeError, TypeError):
        return False


class ActionPolicy:
    def __init__(self, hass, overrides):
        self.hass = hass
        self.overrides = overrides

    def _registry_entry(self, entity_id):
        try:
            from homeassistant.helpers import entity_registry
            return entity_registry.async_get(self.hass).async_get(entity_id)
        except (ImportError, AttributeError, KeyError):
            return None

    def _area(self, entity_id):
        try:
            from homeassistant.helpers import area_registry, device_registry
            entry = self._registry_entry(entity_id)
            if not entry:
                return None
            area_id = entry.area_id
            if not area_id and entry.device_id:
                device = device_registry.async_get(self.hass).async_get(entry.device_id)
                area_id = device.area_id if device else None
            area = area_registry.async_get(self.hass).async_get_area(area_id) if area_id else None
            return area
        except (ImportError, AttributeError, KeyError):
            return None

    def _group(self, state):
        entry = self._registry_entry(state.entity_id)
        return state.entity_id.startswith("group.") or bool(entry and entry.platform == "group")

    def _leaves(self, entity_id, seen=None, user=None, action=None):
        seen = set() if seen is None else seen
        if entity_id in seen or len(seen) >= 256:
            raise ValueError("cyclic or oversized group")
        state = self.hass.states.get(entity_id)
        if state is None:
            raise ValueError("unknown entity")
        if not self._group(state):
            return [entity_id]
        if not _permission(user, entity_id, "read") or not _permission(user, entity_id, "control") or (action and not self._enabled(state, action)[0]):
            raise ValueError("group permission denied")
        members = state.attributes.get("entity_id")
        if not isinstance(members, (list, tuple)) or not members or any(not isinstance(x, str) or not _ENTITY.fullmatch(x) for x in members):
            raise ValueError("unknown group members")
        return [leaf for member in members for leaf in self._leaves(member, seen | {entity_id}, user, action)]

    def _sensitive(self, state):
        domain = state.entity_id.split(".", 1)[0]
        return domain in {"lock", "alarm_control_panel", "script", "siren", "button", "input_button"} or (domain == "cover" and state.attributes.get("device_class") not in {"blind", "curtain", "shade", "shutter", "awning"})

    def _enabled(self, state, action):
        default = not self._sensitive(state)
        if state.entity_id.startswith("scene."):
            default = self._scene_safe(state, None, defaults_only=True)
        value = self.overrides.get(f"{state.entity_id}:{action}", default)
        return value is True, default

    def _scene_safe(self, state, user, *, require_native=True, defaults_only=False):
        # Only HA's native scene implementation proves the attribute is complete.
        entry = self._registry_entry(state.entity_id)
        members = state.attributes.get("entity_id")
        if (require_native and (not entry or entry.platform != "homeassistant")) or not isinstance(members, (list, tuple)) or not members:
            return False
        try:
            leaves = [leaf for member in members for leaf in self._leaves(member, user=user)]
            for leaf in leaves:
                target = self.hass.states.get(leaf)
                domain = leaf.split(".", 1)[0]
                actions = [name for name, spec in _ACTIONS.items() if domain in spec[1]]
                if domain in {"scene", "script"} or not actions or not _permission(user, leaf, "read") or not _permission(user, leaf, "control"):
                    return False
                if any(not self._enabled(target, name)[1 if defaults_only else 0] for name in actions):
                    return False
            return True
        except (ValueError, TypeError, AttributeError, RecursionError):
            return False

    def _scope_ok(self, state, user):
        domain = state.entity_id.split(".", 1)[0]
        if domain not in {"scene", "script"}:
            return True
        if domain == "scene" and self._scene_safe(state, user):
            return True
        # Exposed members remain binding even when the scene may have more.
        if domain == "scene" and "entity_id" in state.attributes and not self._scene_safe(state, user, require_native=False):
            return False
        return self.overrides.get(f"{state.entity_id}:activate") is True and (user is None or bool(getattr(user, "is_admin", False)))

    def catalog(self, user=None, *, for_retrieval=False):
        result = []
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if not _permission(user, entity_id, "read"):
                continue
            domain = entity_id.split(".", 1)[0]
            actions = []
            for action, (label, domains, _, params) in _ACTIONS.items():
                if domain not in domains:
                    if not self._group(state):
                        continue
                    try:
                        if not any(x.split(".", 1)[0] in domains for x in self._leaves(entity_id, user=user)):
                            continue
                    except ValueError:
                        continue
                enabled, default = self._enabled(state, action)
                if self._group(state):
                    try:
                        leaves = self._leaves(entity_id, user=user, action=action)
                        enabled = enabled and any(_permission(user, x, "read") and _permission(user, x, "control") and self._enabled(self.hass.states.get(x), action)[0] and x.split(".", 1)[0] in domains for x in leaves)
                    except ValueError:
                        enabled = False
                enabled = enabled and _permission(user, entity_id, "control") and self._scope_ok(state, user)
                actions.append({"id": action, "label": label, "enabled": enabled, "configured": self._enabled(state, action)[0], "default_enabled": default, "sensitive": self._sensitive(state) or (domain == "scene" and not default), "parameters": {"type": "object", "properties": params, "required": list(params) if action != "light_set" else [], "additionalProperties": False}})
            if actions or domain in {"binary_sensor", "sensor"}:
                area = self._area(entity_id)
                entity = {"id": entity_id, "name": str(state.attributes.get("friendly_name", entity_id)), "area": area.name if area else None, "state": str(state.state), "actions": actions}
                if for_retrieval:
                    entry = self._registry_entry(entity_id)
                    entity.update(aliases=[alias for alias in getattr(entry, "aliases", ()) if isinstance(alias, str)], area_aliases=getattr(area, "aliases", ()), area_id=area.id if area else None, group=self._group(state), device_class=state.attributes.get("device_class"))
                result.append(entity)
        return result

    async def run(self, action, entity_ids, parameters, context, *, dry_run=False, user=None):
        result = {"executed": [], "planned": [], "skipped": [], "errors": []}
        def error(target, reason):
            result["errors"].append({"entity_id": target, "reason": reason})
        context_user = getattr(context, "user_id", None)
        if (context_user is None and user is not None) or (context_user is not None and (user is None or getattr(user, "id", None) != context_user or not getattr(user, "is_active", False))):
            error(None, "context user mismatch or unresolved")
            return result
        if not isinstance(action, str) or action not in _ACTIONS:
            error(None, "unknown action")
            return result
        if not isinstance(entity_ids, list) or not entity_ids or len(entity_ids) > MAX_ACTION_TARGETS or any(not isinstance(x, str) or not _ENTITY.fullmatch(x) for x in entity_ids):
            error(None, "invalid entity_ids")
            return result
        _, domains, service, schema = _ACTIONS[action]
        if not isinstance(parameters, Mapping) or any(k not in schema or not _valid_value(v, schema[k]) for k, v in parameters.items()) or (action != "light_set" and set(parameters) != set(schema)) or (action == "light_set" and not parameters):
            error(None, "invalid parameters")
            return result
        if "hs_color" in parameters and parameters["hs_color"][1] > 100:
            error(None, "invalid parameters")
            return result
        visited = set()
        for entity_id in dict.fromkeys(entity_ids):
            state = self.hass.states.get(entity_id)
            if state is None:
                error(entity_id, "unknown entity")
                continue
            if not _permission(user, entity_id, "read") or not _permission(user, entity_id, "control"):
                result["skipped"].append({"entity_id": entity_id, "reason": "permission denied"})
                continue
            if self._group(state) and not self._enabled(state, action)[0]:
                result["skipped"].append({"entity_id": entity_id, "reason": "action disabled"})
                continue
            try:
                leaves = self._leaves(entity_id, user=user, action=action)
            except ValueError:
                error(entity_id, "invalid group")
                continue
            for leaf in leaves:
                if leaf in visited:
                    continue
                visited.add(leaf)
                target = self.hass.states.get(leaf)
                domain = leaf.split(".", 1)[0]
                if domain not in domains:
                    result["skipped"].append({"entity_id": leaf, "reason": "unsupported action"})
                elif not _permission(user, leaf, "read") or not _permission(user, leaf, "control"):
                    result["skipped"].append({"entity_id": leaf, "reason": "permission denied"})
                elif not self._enabled(target, action)[0] or not self._scope_ok(target, user):
                    result["skipped"].append({"entity_id": leaf, "reason": "action disabled"})
                elif dry_run:
                    result["planned"].append(leaf)
                else:
                    try:
                        actual_service = {"open": f"open_{domain}", "close": f"close_{domain}", "stop": f"stop_{domain}", "set_position": f"set_{domain}_position"}.get(action, service)
                        await self.hass.services.async_call(domain, actual_service, {"entity_id": leaf, **parameters}, blocking=True, context=context)
                        result["executed"].append(leaf)
                    except Exception as exception:
                        error(leaf, "service call failed")
                        if isinstance(exception, ValueError) or type(exception).__module__.startswith("homeassistant."):
                            result["errors"][-1]["detail"] = f"{type(exception).__name__}: {exception}"
                        else:
                            result["errors"][-1]["detail"] = type(exception).__name__
        return result
