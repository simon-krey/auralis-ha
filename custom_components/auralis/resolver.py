"""Build a complete, locally bound action plan before exposing model tools."""

from dataclasses import dataclass, field
from enum import StrEnum
import re
from time import perf_counter

from .actions import _ACTIONS, power_permission, shared_parameters, valid_parameters
from .names import EXPLICIT_LOCATION, contains, normalize, remove_location
from .retrieval import BULK_TARGET, _ACTION_PATTERNS, _DEFERRED, _EXCEPTION, _GENERIC_LIGHT_SWITCH, _GLOBAL, _QUESTION, _TARGET_GRAMMAR, select_context
from .voice_values import value_expression

MAX_STEPS = 8
_SEPARATOR = re.compile(r"\b(?:und|and|dann|then)\b|;", re.I)
_COLOR_OWNER = r"ihre|seine|deren|dessen|its"
_COLOR_NOUN = r"farbe|color|colour"
_VERB = re.compile(rf"^(?:und |and )?(?:dann |then )?(?:schalt\w*|mach\w*|stell(?:e)?|setze|dimm\w*|turn|switch|make|set|dim|open|close|offne|schliesse|activate|starte|start|stop|pause|(?:(?:ver)?a(?:e)?ndere|change)(?= (?:die|the|{_COLOR_OWNER}) (?:{_COLOR_NOUN})\b))\b")
_NUMBER = r"[-+]?\d+(?:[.,]\d+)?"
_MEASURE = re.compile(rf"(?<![\w.,])({_NUMBER})\s*(%|prozent\b|percent\b|grad\b|degrees?\b|kelvin\b)", re.I)
_ENDING = re.compile(r"(?:\b(?:an|ein|aus|on|off)\s*[.!?]*$|\b(?:auf|to)\s+[-+]?\d|\b(?:rot|red|blau|blue|grün|green|gelb|yellow|weiß|white|purple|lila|orange)\s*[.!?]*$)", re.I)
_BARE_SWITCH = re.compile(r"^\s*(?:on|off)\b", re.I)
_REFERENCE_WORD = r"sie|es|ihn|it|them|(?:die )?beiden?|both"
_REFERENCE = re.compile(rf"\b(?:mach\w*|stell(?:e)?|setze|schalt\w*|dimm\w*|make|set|dim|turn|switch) (?:{_REFERENCE_WORD})\b|^(?:(?:und|and) )?(?:jetzt |now )?(?:auf|to) \d")
_PRONOUN = re.compile(rf"\b(?:{_REFERENCE_WORD})\b", re.I)
_COLOR_REQUEST = re.compile(rf"^(?:(?:ver)?(?:ä|ae)ndere|change|setze|stell(?:e)?|set)\s+(?:(?P<possessive>{_COLOR_OWNER})|die|the)\s+(?:{_COLOR_NOUN})\s+(?P<body>.+)$", re.I)
_COLOR_OBJECT = re.compile(r"^(?:der|des|von|of)(?:\s+the)?\s+(?P<target>.+)\s+(?:auf|to)\s+(?P<value>.+)$", re.I)
_COLOR_VALUE = re.compile(r"^(?:auf|to)\s+(.+)$", re.I)
_EXCLUSION = re.compile(r"\b(?:außer|ausser|ausgenommen|except|bis auf)\b", re.I)
_LEAVE = re.compile(r"\b(?:aber lass(?:e)?|but leave)\b", re.I)
_TRAILING_SWITCH = re.compile(r"\s+(an|ein|aus|on|off)[.!?]*$", re.I)
_RELATIVE = re.compile(r"\b(?:heller|dunkler|warmer|k[äa]lter|lauter|leiser|brighter|darker|cooler|louder|quieter)\b", re.I)
_COLORS = {word: color for words, color in (
    ("rot red", [255, 0, 0]), ("blau blue", [0, 0, 255]), ("grun gruen green", [0, 128, 0]),
    ("gelb yellow", [255, 255, 0]), ("weiss white", [255, 255, 255]),
    ("lila purple", [128, 0, 128]), ("orange", [255, 165, 0]), ("turkis tuerkis turquoise", [64, 224, 208]),
) for word in words.split()}
_WHITE_TONES = frozenset({"warmweiss", "kaltweiss", "warm white", "cool white", "cold white"})


@dataclass
class ActionCandidate:
    action: str
    members: dict[str, tuple[str, ...]]
    schema: dict
    parameters: dict
    scope: list[str]
    state_basis: tuple = ()

    @property
    def targets(self):
        return list(self.members)

    def diagnostic(self):
        return {"action": self.action, "targets": self.targets, "parameters": self.parameters, "scope": self.scope}


class ResolutionMode(StrEnum):
    DETERMINISTIC = "deterministic"
    BOUNDED = "bounded"
    CLARIFICATION = "clarification"
    NO_ACTION = "no_action"


@dataclass(frozen=True)
class BoundedChoice:
    id: str
    action: ActionCandidate

    def diagnostic(self):
        return self.action.diagnostic() | {"choice": self.id, "constraints": self.action.schema}


@dataclass
class Resolution:
    status: str
    entities: list
    enabled: dict = field(default_factory=dict)
    bulk: list = field(default_factory=list)
    steps: list[ActionCandidate] = field(default_factory=list)
    trace: dict = field(default_factory=dict)
    adaptive: bool = False
    choices: tuple[BoundedChoice, ...] = ()
    mode: ResolutionMode = field(init=False)

    def __post_init__(self):
        if self.choices:
            self.mode = ResolutionMode.BOUNDED
        elif self.steps:
            self.mode = ResolutionMode.DETERMINISTIC
        elif self.status in {"clarify", "no_match"}:
            self.mode = ResolutionMode.CLARIFICATION
        else:
            self.mode = ResolutionMode.NO_ACTION


def _parts(prompt, catalog):
    start = 0
    aliases = None
    prefix = ""
    for separator in _SEPARATOR.finditer(prompt):
        before, after = prompt[start:separator.start()], prompt[separator.end():]
        marker = separator.group().lower()
        elliptical_switch = marker == "and" and _BARE_SWITCH.match(after) and re.search(r"\b(?:turn|switch)\b", before, re.I)
        if (marker in {"dann", "then", ";"} or elliptical_switch or _VERB.match(normalize(after))
            or (_ENDING.search(value_expression(before.strip())[0]) and _ENDING.search(value_expression(after.strip())[0]))):
            if marker != ";":
                if aliases is None:
                    aliases = {normalize(value) for entity in catalog for value in (entity["name"], entity.get("device_name", ""), *entity.get("aliases", ())) if _SEPARATOR.search(value)}
                if any(left and right and f" {normalize(before)}".endswith(f" {left}") and f"{normalize(after)} ".startswith(f"{right} ")
                       for alias in aliases if f" {marker} " in alias for left, right in [alias.split(f" {marker} ", 1)]):
                    continue
            yield prefix + re.sub(r"(?:,\s*)?(?:\bund|\band)\s*$", "", before.strip()).rstrip(" ,")
            start = separator.end()
            prefix = "turn " if elliptical_switch else ""
    yield prefix + prompt[start:].strip()


def _parameter_values(action, prompt, trace, schema):
    text = normalize(prompt)
    names = [evidence["heard"] for field_name in ("names", "rooms") for evidence in trace.get(field_name, [])]
    for phrase in sorted(set(names), key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text)
    measures = list(_MEASURE.finditer(prompt))
    if len(measures) > 1 or _RELATIVE.search(text):
        return None
    value, unit = None, None
    if measures:
        value = float(measures[0][1].replace(",", "."))
        unit = measures[0][2].lower()
    if action == "light_set":
        colors = {word for word in text.split() if word in _COLORS}
        if len(colors) > 1 or unit not in {None, "%", "percent", "prozent", "kelvin"}:
            return None
        result = {"rgb_color": _COLORS[next(iter(colors))]} if colors else {}
        if value is not None:
            if unit == "kelvin" and not value.is_integer():
                return None
            result["color_temp_kelvin" if unit == "kelvin" else "brightness_pct"] = int(value) if value.is_integer() else value
        return result
    if action in {"fan_set_percentage", "set_position", "volume_set", "climate_temperature"}:
        if action == "climate_temperature":
            return {"temperature": value} if unit in {"grad", "degree", "degrees"} else None
        if unit not in {"percent", "prozent", "%"}:
            return None
        key = {"fan_set_percentage": "percentage", "set_position": "position", "volume_set": "volume_level"}[action]
        return {key: value / 100 if action == "volume_set" else value}
    if action == "set_value":
        values = re.findall(rf"\b(?:to|auf)\s+({_NUMBER})(?![\w.,])", prompt, re.I)
        if len(values) == 1 and not unit:
            value = normalize(values[0])
            remainder = re.sub(rf"\b(?:to|auf) {re.escape(value)}\b", " ", text)
            if not _unknown_parameter_words(remainder):
                return {"value": float(values[0].replace(",", "."))}
        return None
    if action in {"select_option", "climate_hvac_mode"}:
        key = "option" if action == "select_option" else "hvac_mode"
        options = [option for option in schema[key].get("enum", []) if contains(" ".join(text.split()), normalize(option))]
        if len(options) == 1 and not _unknown_parameter_words(re.sub(rf"\b{re.escape(normalize(options[0]))}\b", " ", text)):
            return {key: options[0]}
        return None
    if action == "fan_oscillate":
        enabled = bool(_ACTION_PATTERNS["turn_on"].search(text))
        disabled = bool(_ACTION_PATTERNS["turn_off"].search(text))
        return {"oscillating": enabled} if enabled != disabled else None
    return {} if not _ACTIONS[action][3] and unit is None else None


def _unknown_parameter_words(text):
    for pattern in _ACTION_PATTERNS.values():
        text = pattern.sub(" ", text)
    return set(text.split()) - _TARGET_GRAMMAR


def _part(prompt, catalog, language, reference, *, previous_clause=False, keep_noops=False, timings=None, local_scope=False):
    trace = {}
    property_request = _COLOR_REQUEST.fullmatch(prompt.strip())
    if property_request:
        subject = _COLOR_OBJECT.fullmatch(property_request["body"]) if not property_request["possessive"] else None
        value = _COLOR_VALUE.fullmatch(property_request["body"])
        color = normalize(subject["value"] if subject else value[1] if value else "")
        if color not in _COLORS and color not in _WHITE_TONES:
            return Resolution("clarify", [], trace={"reason": "missing_ambiguous_or_unsupported_parameters" if subject or value else "unrecognized_color_request"})
        if subject:
            prompt = f"set {subject['target']} color to {color}"
        elif value:
            if not reference or not (property_request["possessive"] or previous_clause):
                return Resolution("clarify", [], trace={"reason": "unresolved_reference"})
            if len(reference) != 1:
                return Resolution("clarify", [], trace={"reason": "ambiguous_reference"})
            antecedents = [entity for entity in catalog if entity["id"] == reference[0] and entity["state"] not in {"unknown", "unavailable"}]
            if not antecedents or antecedents[0].get("group"):
                return Resolution("clarify", [], trace={"reason": "unresolved_reference"})
            catalog = antecedents
            prompt = f"set {reference[0]} color to {color}"
            trace["reference"] = list(reference)
    if reference and re.match(r"^(?:dimme|dim) auf\b", prompt, re.I):
        prompt = re.sub(r"^(?:dimme|dim) ", "dim it ", prompt, count=1, flags=re.I)
    if reference and (normalize(prompt) in _COLORS or normalize(prompt) in _WHITE_TONES):
        prompt = f"set it to {prompt}"
    prompt, live_value = value_expression(prompt, allow_context=bool(reference))
    text = normalize(prompt)
    if _REFERENCE.search(text):
        if not reference:
            return Resolution("clarify", [], trace={"reason": "unresolved_reference"})
        if re.search(r"\b(?:beiden?|both)\b", text) and len(reference) != 2:
            return Resolution("clarify", [], trace={"reason": "ambiguous_reference"})
        if re.search(r"\b(?:it|es|ihn)\b", text) and len(reference) != 1:
            return Resolution("clarify", [], trace={"reason": "ambiguous_reference"})
        catalog = [entity for entity in catalog if entity["id"] in reference]
        targets = " and ".join(entity["id"] for entity in catalog)
        if _PRONOUN.search(prompt):
            prompt = re.sub(rf"\b({_REFERENCE_WORD})\s+(?:jetzt|now)\b", r"\1", prompt, count=1, flags=re.I)
            prompt = _PRONOUN.sub(targets, prompt, count=1)
        else:
            prompt = re.sub(r"^(?:(?:und|and) )?(?:jetzt |now )?(?=auf |to )", f"set {targets} ", prompt, flags=re.I)
        trace["reference"] = list(reference)
    prompt = re.sub(r"\s*,\s*(?:und|and)\s+", " and ", prompt, flags=re.I)
    prompt = re.sub(r"(?<!\d),|,(?!\d)", " and ", prompt)
    spoken = re.sub(r"(?<=\d)\s*%", " percent", prompt)
    entities, enabled, bulk, status = select_context(spoken, catalog, trace=trace, language=language, strict=True, keep_noops=keep_noops, timings=timings, local_scope=local_scope)
    if status != "matched":
        if status == "no_action" and re.search(r"\b(?:gemutlicher|gemuetlicher|gemutlich|gemuetlich|cosier|cozier)\b", text):
            return Resolution("clarify", entities, trace=trace | {"reason": "subjective_value_not_configured"})
        return Resolution(status, entities, enabled, bulk, trace=trace)
    targets = bulk or [entity["id"] for entity in entities]
    if len(enabled) != 1:
        return Resolution("clarify", entities, trace=trace | {"reason": "ambiguous_action"})
    action = next(iter(enabled))
    if property_request and action != "light_set":
        return Resolution("clarify", entities, trace=trace | {"reason": "ambiguous_action"})
    scope = [item["id"] for item in trace.get("scope", [])] or targets
    target_set = set(targets)
    chosen = {entity["id"]: entity for entity in catalog if entity["id"] in target_set}
    specs = {target: next(item for item in chosen[target]["actions"] if item["id"] == action) for target in targets}
    schema = shared_parameters([item.get("parameters", {}).get("properties", {}) for item in specs.values()])
    if _ACTIONS[action][3] and not schema:
        return Resolution("clarify", entities, trace=trace | {"reason": "no_common_parameter_schema"})
    basis = ()
    if live_value:
        key = live_value.parameter
        value = live_value.apply(chosen[targets[0]], schema[key]) if len(targets) == 1 and not bulk and key in schema else None
        if value is None or keep_noops and live_value.parameter != "percentage":
            return Resolution("clarify", entities, trace=trace | {"reason": "live_value_unsupported_or_ambiguous"})
        parameter, basis = value
        parameters = {key: parameter}
        trace["value_source"] = basis[0]
    else:
        parameters = _parameter_values(action, prompt, trace, schema)
    if parameters and (unsupported := sorted(parameters.keys() - schema.keys())):
        return Resolution("no_action", entities, trace=trace | {"reason": "unsupported_action_parameters", "unsupported_parameters": unsupported})
    if parameters is None or not valid_parameters(action, parameters, schema):
        return Resolution("clarify", entities, trace=trace | {"reason": "missing_ambiguous_or_unsupported_parameters"})
    members = {target: tuple(item.get("members", (target,))) for target, item in specs.items()}
    leaves = [leaf for group in members.values() for leaf in group]
    room_ids = {room["id"] for room in trace.get("rooms", [])}
    if len(room_ids) == 1:
        room_members = {entity["id"] for entity in catalog if entity.get("area_id") in room_ids}
        if room_members and (action == "activate" or not (set(targets) | set(leaves)) <= room_members):
            return Resolution("clarify", entities, trace=trace | {"reason": "unverified_room_targets"})
    if len(leaves) != len(set(leaves)):
        return Resolution("clarify", entities, trace=trace | {"reason": "overlapping_group_targets"})
    if power := power_permission(action, parameters):
        leaf_ids = set(leaves)
        blocked = [entity["id"] for entity in catalog if entity["id"] in leaf_ids and any(
            item["id"] == power and item.get("configured", item["enabled"]) is not True for item in entity["actions"]
        )]
        if blocked:
            return Resolution("no_action", entities, trace=trace | {"reason": "power_action_disabled", "rejected": blocked})
    if target_set != set(reference) and "reference" in trace:
        return Resolution("clarify", entities, trace=trace | {"reason": "reference_target_set_changed"})
    adaptive = bool(parameters or any(len(leaves) != 1 or target not in leaves for target, leaves in members.items()) or trace.get("target_spans"))
    return Resolution(status, entities, enabled, bulk, [ActionCandidate(action, members, schema, parameters, scope, basis)], trace, adaptive)


def _exclusion(prompt, catalog, language, reference, timings, local_scope=False):
    leave = _LEAVE.search(prompt)
    marker = leave or _EXCLUSION.search(prompt)
    if not marker:
        return None
    start, end = prompt[:marker.start()].rstrip(" ,"), prompt[marker.end():].strip()
    state = _TRAILING_SWITCH.search(end)
    if state and not leave:
        start += f" {state[1]}"
        end = end[:state.start()]
    included = _part(start, catalog, language, reference, timings=timings, local_scope=local_scope)
    blocked = Resolution("clarify", included.entities, trace=included.trace | {"reason": "unresolved_exclusion"})
    if len(included.steps) != 1 or not included.bulk and not leave:
        return blocked
    step = included.steps[0]
    if leave and state:
        end = end[:state.start()]
    scope = catalog if leave else [entity for entity in catalog if entity["id"] in step.scope]
    excluded, _, _, status = select_context(end, scope, language=language, strict=True, timings=timings)
    location_only = re.fullmatch(r"(?:(?:der|die|den|das|the|those|ones?) )?(?:im|in(?: the| der| dem)?) .+", normalize(end))
    if location_only and status == "clarify" and excluded and len({entity.get("area_id", entity["area"]) for entity in excluded}) == 1:
        pass
    elif status not in {"no_action", "read_only"} or len(excluded) != 1:
        return blocked
    removed = {entity["id"] for entity in excluded}
    if leave:
        required_state = {"an": "on", "ein": "on", "on": "on", "aus": "off", "off": "off"}.get(state[1].lower()) if state else None
        if required_state and any(entity["state"] != required_state for entity in excluded):
            return blocked
        if not included.bulk:
            if removed & set(step.scope):
                return blocked
            included.trace["excluded"] = sorted(removed)
            return included
    if not removed or not removed <= set(step.scope):
        return blocked
    step.scope = [target for target in step.scope if target not in removed]
    step.members = {target: leaves for target, leaves in step.members.items() if target not in removed}
    if not step.targets:
        return blocked
    included.bulk = step.targets
    included.entities = [included.entities[0] | {"count": len(step.targets)}]
    included.trace["excluded"] = sorted(removed)
    included.trace["scope"] = [item for item in included.trace.get("scope", []) if item["id"] not in removed]
    included.adaptive = True
    return included


_ALTERNATIVES = re.compile(r"^(?:wahlweise|either) (.+) (?:oder|or) (.+)$", re.I)
_COMFORT = re.compile(r"^(?:mach(?:e)?|make) (.+) (?:gemütlicher|gemuetlicher|cozier|cosier)[.!?]*$", re.I)
_HERE = re.compile(r"\b(?:hier|bei mir|in diesem (?:zimmer|raum)|here|in this room)\b", re.I)
_LOCAL_OFF = re.compile(r"(?:bitte |please )?(?:(?:schalte?|mache?) alles aus|(?:turn|switch) (?:everything off|off everything))(?: bitte| please)?")
_LOCAL_POWER_DOMAINS = {"light", "switch", "fan", "humidifier"}
_EXPLICIT_ALL = re.compile(r"\b(?:alle[nrs]?|samtliche[nrs]?|all|every)\b")
_REPEAT_LIGHT = re.compile(r"(?:und|and) ((?:die|das|the) (?:lampe|leuchte|licht|lamp|light) (?:im|in|in der|in the) .+?) (?:auch|too)[.!]*$", re.I)
_CHOICE_ACTIONS = frozenset({"light_set", "turn_on", "turn_off", "fan_set_percentage", "volume_set"})


def _bounded(alternatives, catalog, language, *, reason="explicit_alternatives", timings=None):
    branches = [_part(branch, catalog, language, (), keep_noops=True, timings=timings) for branch in alternatives]
    entities = {entity["id"]: entity for branch in branches for entity in branch.entities}
    trace = {"clauses": [branch.trace | {"status": branch.status, "text": text[:512]} for branch, text in zip(branches, alternatives)]}
    if any(len(branch.steps) != 1 or branch.bulk or branch.trace.get("target_spans") or not branch.trace.get("names")
           or any(evidence["method"] not in {"exact", "word_join"} for evidence in branch.trace["names"]) for branch in branches):
        return Resolution("clarify", list(entities.values()), trace=trace | {"reason": "incomplete_bounded_choices"})
    first, second = (branch.steps[0] for branch in branches)
    if (first.targets != second.targets or len(first.targets) != 1
        or not first.targets[0].startswith(("light.", "fan.", "media_player."))
        or any(step.action not in _CHOICE_ACTIONS or step.members[first.targets[0]] != tuple(first.targets) for step in (first, second))
        or (first.action, first.parameters) == (second.action, second.parameters)):
        return Resolution("clarify", list(entities.values()), trace=trace | {"reason": "unsafe_or_duplicate_bounded_choices"})
    target = entities[first.targets[0]]
    if target.get("group") or any(spec.get("sensitive") for spec in target["actions"] if spec["id"] in {first.action, second.action}):
        return Resolution("clarify", list(entities.values()), trace=trace | {"reason": "unsafe_bounded_target"})
    return Resolution("bounded", [target], trace=trace | {"intent_source": reason}, choices=(BoundedChoice("option_1", first), BoundedChoice("option_2", second)))


def target_alternatives(prompt, catalog, language, resolution):
    trace = resolution.trace
    entities = sorted(resolution.entities, key=lambda entity: entity["id"])
    evidence = trace.get("names", ())
    if (trace.get("reason") != "ambiguous_target" or trace.get("target_spans") or len(entities) != 2
        or any(entity.get("group") or not entity["id"].startswith(("light.", "fan.", "media_player.")) for entity in entities)
        or len(evidence) != 2 or len({match["heard"] for match in evidence}) != 1
        or any(match["method"] != "exact" for match in evidence)
        or len({(entity["name"], entity["area"]) for entity in entities}) != 2):
        return ()
    choices = []
    for index, entity in enumerate(entities):
        other = entities[1 - index]["id"]
        result = _part(prompt, [candidate for candidate in catalog if candidate["id"] != other], language, (), keep_noops=True)
        if len(result.steps) != 1 or result.bulk:
            return ()
        step = result.steps[0]
        if (step.targets != [entity["id"]] or step.action not in _CHOICE_ACTIONS
            or any(item.get("sensitive") for item in entity["actions"] if item["id"] == step.action)):
            return ()
        choices.append(BoundedChoice(f"option_{index + 1}", step))
    if (choices[0].action.action, choices[0].action.parameters) != (choices[1].action.action, choices[1].action.parameters):
        return ()
    return tuple(choices)


def resolve(prompt, catalog, language, reference=(), *, comfort=False, timings=None, voice_area=None, voice_request=False, previous_action=None):
    options = {"comfort": comfort, "timings": timings, "previous_action": previous_action}
    marker = _HERE.search(prompt)
    stripped = _HERE.sub(" ", prompt) if marker else prompt
    normalized = normalize(stripped)
    if marker and _GLOBAL.search(normalized):
        return Resolution("clarify", [], trace={"reason": "conflicting_room_scope"})
    everything_areas = set()
    if re.search(r"\b(?:alles|everything)\b", normalized) and EXPLICIT_LOCATION.search(normalized):
        for entity in catalog:
            if entity.get("area_id") and any(_LOCAL_OFF.fullmatch(" ".join(remove_location(normalized, normalize(name)).split()))
                                             for name in (entity["area"], *entity.get("area_aliases", ())) if name):
                everything_areas.add(entity["area_id"])
    if len(everything_areas) > 1:
        return Resolution("clarify", [], trace={"reason": "conflicting_room_scope"})
    if everything_areas:
        return _room_everything(catalog, next(iter(everything_areas)))
    if marker and EXPLICIT_LOCATION.search(normalized):
        if any(_HERE.search(part) and not EXPLICIT_LOCATION.search(normalize(_HERE.sub(" ", part))) for part in _parts(prompt, catalog)):
            return Resolution("clarify", [], trace={"reason": "conflicting_room_scope"})
        resolved = _resolve(stripped, catalog, language, reference, **options)
        clauses = resolved.trace.get("clauses", [resolved.trace])
        rooms = {room["id"] for clause in clauses for room in clause.get("rooms", []) if room["method"] in {"exact", "word_join"}}
        room_members = {entity["id"] for entity in catalog if entity.get("area_id") in rooms}
        if len(rooms) != 1 or not room_members or not _room_bound(resolved, room_members):
            return Resolution("clarify", [], trace=resolved.trace | {"reason": "conflicting_room_scope"})
        resolved.trace["target_area"] = next(iter(rooms))
        return resolved
    implicit = False
    if not marker and (voice_request or voice_area is not None):
        first = normalize(next(_parts(prompt, catalog))) if _SEPARATOR.search(prompt) else normalized
        implicit = bool(_GENERIC_LIGHT_SWITCH.fullmatch(first) and not _EXPLICIT_ALL.search(first))
        if implicit and (EXPLICIT_LOCATION.search(normalized) or _GLOBAL.search(normalized)):
            return Resolution("clarify", [], trace={"reason": "conflicting_room_scope"})
    if not marker and not implicit:
        if _LOCAL_OFF.fullmatch(normalized):
            return Resolution("clarify", [], trace={"reason": "unspecified_area_for_everything"})
        resolved = _resolve(prompt, catalog, language, reference, **options)
        clauses = resolved.trace.get("clauses", [resolved.trace])
        rooms = {room["id"] for clause in clauses for room in clause.get("rooms", [])}
        if len(rooms) == 1 and (area := next(iter(rooms))) in {entity.get("area_id") for entity in catalog}:
            area_ids = {entity["id"] for entity in catalog if entity.get("area_id") == area}
            if not _room_bound(resolved, area_ids):
                return Resolution("clarify", [], trace=resolved.trace | {"reason": "unverified_room_targets"})
            resolved.trace["target_area"] = area
        return resolved
    if not voice_area:
        return Resolution("clarify", [], trace={"reason": "unknown_voice_area"})
    if marker and _LOCAL_OFF.fullmatch(normalized):
        return _room_everything(catalog, voice_area, from_microphone=True)
    area_catalog = [entity for entity in catalog if entity.get("area_id") == voice_area]
    resolved = _resolve(stripped.strip(), area_catalog, language, reference, local_scope=True, **options)
    ids = {entity["id"] for entity in area_catalog}
    clauses = resolved.trace.get("clauses", [resolved.trace])
    if not _room_bound(resolved, ids) or any(
        evidence["method"] not in {"exact", "word_join"} for clause in clauses for evidence in clause.get("names", [])
    ):
        return Resolution("clarify", [], trace=resolved.trace | {"reason": "unverified_room_targets", "voice_area": voice_area, "target_area": voice_area})
    resolved.trace.update(voice_area=voice_area, target_area=voice_area)
    return resolved


def _room_bound(resolved, area_ids):
    return all(step.action != "activate" and target in area_ids and set(leaves) <= area_ids
               for step in [*resolved.steps, *(choice.action for choice in resolved.choices)]
               for target, leaves in step.members.items())


def _room_everything(catalog, area, *, from_microphone=False):
    scope, targets, rows = [], {}, []
    for entity in catalog:
        target = entity["id"]
        if entity.get("area_id") != area or entity.get("group") or target.partition(".")[0] not in _LOCAL_POWER_DOMAINS:
            continue
        action = next((item for item in entity["actions"] if item["id"] == "turn_off" and not item.get("sensitive")), None)
        if action is None:
            continue
        status = "not_allowed" if not action["enabled"] else "already_set" if entity["state"] == "off" else "target"
        rows.append(dict(id=target, name=entity["name"], area=entity["area"], state=entity["state"], status=status))
        if action["enabled"]:
            scope.append(target)
            if status == "target":
                targets[target] = (target,)
    trace = {"scope": rows, "room_everything": True, "target_area": area}
    if from_microphone:
        trace["voice_area"] = area
    if not scope:
        return Resolution("no_action", [], trace=trace)
    summary = {"id": BULK_TARGET, "name": "Freigegebene Geräte im Raum", "area": rows[0]["area"], "count": len(targets or scope), "state": "off"}
    if not targets:
        return Resolution("already_set", [summary], trace=trace)
    return Resolution("matched", [summary], {"turn_off": {}}, list(targets), [ActionCandidate("turn_off", targets, {}, {}, scope)], trace, True)


def _resolve(prompt, catalog, language, reference, *, comfort, timings, previous_action, local_scope=False):
    normalized = normalize(prompt)
    if repeat := _REPEAT_LIGHT.fullmatch(prompt.strip()):
        if len(reference) == 1 and previous_action in {"turn_on", "turn_off"}:
            power = "an" if previous_action == "turn_on" else "aus"
            repeated = _part(f"schalte {repeat[1]} {power}", catalog, language, (), keep_noops=True, timings=timings)
            if (len(repeated.steps) == 1 and not repeated.bulk and len(repeated.entities) == 1
                and repeated.steps[0].targets[0].startswith("light.") and repeated.steps[0].targets[0] not in reference
                and not repeated.entities[0].get("group") and repeated.trace.get("rooms")
                and all(evidence["method"] in {"exact", "word_join"} for evidence in repeated.trace["rooms"])):
                repeated.trace["reference_source"] = "last_executed_power_action"
                return repeated
        return Resolution("clarify", [], trace={"reason": "unresolved_repeat_action"})
    exclusion = _EXCLUSION.search(prompt) or _LEAVE.search(prompt)
    without_marker = normalize(prompt[:exclusion.start()] + " " + prompt[exclusion.end():]) if exclusion else normalized
    if _QUESTION.search(normalized) or _EXCEPTION.search(without_marker) or _DEFERRED.search(normalized):
        return _part(prompt, catalog, language, reference, timings=timings, local_scope=local_scope)
    if not exclusion and (alternatives := _ALTERNATIVES.fullmatch(prompt.strip())):
        return _bounded(alternatives.groups(), catalog, language, timings=timings)
    if exclusion:
        return _exclusion(prompt, catalog, language, reference, timings, local_scope)
    if re.search(r"^(?:(?:und|and|nein|no) )?(?:die andere|the other|die im|those in|auch|also)\b", normalized):
        return Resolution("clarify", [], trace={"reason": "ambiguous_reference"})
    parsing_started = perf_counter()
    parts = list(_parts(prompt, catalog))
    if timings is not None:
        timings["clause_parsing_ms"] = (perf_counter() - parsing_started) * 1000
    if len(parts) == 1:
        if comfort and (match := _COMFORT.fullmatch(prompt.strip())):
            target = match[1]
            interpreted = _bounded((f"set {target} brightness to 30 percent", f"set {target} to 2700 kelvin"), catalog, language, reason="configured_light_comfort", timings=timings)
            if interpreted.choices and all(choice.action.action == "light_set" for choice in interpreted.choices):
                return interpreted
        return _part(prompt, catalog, language, reference, timings=timings, local_scope=local_scope)
    if len(parts) > MAX_STEPS or not all(parts):
        return Resolution("clarify", [], trace={"reason": "invalid_clause_count"})
    clauses, steps = [], []
    entities = {}
    for part in parts:
        item = _part(part, catalog, language, reference, previous_clause=bool(clauses), keep_noops=True, timings=timings, local_scope=local_scope)
        clauses.append(item.trace | {"status": item.status, "text": part[:512]})
        entities.update((entity["id"], entity) for entity in item.entities if entity["id"] != BULK_TARGET)
        if item.status not in {"matched", "already_set"} or any(target["status"] == "not_allowed" for target in item.trace.get("scope", [])):
            return Resolution("clarify", list(entities.values()), trace={"reason": "incomplete_compound_plan", "clauses": clauses})
        reference = tuple(item.steps[0].scope) if item.steps else tuple(item.bulk or [entity["id"] for entity in item.entities if entity["id"] != BULK_TARGET])
        steps.extend(item.steps)
    if not steps:
        return Resolution("clarify", list(entities.values()), trace={"reason": "no_changes", "clauses": clauses})
    previous = {}
    for step in steps:
        for leaf in (leaf for members in step.members.values() for leaf in members):
            operation = step.action, step.parameters
            if previous.get(leaf) == operation:
                return Resolution("clarify", list(entities.values()), trace={"reason": "duplicate_compound_target", "clauses": clauses})
            previous[leaf] = operation
    return Resolution("matched", list(entities.values()), steps=steps, trace={"clauses": clauses}, adaptive=True)
