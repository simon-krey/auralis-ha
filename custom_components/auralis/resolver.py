"""Build a complete, locally bound action plan before exposing model tools."""

from dataclasses import dataclass, field
import re

from .actions import _ACTIONS, shared_parameters, valid_parameters
from .names import contains, normalize
from .retrieval import BULK_TARGET, _ACTION_PATTERNS, _DEFERRED, _EXCEPTION, _QUESTION, _TARGET_GRAMMAR, select_context

MAX_STEPS = 8
_SEPARATOR = re.compile(r"\b(?:und|and|dann|then)\b|;", re.I)
_VERB = re.compile(r"^(?:und |and )?(?:dann |then )?(?:schalt\w*|mach\w*|stell\w*|dimm\w*|turn|switch|make|set|dim|open|close|offne|schliesse|activate|starte|start|stop|pause)\b")
_NUMBER = r"[-+]?\d+(?:[.,]\d+)?"
_MEASURE = re.compile(rf"(?<![\w.,])({_NUMBER})\s*(%|prozent\b|percent\b|grad\b|degrees?\b|kelvin\b)", re.I)
_ENDING = re.compile(r"(?:\b(?:an|ein|aus|on|off)\s*[.!?]*$|\b(?:auf|to)\s+[-+]?\d|\b(?:rot|red|blau|blue|grün|green|gelb|yellow|weiß|white|purple|lila|orange)\s*[.!?]*$)", re.I)
_BARE_SWITCH = re.compile(r"^\s*(?:on|off)\b", re.I)
_REFERENCE = re.compile(r"\b(?:mach\w*|stell\w*|schalt\w*|dimm\w*|make|set|dim|turn|switch) (?:sie|es|ihn|it|them|beide|both)\b|^(?:(?:und|and) )?(?:jetzt |now )?(?:auf|to) \d")
_PRONOUN = re.compile(r"\b(?:sie|es|ihn|it|them|beide|both)\b", re.I)
_EXCLUSION = re.compile(r"\b(?:außer|ausser|ausgenommen|except|bis auf)\b", re.I)
_LEAVE = re.compile(r"\b(?:aber lass(?:e)?|but leave)\b", re.I)
_TRAILING_SWITCH = re.compile(r"\s+(an|ein|aus|on|off)[.!?]*$", re.I)
_RELATIVE = re.compile(r"\b(?:heller|dunkler|warmer|k[äa]lter|lauter|leiser|brighter|darker|cooler|louder|quieter)\b", re.I)
_COLORS = {word: color for words, color in (
    ("rot red", [255, 0, 0]), ("blau blue", [0, 0, 255]), ("grun gruen green", [0, 128, 0]),
    ("gelb yellow", [255, 255, 0]), ("weiss white", [255, 255, 255]),
    ("lila purple", [128, 0, 128]), ("orange", [255, 165, 0]),
) for word in words.split()}


@dataclass
class ActionCandidate:
    action: str
    members: dict[str, tuple[str, ...]]
    schema: dict
    parameters: dict
    scope: list[str]

    @property
    def targets(self):
        return list(self.members)

    def diagnostic(self):
        return {"action": self.action, "targets": self.targets, "parameters": self.parameters, "scope": self.scope}


@dataclass
class Resolution:
    status: str
    entities: list
    enabled: dict = field(default_factory=dict)
    bulk: list = field(default_factory=list)
    steps: list[ActionCandidate] = field(default_factory=list)
    trace: dict = field(default_factory=dict)
    adaptive: bool = False


def _parts(prompt, catalog):
    start = 0
    aliases = None
    prefix = ""
    for separator in _SEPARATOR.finditer(prompt):
        before, after = prompt[start:separator.start()], prompt[separator.end():]
        marker = separator.group().lower()
        elliptical_switch = marker == "and" and _BARE_SWITCH.match(after) and re.search(r"\b(?:turn|switch)\b", before, re.I)
        if marker in {"dann", "then", ";"} or elliptical_switch or _VERB.match(normalize(after)) or (_ENDING.search(before) and _ENDING.search(after)):
            if marker != ";":
                if aliases is None:
                    aliases = {normalize(value) for entity in catalog for value in (entity["name"], *entity.get("aliases", ())) if _SEPARATOR.search(value)}
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


def _part(prompt, catalog, language, reference, *, keep_noops=False):
    trace = {}
    text = normalize(prompt)
    if _REFERENCE.search(text):
        if not reference:
            return Resolution("clarify", [], trace={"reason": "unresolved_reference"})
        if re.search(r"\b(?:beide|both)\b", text) and len(reference) != 2:
            return Resolution("clarify", [], trace={"reason": "ambiguous_reference"})
        if re.search(r"\b(?:it|es|ihn)\b", text) and len(reference) != 1:
            return Resolution("clarify", [], trace={"reason": "ambiguous_reference"})
        catalog = [entity for entity in catalog if entity["id"] in reference]
        targets = " and ".join(entity["id"] for entity in catalog)
        if _PRONOUN.search(prompt):
            prompt = _PRONOUN.sub(targets, prompt, count=1)
        else:
            prompt = re.sub(r"^(?:(?:und|and) )?(?:jetzt |now )?(?=auf |to )", f"set {targets} ", prompt, flags=re.I)
        trace["reference"] = list(reference)
    prompt = re.sub(r"\s*,\s*(?:und|and)\s+", " and ", prompt, flags=re.I)
    prompt = re.sub(r"(?<!\d),|,(?!\d)", " and ", prompt)
    spoken = re.sub(r"(?<=\d)\s*%", " percent", prompt)
    entities, enabled, bulk, status = select_context(spoken, catalog, trace=trace, language=language, strict=True, keep_noops=keep_noops)
    result = Resolution(status, entities, enabled, bulk, trace=trace)
    if status != "matched":
        return result
    targets = bulk or [entity["id"] for entity in entities]
    if len(enabled) != 1:
        return Resolution("clarify", entities, trace=trace | {"reason": "ambiguous_action"})
    action = next(iter(enabled))
    scope = [item["id"] for item in trace.get("scope", [])] or targets
    target_set = set(targets)
    chosen = {entity["id"]: entity for entity in catalog if entity["id"] in target_set}
    specs = {target: next(item for item in chosen[target]["actions"] if item["id"] == action) for target in targets}
    schema = shared_parameters([item.get("parameters", {}).get("properties", {}) for item in specs.values()])
    if _ACTIONS[action][3] and not schema:
        return Resolution("clarify", entities, trace=trace | {"reason": "no_common_parameter_schema"})
    parameters = _parameter_values(action, prompt, trace, schema)
    if parameters is None or not valid_parameters(action, parameters, schema):
        return Resolution("clarify", entities, trace=trace | {"reason": "missing_ambiguous_or_unsupported_parameters"})
    members = {target: tuple(item.get("members", (target,))) for target, item in specs.items()}
    leaves = [leaf for group in members.values() for leaf in group]
    if len(leaves) != len(set(leaves)):
        return Resolution("clarify", entities, trace=trace | {"reason": "overlapping_group_targets"})
    if target_set != set(reference) and "reference" in trace:
        return Resolution("clarify", entities, trace=trace | {"reason": "reference_target_set_changed"})
    result.steps = [ActionCandidate(action, members, schema, parameters, scope)]
    result.adaptive = bool(parameters or any(len(leaves) != 1 or target not in leaves for target, leaves in members.items()) or trace.get("target_spans"))
    return result


def _exclusion(prompt, catalog, language, reference):
    leave = _LEAVE.search(prompt)
    marker = leave or _EXCLUSION.search(prompt)
    if not marker:
        return None
    start, end = prompt[:marker.start()].rstrip(" ,"), prompt[marker.end():].strip()
    state = _TRAILING_SWITCH.search(end)
    if state and not leave:
        start += f" {state[1]}"
        end = end[:state.start()]
    included = _part(start, catalog, language, reference)
    blocked = Resolution("clarify", included.entities, trace=included.trace | {"reason": "unresolved_exclusion"})
    if len(included.steps) != 1 or not included.bulk and not leave:
        return blocked
    step = included.steps[0]
    if leave and state:
        end = end[:state.start()]
    scope = catalog if leave else [entity for entity in catalog if entity["id"] in step.scope]
    excluded, _, _, status = select_context(end, scope, language=language, strict=True)
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


def resolve(prompt, catalog, language, reference=()):
    normalized = normalize(prompt)
    exclusion = _EXCLUSION.search(prompt) or _LEAVE.search(prompt)
    without_marker = normalize(prompt[:exclusion.start()] + " " + prompt[exclusion.end():]) if exclusion else normalized
    if _QUESTION.search(normalized) or _EXCEPTION.search(without_marker) or _DEFERRED.search(normalized):
        return _part(prompt, catalog, language, reference)
    if exclusion:
        return _exclusion(prompt, catalog, language, reference)
    if re.search(r"^(?:(?:und|and|nein|no) )?(?:die andere|the other|die im|those in|auch|also)\b", normalized):
        return Resolution("clarify", [], trace={"reason": "ambiguous_reference"})
    parts = list(_parts(prompt, catalog))
    if len(parts) == 1:
        return _part(prompt, catalog, language, reference)
    if len(parts) > MAX_STEPS or not all(parts):
        return Resolution("clarify", [], trace={"reason": "invalid_clause_count"})
    clauses, steps = [], []
    entities = {}
    for part in parts:
        item = _part(part, catalog, language, reference, keep_noops=True)
        clauses.append(item.trace | {"status": item.status})
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
