"""Scoped Auralis actions for Home Assistant.

Overrides are a live mapping of ``f'{entity_id}:{action_id}'`` to bool.
Only an administrator should be allowed to edit this mapping.
The caller must resolve context.user_id and pass the matching HA user to run;
None is reserved for HA's userless contexts (for example voice satellites).
"""

from collections.abc import Mapping
import math
from functools import cache
import re
from time import perf_counter


_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_DEFAULT_DOMAINS = {"light", "switch", "climate", "fan", "humidifier", "cover", "group", "scene"}
_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DND = re.compile(r"(?<![^\W_])(?:d[\W_]*n[\W_]*d|do[\W_]*not[\W_]*disturb)(?![^\W_])", re.I)
MAX_ACTION_TARGETS = 100
MAX_GROUP_NODES = 256

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

_DOMAIN_ACTIONS = {domain: tuple((action, spec) for action, spec in _ACTIONS.items() if domain in spec[1])
                   for domain in {name for _, domains, _, _ in _ACTIONS.values() for name in domains}}


@cache
def _features():
    from homeassistant.components.alarm_control_panel.const import AlarmControlPanelEntityFeature
    from homeassistant.components.climate.const import ClimateEntityFeature
    from homeassistant.components.cover.const import CoverEntityFeature
    from homeassistant.components.fan.const import FanEntityFeature
    from homeassistant.components.media_player.const import MediaPlayerEntityFeature
    from homeassistant.components.siren.const import SirenEntityFeature
    from homeassistant.components.valve.const import ValveEntityFeature
    from homeassistant.components.vacuum.const import VacuumEntityFeature

    return {
        "fan": {"turn_on": FanEntityFeature.TURN_ON, "turn_off": FanEntityFeature.TURN_OFF, "fan_set_percentage": FanEntityFeature.SET_SPEED, "fan_oscillate": FanEntityFeature.OSCILLATE},
        "cover": {"open": CoverEntityFeature.OPEN, "close": CoverEntityFeature.CLOSE, "set_position": CoverEntityFeature.SET_POSITION, "stop": CoverEntityFeature.STOP},
        "valve": {"open": ValveEntityFeature.OPEN, "close": ValveEntityFeature.CLOSE, "set_position": ValveEntityFeature.SET_POSITION, "stop": ValveEntityFeature.STOP},
        "climate": {"climate_temperature": ClimateEntityFeature.TARGET_TEMPERATURE},
        "media_player": {"media_play": MediaPlayerEntityFeature.PLAY, "media_pause": MediaPlayerEntityFeature.PAUSE, "media_stop": MediaPlayerEntityFeature.STOP,
                         "media_next_track": MediaPlayerEntityFeature.NEXT_TRACK, "media_previous_track": MediaPlayerEntityFeature.PREVIOUS_TRACK, "volume_set": MediaPlayerEntityFeature.VOLUME_SET},
        "vacuum": {"vacuum_start": VacuumEntityFeature.START, "vacuum_pause": VacuumEntityFeature.PAUSE, "vacuum_return_to_base": VacuumEntityFeature.RETURN_HOME},
        "alarm_control_panel": {"alarm_arm_home": AlarmControlPanelEntityFeature.ARM_HOME, "alarm_arm_away": AlarmControlPanelEntityFeature.ARM_AWAY},
        "siren": {"turn_on": SirenEntityFeature.TURN_ON, "turn_off": SirenEntityFeature.TURN_OFF},
    }

_COLOR = {"hs", "xy", "rgb", "rgbw", "rgbww"}


def _values(attributes, key):
    values = attributes.get(key)
    if isinstance(values, (set, frozenset)):
        return sorted(value for value in values if isinstance(value, str) and value)
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value)) if isinstance(values, (list, tuple, set, frozenset)) else []


def _range(attributes, low_key, high_key, schema):
    low, high = attributes.get(low_key), attributes.get(high_key)
    if _valid_value(low, schema) and _valid_value(high, schema) and low <= high:
        return _number(low, high)
    return None


def _capability(state, action):
    _, domains, _, schema = _ACTIONS[action]
    domain = state.entity_id.partition(".")[0]
    if domain not in domains or state.state in {"unavailable", "unknown"}:
        return None
    attrs = state.attributes
    feature = _features().get(domain, {}).get(action) if domain in {"fan", "cover", "valve", "climate", "media_player", "vacuum", "alarm_control_panel", "siren"} else None
    if feature and (not isinstance(attrs.get("supported_features"), int) or isinstance(attrs["supported_features"], bool) or not attrs["supported_features"] & feature):
        return None
    if action == "light_set":
        modes = set(_values(attrs, "supported_color_modes"))
        result = {}
        if modes & (_COLOR | {"brightness", "color_temp", "white"}):
            result.update({key: schema[key] for key in ("brightness", "brightness_pct")})
        if modes & _COLOR:
            result.update({key: schema[key] for key in ("rgb_color", "hs_color")})
        if "color_temp" in modes:
            limits = _range(attrs, "min_color_temp_kelvin", "max_color_temp_kelvin", schema["color_temp_kelvin"])
            if limits:
                result["color_temp_kelvin"] = limits
        return result or None
    for name, key in (("climate_hvac_mode", "hvac_mode"), ("select_option", "option")):
        if action == name:
            options = _values(attrs, "hvac_modes" if name == "climate_hvac_mode" else "options")
            return {key: _enum(*options)} if options else None
    for name, key, low, high in (("climate_temperature", "temperature", "min_temp", "max_temp"), ("set_value", "value", "min", "max")):
        if action == name:
            limits = _range(attrs, low, high, schema[key])
            return {key: limits} if limits else None
    return schema


def shared_parameters(schemas):
    common = dict(schemas[0])
    for schema in schemas[1:]:
        for name, spec in list(common.items()):
            other = schema.get(name)
            if spec == other:
                continue
            if other and spec["type"] == other["type"] == "number":
                low, high = max(spec["minimum"], other["minimum"]), min(spec["maximum"], other["maximum"])
                if low <= high:
                    common[name] = _number(low, high)
                    continue
            if other and "enum" in spec and "enum" in other:
                values = [value for value in spec["enum"] if value in other["enum"]]
                if values:
                    common[name] = _enum(*values)
                    continue
            del common[name]
    return common


def _valid_value(value, schema):
    kind = schema["type"]
    if kind == "number":
        return type(value) in (int, float) and schema["minimum"] <= value <= schema["maximum"] and math.isfinite(value)
    if kind == "boolean":
        return type(value) is bool
    if kind == "string":
        return isinstance(value, str) and (value in schema["enum"] if "enum" in schema else schema["minLength"] <= len(value) <= schema["maxLength"])
    return isinstance(value, list) and schema["minItems"] <= len(value) <= schema["maxItems"] and all(_valid_value(item, schema["items"]) for item in value)


def valid_parameters(action, parameters, schema):
    if not isinstance(parameters, Mapping) or any(key not in schema or not _valid_value(value, schema[key]) for key, value in parameters.items()):
        return False
    if action != "light_set":
        return parameters.keys() == schema.keys()
    return bool(parameters) and len(parameters.keys() & {"brightness", "brightness_pct"}) <= 1 and len(parameters.keys() & {"rgb_color", "hs_color", "color_temp_kelvin"}) <= 1 and ("hs_color" not in parameters or parameters["hs_color"][1] <= 100)


def power_permission(action, parameters):
    if action == "light_set":
        return "turn_off" if parameters.get("brightness_pct", parameters.get("brightness")) == 0 else "turn_on"
    if action == "fan_set_percentage":
        return "turn_off" if parameters["percentage"] == 0 else "turn_on"
    return None


def _service(action, entity_id):
    domain = entity_id.partition(".")[0]
    if action in {"open", "close", "stop"}:
        return domain, f"{action}_{domain}"
    if action == "set_position":
        return domain, f"set_{domain}_position"
    return domain, _ACTIONS[action][2]


def _permission(user, entity_id, permission):
    if user is None:
        return True
    try:
        return bool(user.is_active and user.permissions.check_entity(entity_id, permission))
    except (AttributeError, TypeError):
        return False


class ActionPolicy:
    def __init__(self, hass, overrides):
        self.hass = hass
        self.overrides = overrides

    def _exposed(self, entity_id):
        from homeassistant.components.homeassistant.exposed_entities import async_should_expose

        return not getattr(self._registry_entry(entity_id), "disabled_by", None) and async_should_expose(self.hass, "conversation", entity_id) is True

    def _can_control(self, state, user):
        return (self._exposed(state.entity_id) and _permission(user, state.entity_id, "control")
                and (user is not None or not self._sensitive(state) and not state.entity_id.startswith("scene.")))

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
                area_id = device_registry.async_get_effective_area_id(self.hass, device) if device else None
            area = area_registry.async_get(self.hass).async_get_area(area_id) if area_id else None
            return area
        except (ImportError, AttributeError, KeyError):
            return None

    def _group(self, state):
        entry = self._registry_entry(state.entity_id)
        return state.entity_id.startswith("group.") or bool(entry and entry.platform == "group")

    def voice_location(self, device_id, satellite_id, fallback):
        result = {"device_id": device_id, "satellite_id": satellite_id, "area_id": None, "source": "unknown", "verified": False}
        try:
            from homeassistant.helpers import area_registry, device_registry
            devices = device_registry.async_get(self.hass)
            areas = area_registry.async_get(self.hass)
        except (ImportError, AttributeError, KeyError):
            return result
        satellite = self._registry_entry(satellite_id) if satellite_id and satellite_id.startswith("assist_satellite.") else None
        if satellite_id and (satellite is None or getattr(satellite, "disabled_by", None)):
            return result
        satellite_device = satellite.device_id if satellite else None
        if device_id and satellite_device and device_id != satellite_device:
            return result | {"source": "conflict"}
        effective_device = satellite_device or device_id
        device = devices.async_get(effective_device) if effective_device else None
        if (effective_device and device is None) or not (satellite or device):
            return result
        result.update(device_id=effective_device, verified=True)
        device_area = device_registry.async_get_effective_area_id(self.hass, device) if device else None
        satellite_area = satellite.area_id if satellite else None
        if satellite_area and device_area and satellite_area != device_area:
            return result | {"source": "conflict"}
        area_id = satellite_area or device_area
        source = "satellite_entity" if satellite_area else ("device" if device and device.area_id else "parent_device")
        if area_id is None:
            manual = {fallback[key] for key in (satellite_id, effective_device) if key in fallback}
            if len(manual) > 1:
                return result | {"source": "conflict"}
            area_id, source = next(iter(manual), None), "manual"
        if area_id and (area := areas.async_get_area(area_id)):
            result.update(area_id=area.id, area=area.name, source=source)
        return result

    def in_area(self, entity_id, area_id):
        return getattr(self._area(entity_id), "id", None) == area_id

    def identity(self, entity_id):
        return getattr(self._registry_entry(entity_id), "id", None)

    def room_group_ok(self, entity_id, area_id):
        try:
            return all(self.in_area(leaf, area_id) for leaf in self._leaves(entity_id, enforce_exposure=False))
        except ValueError:
            return False

    def _leaves(self, entity_id, *, user=None, action=None, enforce_exposure=True):
        visited, expanding, leaves = set(), set(), []

        def walk(target):
            if target in expanding:
                raise ValueError("cyclic group")
            if target in visited:
                return
            if len(visited) >= MAX_GROUP_NODES:
                raise ValueError("oversized group")
            visited.add(target)
            state = self.hass.states.get(target)
            if state is None:
                raise ValueError("unknown entity")
            if not self._group(state):
                leaves.append(target)
                return
            if (state.state in {"unavailable", "unknown"} or not _permission(user, target, "read") or not _permission(user, target, "control")
                    or enforce_exposure and not self._can_control(state, user) or action and not self._enabled(state, action)[0]):
                raise ValueError("group unavailable or permission denied")
            members = state.attributes.get("entity_id")
            if not isinstance(members, (list, tuple)) or not members or len(members) > MAX_GROUP_NODES or any(not isinstance(member, str) or not _ENTITY.fullmatch(member) for member in members):
                raise ValueError("unknown group members")
            expanding.add(target)
            for member in members:
                walk(member)
            expanding.remove(target)

        walk(entity_id)
        return leaves

    def _location_metadata(self, area, entry):
        device_name = None
        floor = None
        try:
            from homeassistant.helpers import device_registry, floor_registry
            if entry and entry.device_id:
                device = device_registry.async_get(self.hass).async_get(entry.device_id)
                if device:
                    device_name = device.name_by_user or device.name
            if area and area.floor_id:
                floor = floor_registry.async_get(self.hass).async_get_floor(area.floor_id)
        except (ImportError, AttributeError, KeyError):
            pass
        return device_name, floor

    def _sensitive(self, state):
        domain = state.entity_id.split(".", 1)[0]
        return domain in {"lock", "alarm_control_panel", "script", "siren", "button", "input_button"} or (domain == "cover" and state.attributes.get("device_class") not in {"blind", "curtain", "shade", "shutter", "awning"})

    def _default_enabled(self, state):
        default = state.entity_id.partition(".")[0] in _DEFAULT_DOMAINS and not self._sensitive(state)
        if default:
            entry = self._registry_entry(state.entity_id)
            names = state.entity_id, str(state.attributes.get("friendly_name", "")), str(getattr(entry, "name", ""))
            default = not getattr(entry, "entity_category", None) and not any(
                _DND.search(name) or _DND.search(_WORD_BOUNDARY.sub(" ", name)) for name in names
            )
        if default and state.entity_id.startswith("scene."):
            default = self._scene_safe(state, None, defaults_only=True)
        return default

    def _enabled(self, state, action, *, default=None):
        if default is None:
            default = self._default_enabled(state)
        value = self.overrides.get(f"{state.entity_id}:{action}", default)
        return value is True, default

    def _scene_safe(self, state, user, *, require_native=True, defaults_only=False):
        # Only HA's native scene implementation proves the attribute is complete.
        entry = self._registry_entry(state.entity_id)
        members = state.attributes.get("entity_id")
        if (require_native and (not entry or entry.platform != "homeassistant")) or not isinstance(members, (list, tuple)) or not members or len(members) > MAX_GROUP_NODES:
            return False
        try:
            leaves = [leaf for member in members for leaf in self._leaves(member, user=user, enforce_exposure=not defaults_only)]
            for leaf in leaves:
                target = self.hass.states.get(leaf)
                domain = leaf.split(".", 1)[0]
                actions = [name for name, spec in _ACTIONS.items() if domain in spec[1]]
                if target.state in {"unavailable", "unknown"} or domain in {"scene", "script"} or not actions or not _permission(user, leaf, "read") or not _permission(user, leaf, "control"):
                    return False
                if not defaults_only and not self._can_control(target, user):
                    return False
                if any(not self._enabled(target, name)[1 if defaults_only else 0] for name in actions):
                    return False
            return True
        except (ValueError, TypeError, AttributeError, RecursionError):
            return False

    def scene_scope(self, entity_id):
        state = self.hass.states.get(entity_id)
        members = state.attributes.get("entity_id") if state else None
        if not isinstance(members, (list, tuple)) or not members or len(members) > MAX_GROUP_NODES:
            return None
        try:
            return tuple(sorted({leaf for member in members for leaf in self._leaves(member, enforce_exposure=False)}))
        except (ValueError, TypeError, RecursionError):
            return None

    def _scope_ok(self, state, user):
        domain = state.entity_id.split(".", 1)[0]
        if domain not in {"scene", "script"}:
            return True
        if domain == "scene" and self._scene_safe(state, user):
            return True
        # Exposed members remain binding even when the scene may have more.
        if domain == "scene" and "entity_id" in state.attributes and not self._scene_safe(state, user, require_native=False):
            return False
        return self.overrides.get(f"{state.entity_id}:activate") is True and bool(getattr(user, "is_admin", False))

    def catalog(self, user=None, *, for_retrieval=False, trace=None, area_id=None):
        def timed(stage, function, *args, **kwargs):
            if trace is None:
                return function(*args, **kwargs)
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                key = f"catalog_{stage}_ms"
                trace[key] = trace.get(key, 0.0) + (perf_counter() - started) * 1000

        result = []
        for state in timed("state", self.hass.states.async_all):
            entity_id = state.entity_id
            area = timed("registry", self._area, entity_id) if area_id else None
            if area_id and getattr(area, "id", None) != area_id:
                continue
            if not _permission(user, entity_id, "read"):
                continue
            exposed = timed("registry", self._exposed, entity_id)
            domain = entity_id.split(".", 1)[0]
            can_control = _permission(user, entity_id, "control")
            sensitive = self._sensitive(state)
            anonymous_sensitive = user is None and (sensitive or domain == "scene")
            actions = []
            group = timed("group", self._group, state)
            domain_actions = _ACTIONS.items() if group else _DOMAIN_ACTIONS.get(domain, ())
            default = self._default_enabled(state) if domain_actions else False
            for action, (label, domains, _, params) in domain_actions:
                if domain not in domains:
                    if not group:
                        continue
                    try:
                        if not any(x.split(".", 1)[0] in domains for x in timed("group", self._leaves, entity_id, user=user, enforce_exposure=for_retrieval)):
                            continue
                    except ValueError:
                        continue
                configured, _ = self._enabled(state, action, default=default)
                enabled = configured
                members = None
                supported = timed("capability", _capability, state, action)
                if group:
                    try:
                        members = sorted(set(timed("group", self._leaves, entity_id, user=user, action=action, enforce_exposure=for_retrieval)))
                        targets = [self.hass.states.get(member) for member in members]
                        capabilities = [timed("capability", _capability, target, action) for target in targets]
                        supported = shared_parameters(capabilities) if all(cap is not None for cap in capabilities) else None
                        if params and not supported:
                            supported = None
                        authorized = [_permission(user, target.entity_id, "read") and (self._can_control(target, user) if for_retrieval else _permission(user, target.entity_id, "control")) and self._enabled(target, action)[0] and self._scope_ok(target, user) and capability is not None for target, capability in zip(targets, capabilities)]
                        enabled = enabled and (all(authorized) if for_retrieval else any(authorized))
                    except ValueError:
                        enabled = False
                enabled = enabled and (supported is not None or group and not for_retrieval) and can_control and self._scope_ok(state, user)
                if for_retrieval and (not exposed or anonymous_sensitive):
                    enabled = False
                schema = supported if supported is not None else {}
                item = {"id": action, "label": label, "enabled": enabled, "supported": supported is not None, "configured": configured, "default_enabled": default, "sensitive": sensitive or (domain == "scene" and not default), "parameters": {"type": "object", "properties": schema, "required": list(schema) if action != "light_set" else [], "additionalProperties": False}}
                if for_retrieval and not enabled:
                    item["disabled_reason"] = ("assist_not_exposed" if not exposed else "user_control_denied" if not can_control
                                               else "anonymous_sensitive" if anonymous_sensitive else "unsupported" if supported is None else "policy")
                if for_retrieval and enabled and members:
                    item["members"] = members
                    item["sensitive"] |= any(self._sensitive(target) for target in targets)
                actions.append(item)
            if actions or domain in {"binary_sensor", "sensor"}:
                if area is None:
                    area = timed("registry", self._area, entity_id)
                entity = {"id": entity_id, "name": str(state.attributes.get("friendly_name", entity_id)), "area": area.name if area else None, "state": str(state.state), "actions": actions, "exposed": exposed}
                if for_retrieval:
                    entry = timed("registry", self._registry_entry, entity_id)
                    device_name, floor = timed("registry", self._location_metadata, area, entry)
                    entity.update(aliases=[alias for alias in getattr(entry, "aliases", ()) if isinstance(alias, str)], area_aliases=getattr(area, "aliases", ()), area_id=area.id if area else None, registry_id=getattr(entry, "id", None), group=group, device_class=state.attributes.get("device_class"))
                    if domain == "scene" and any(item["enabled"] for item in actions):
                        entity["scene_scope"] = self.scene_scope(entity_id)
                    live_values = {key: state.attributes[key] for key in ("brightness", "volume_level", "temperature", "percentage_step") if key in state.attributes}
                    if live_values:
                        entity["live_values"] = live_values
                    if isinstance(device_name, str) and device_name:
                        entity.update(device_name=device_name, device_id=getattr(entry, "device_id", None))
                    if floor:
                        entity.update(floor=floor.name, floor_id=floor.floor_id, floor_aliases=floor.aliases)
                result.append(entity)
        return result

    def basis_matches(self, entity_id, basis):
        current = self.hass.states.get(entity_id)
        if current is None:
            return False
        value = current.attributes.get(basis[0])
        return type(value) in (int, float) and value == basis[1] and (len(basis) == 2 or current.state == basis[2])

    def _check_leaf(self, entity_id, action, parameters, user):
        target = self.hass.states.get(entity_id)
        if target is None:
            return "errors", "unknown entity"
        if not _permission(user, entity_id, "read") or not self._can_control(target, user):
            return "skipped", "permission denied"
        capability = _capability(target, action)
        if capability is None or self._group(target):
            return "skipped", "unsupported action"
        if not self._enabled(target, action)[0] or not self._scope_ok(target, user):
            return "skipped", "action disabled"
        if not valid_parameters(action, parameters, capability):
            return "errors", "invalid parameters for current capabilities"
        if power := power_permission(action, parameters):
            if not self._enabled(target, power)[0]:
                return "skipped", "power action disabled"
        if (has_service := getattr(self.hass.services, "has_service", None)) and not has_service(*_service(action, entity_id)):
            return "errors", "service not registered"
        return None

    def preflight(self, action, entity_ids, parameters, context, *, user=None, expected_leaves=None):
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
        _, _, _, schema = _ACTIONS[action]
        if action not in {"select_option", "climate_hvac_mode"} and not valid_parameters(action, parameters, schema):
            error(None, "invalid parameters")
            return result
        if not isinstance(parameters, Mapping):
            error(None, "invalid parameters")
            return result
        visited = set()
        for entity_id in dict.fromkeys(entity_ids):
            state = self.hass.states.get(entity_id)
            if state is None:
                error(entity_id, "unknown entity")
                continue
            if not _permission(user, entity_id, "read") or not self._can_control(state, user):
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
                failure = self._check_leaf(leaf, action, parameters, user)
                if failure:
                    category, reason = failure
                    result[category].append({"entity_id": leaf, "reason": reason})
                else:
                    result["planned"].append(leaf)
        if expected_leaves is not None and set(result["planned"]) != set(expected_leaves):
            error(None, "target set changed or no longer authorized")
        return result

    async def run(self, action, entity_ids, parameters, context, *, dry_run=False, user=None, expected_leaves=None, guard=None):
        result = self.preflight(action, entity_ids, parameters, context, user=user, expected_leaves=expected_leaves)
        if expected_leaves is not None and (result["errors"] or result["skipped"]):
            result["planned"] = []
            return result
        if dry_run:
            return result
        targets, result["planned"] = result["planned"], []
        contains_groups = expected_leaves is not None and set(entity_ids) != set(expected_leaves)
        for index, leaf in enumerate(targets):
            try:
                if guard:
                    user, reason = await guard(leaf, entity_ids if contains_groups else ())
                    if reason:
                        result["skipped"].extend({"entity_id": target, "reason": reason} for target in targets[index:])
                        break
                if contains_groups:
                    live = self.preflight(action, entity_ids, parameters, context, user=user, expected_leaves=expected_leaves)
                    if live["errors"] or live["skipped"]:
                        result["errors"].extend(live["errors"])
                        result["skipped"].extend(live["skipped"])
                        result["skipped"].extend({"entity_id": target, "reason": "group changed or no longer authorized"} for target in targets[index:])
                        break
                failure = self._check_leaf(leaf, action, parameters, user)
            except Exception as error:
                result["errors"].append({"entity_id": leaf, "reason": "live authorization could not be verified", "detail": type(error).__name__})
                result["skipped"].extend({"entity_id": target, "reason": "earlier target could not be verified"} for target in targets[index + 1:])
                break
            if failure:
                category, reason = failure
                result[category].append({"entity_id": leaf, "reason": reason})
                if expected_leaves is not None:
                    result["skipped"].extend({"entity_id": target, "reason": "earlier target changed"} for target in targets[index + 1:])
                    break
                continue
            domain, service = _service(action, leaf)
            try:
                await self.hass.services.async_call(domain, service, {"entity_id": leaf, **parameters}, blocking=True, context=context)
                result["executed"].append(leaf)
            except Exception as exception:
                detail = f"{type(exception).__name__}: {exception}" if isinstance(exception, ValueError) or type(exception).__module__.startswith("homeassistant.") else type(exception).__name__
                result["errors"].append({"entity_id": leaf, "reason": "service call failed", "detail": detail})
                if expected_leaves is not None:
                    result["skipped"].extend({"entity_id": target, "reason": "earlier service call failed"} for target in targets[index + 1:])
                    break
        return result
