"""Local selection of entities and actions before a model request."""

import re
from difflib import SequenceMatcher

from .names import EXPLICIT_LOCATION, MATCH_METHODS, match_names, normalize, number_words, remove_location

BULK_TARGET = "__all_matching__"
_TYPES = {
    "light": "licht lichter beleuchtung lampe lampen leuchte leuchten light lights lamp lamps",
    "switch": "schalter steckdose steckdosen switch switches plug plugs",
    "fan": "ventilator ventilatoren lufter fan fans",
    "humidifier": "luftbefeuchter humidifier",
    "cover": "rollladen rollos rollo jalousie jalousien vorhang vorhange blinds blind curtain curtains cover garage garagentor",
    "valve": "ventil ventile valve valves",
    "lock": "schloss schlosser lock locks",
    "alarm_control_panel": "alarm alarmanlage",
    "climate": "heizung thermostat thermostate klimaanlage klima heating thermostats ac",
    "media_player": "lautsprecher fernseher speaker speakers tv television",
    "vacuum": "saugroboter staubsauger vacuum",
    "scene": "szene szenen scene scenes",
    "script": "skript skripte script scripts",
    "button": "taste tasten knopf button buttons",
    "sensor": "sensor sensoren temperatur temperature humidity luftfeuchtigkeit",
}
_DOMAINS = {word: domain for domain, words in _TYPES.items() for word in words.split()}
_PLURAL_LIGHTS = frozenset("lichter lampen leuchten lights lamps".split())
_ALL = re.compile(r"\b(?:alle[nrs]?|samtliche[nrs]?|all|every)\b")
_GLOBAL = re.compile(r"\b(?:uberall|ueberall|everywhere|im ganzen haus|throughout the house)\b")
_SWITCH_SCOPE_WORDS = frozenset("die das den the ma schalt schalte schalten mach mache machen turn switch set an ein aus on off abschalten einschalten ausschalten anschalten anmachen ausmachen".split())
_GENERIC_LIGHT_SWITCH = re.compile(r"(?:(?:bitte|please) )?(?:(?:schalte?|mache?|turn|switch) )?(?:(?:die|das|the|alle|samtliche|all|every) )?(?:(?:die|the) )?(?:licht|lichter|beleuchtung|lampe|lampen|leuchten|light|lights|lamp|lamps) (?:an|ein|aus|on|off|ein ?schalten|aus ?schalten|an ?machen|aus ?machen)(?: bitte| please)?")
_COLLECTIVE_LIGHT_SWITCH = re.compile(r"\b(?:licht|light|beleuchtung)\b")
_ENGLISH_LIGHT_SWITCH = re.compile(r"(?:please )?(?:turn|switch) (?:on|off) (?:the )?lights?(?: please)?")
_COURTESY = re.compile(r"^(?:(?:kannst|konntest|wurdest|magst) du|(?:can|could|would) you)(?: (?:bitte|please|mal|mir|kurz))* ")
_EXCEPTION = re.compile(r"\b(?:nicht|nie|niemals|kein\w*|not|never|neither|nor|don t|doesn t|do not|except|ausser|ausgenommen|bis auf|wenn|falls|sobald|if|unless|statt|instead|ohne|without)\b")
_NUMBER_LIST = re.compile(r"\b\d+\s+(?:(?:und|and|bis|to|through|oder|or)\s+(?:\w+\s+){0,3})?\d+\b")
_FILLER = frozenset("bitte please mal ma kurz mir kannst konntest wurdest magst du can could would you schalt schalte schalten mach mache machen turn switch set stell stelle auf an ein aus on off im in am der dem den die das the a an des von my mein meine den zum zur und and alle all every samtliche prozent percent grad degrees sie es it im ganzen haus everywhere throughout house".split())
_LOCAL_QUALIFIER = re.compile(r"\b(?:neben|zwischen|uber dem|unter dem|auf dem|gegenuber|near|next to|between|above|below|opposite|nur|only)\b")
_DEFERRED = re.compile(r"\b(?:spater|nachher|morgen|later|tomorrow|um \d+|in \d+ (?:sekunden|minuten|stunden|seconds?|minutes?|hours?))\b")
_QUESTION = re.compile(r"^(?:ist|sind|war|was|welch\w*|wie|wo|is|are|what|which|how|where|tell me)\b")
_ACTION_WORDS = {
    "turn_on": r"ein ?schalt\w*|an ?schalt\w*|an ?mach\w*|turn on|switch on|(?:an|ein|on)(?= *(?:bitte|please)?(?:$| (?:und|and|im|in)\b))",
    "turn_off": r"aus ?schalt\w*|abschalt\w*|aus ?mach\w*|turn off|switch off|(?:aus|off)(?= *(?:bitte|please)?(?:$| (?:und|and|im|in)\b))",
    "light_set": r"dimm\w*|heller|dunkler|helligkeit|brightness|brighter|darker|farb\w*|color|colour|rot|blau|grun|gruen|gelb|weiss|lila|orange|red|blue|green|yellow|white|purple|warmweiss|kaltweiss|warmer|kalter|cooler|white balance",
    "activate": r"aktivier\w*|activate|starte?n?|start|ausfuhr\w*|execute|run",
    "open": r"offn\w*|oeffn\w*|offen|hochfahr\w*|hoch|open|raise",
    "close": r"schliess\w*|runterfahr\w*|herunter|runter|zu\s*machen|close|lower",
    "stop": r"halt\w*|stopp?\w*",
    "unlock": r"entriegel\w*|aufschliess\w*|unlock",
    "lock": r"verriegel\w*|abschliess\w*|lock(?= (?:the|my))",
    "climate_temperature": r"grad|degrees?|temperatur|temperature|warmer|kalter|cooler",
    "climate_hvac_mode": r"heizmodus|kuhlmodus|hvac|heat mode|cool mode",
    "volume_set": r"lautstarke|lauter|leiser|volume|louder|quieter",
    "media_play": r"abspiel\w*|wiedergabe|play|resume",
    "media_pause": r"paus\w*",
    "media_stop": r"stopp?\w*",
    "media_next_track": r"nachst\w* lied|nachst\w* titel|next track|next song",
    "media_previous_track": r"vorherig\w* lied|vorherig\w* titel|previous track|previous song",
    "vacuum_start": r"saug\w*|starte?n?|start|clean",
    "vacuum_pause": r"paus\w*|stopp?\w*",
    "vacuum_return_to_base": r"basis|ladestation|andock\w*|dock|base",
    "press": r"druck\w*|press",
    "select_option": r"wahl\w*|auswahl\w*|select|choose",
    "set_value": r"wert|value",
    "set_position": r"position|halbhoch|halb auf|halfway",
    "fan_set_percentage": r"geschwindigkeit|drehzahl|speed",
    "fan_oscillate": r"oszill\w*|oscillat\w*|schwenk\w*",
    "alarm_disarm": r"entscharf\w*|disarm",
    "alarm_arm_away": r"abwesen\w*|arm away",
    "alarm_arm_home": r"anwesend\w*|arm home",
}
_ACTION_PATTERNS = {action: re.compile(rf"\b(?:{pattern})\b") for action, pattern in _ACTION_WORDS.items()}
_COORDINATOR = re.compile(r"\b(?:und|and)\b")
_TARGET_GRAMMAR = _FILLER | _DOMAINS.keys() | set("to dim make change both beide beiden also auch wieder again kelvin degree degrees prozent percent setze einschalten ausschalten abschalten ausmachen anmachen".split())
_UNSUPPORTED_SCOPE = re.compile(r"\b(?:entweder|either|oder|or|samt|sowie|plus|zusammen mit|along with|as well as|together with|bevor|nachdem|before|after|while|wahrend|solange)\b")
_MEASURE_WORDS = re.compile(r"\b\d+(?: \d+)? (?:prozent|percent|grad|degrees?|kelvin)\b")


def _target_spans(text, names, generic, *, german):
    exact = match_names(text, names, generic=generic, german=german)
    protected = {phrase for group in exact.values() for method, phrase in group if method < 2}
    boundaries = [item for item in _COORDINATOR.finditer(text) if not any(
        match.start() <= item.start() < match.end()
        for phrase in protected if _COORDINATOR.search(phrase)
        for match in re.finditer(rf"\b{re.escape(phrase)}\b", text)
    )]
    if not boundaries:
        return exact, [], False
    matches, spans, incomplete = {}, [], False
    start = 0
    for boundary in [*boundaries, None]:
        end = boundary.start() if boundary else len(text)
        expression = text[start:end].strip()
        found = match_names(expression, names, generic=generic, german=german)
        spans.append({"start": start, "end": end, "expression": expression, "candidates": sorted(found), "status": "resolved" if len(found) == 1 else "ambiguous" if found else "unresolved"})
        if len(found) != 1:
            incomplete = True
        for target, evidence in found.items():
            matches.setdefault(target, set()).update(evidence)
            remainder = expression
            for _, phrase in sorted(evidence, key=lambda item: -len(item[1])):
                remainder = re.sub(rf"\b{re.escape(phrase)}\b", " ", remainder)
            remainder = _MEASURE_WORDS.sub(" ", remainder)
            for pattern in _ACTION_PATTERNS.values():
                remainder = pattern.sub(" ", remainder)
            if set(remainder.split()) - _TARGET_GRAMMAR - generic:
                incomplete = True
                spans[-1]["status"] = "unresolved"
        if boundary:
            start = boundary.end()
    return matches or exact, spans, incomplete


def _domain(entity):
    return entity["id"].partition(".")[0]


def _matches_domain(entity, domains, words):
    domain = _domain(entity)
    if not domains or domain not in domains:
        return not domains
    if domain == "cover":
        garage = bool(words & {"garage", "garagentor"})
        return entity.get("device_class") in ({"garage"} if garage else {"blind", "curtain", "shade", "shutter", "awning"})
    if domain == "switch" and words & {"steckdose", "steckdosen", "plug", "plugs"}:
        return entity.get("device_class") == "outlet"
    return True


def select_context(prompt, catalog, *, trace=None, language="de", strict=False, keep_noops=False):
    german = (language or "de").lower().split("-")[0].split("_")[0] == "de"
    def normalize_name(value):
        normalized = normalize(value)
        return number_words(normalized) if german else normalized

    original = normalize_name(prompt)
    rooms = {}
    for entity in catalog:
        if entity["area"]:
            rooms.setdefault(("area", entity.get("area_id", entity["area"])), set()).update(normalize_name(value) for value in (entity["area"], *entity.get("area_aliases", ())) if value)
        if strict and entity.get("floor"):
            rooms.setdefault(("floor", entity["floor_id"]), set()).update(normalize_name(value) for value in (entity["floor"], *entity.get("floor_aliases", ())) if value)
    room_prefixes = {phrase for phrases in rooms.values() for phrase in phrases if phrase and " " not in phrase}
    prefix_lengths = sorted({len(phrase) for phrase in room_prefixes})
    compounds = {word: f"{word[:split]} {word[split:]}" for word in set(original.split()) for split in prefix_lengths if word[:split] in room_prefixes and word[split:] in _DOMAINS}
    text = " ".join(compounds.get(word, word) for word in original.split())
    area_matches = match_names(text, rooms, rooms=True, german=german)
    areas = set(area_matches)
    room_phrases = {phrase for matches in area_matches.values() for _, phrase in matches}
    named_text = original
    for phrase in sorted(room_phrases, key=len, reverse=True):
        named_text = remove_location(named_text, phrase)
    words = set(text.split())
    domains = {_DOMAINS[word] for word in words & _DOMAINS.keys()}
    if domains & {"climate", "light"} and not words & {"sensor", "sensoren"}:
        domains.discard("sensor")
    scoped = [entity for entity in catalog if not areas or ("area", entity.get("area_id", entity["area"])) in areas or ("floor", entity.get("floor_id")) in areas]
    names = {entity["id"]: {normalize_name(value) for value in (entity["name"], entity["id"], *entity.get("aliases", ()))} for entity in scoped}
    generic = _DOMAINS.keys() | room_phrases
    matches, spans, incomplete = _target_spans(named_text, names, generic, german=german)
    specific = {phrase for group in matches.values() for _, phrase in group}
    named = [entity for entity in scoped if entity["id"] in matches]
    if trace is not None:
        for key, found in (("rooms", area_matches), ("names", matches)):
            trace[key] = [{"id": target[1] if key == "rooms" else target, "heard": phrase, "method": MATCH_METHODS[method]} for target, group in sorted(found.items())[:20] for method, phrase in [min(group)]]
        if spans:
            trace["target_spans"] = spans
        if strict:
            trace["name_spans"] = [
                {"id": target, "start": occurrence.start(), "end": occurrence.end(), "method": MATCH_METHODS[method]}
                for target, group in sorted(matches.items()) for method, phrase in sorted(group)
                for occurrence in re.finditer(rf"\b{re.escape(phrase)}\b", named_text)
            ][:50]
    global_scope = bool(_GLOBAL.search(text))
    explicit_all = bool(_ALL.search(text))
    room_command = named_text
    for phrase in sorted(room_phrases, key=len, reverse=True):
        room_command = re.sub(rf"\b{re.escape(phrase)}\b", " ", room_command)
    room_command = _COURTESY.sub("", " ".join(room_command.split()))
    room_command = " ".join(word for word in room_command.split() if word not in {"bitte", "please", "mal", "kurz", "mir"})
    room_command = re.sub(r"^((?:schalte?|mache?) )ma ", r"\1", room_command)
    collective_switch = _COLLECTIVE_LIGHT_SWITCH.search(room_command) and _GENERIC_LIGHT_SWITCH.fullmatch(room_command)
    room_lights = len(areas) == 1 and not named and (words & _PLURAL_LIGHTS or collective_switch or _ENGLISH_LIGHT_SWITCH.fullmatch(room_command))
    bulk = (len(domains) == 1 and domains <= {"light", "switch", "fan", "humidifier"} and explicit_all) or (domains == {"light"} and room_lights)
    candidates = []
    if named or areas or domains:
        for entity in (named if named and not bulk else scoped):
            if (named and not bulk) or _matches_domain(entity, domains, words):
                candidates.append(entity)

    action_text = text
    for phrase in sorted(specific | room_phrases, key=len, reverse=True):
        action_text = re.sub(rf"\b{re.escape(phrase)}\b", " ", action_text)
    action_text = " ".join(_GLOBAL.sub(" ", action_text).split())
    hints = {action for action, pattern in _ACTION_PATTERNS.items() if pattern.search(action_text)}
    if "turn_on" in hints:
        hints.update({"activate", "vacuum_start"})
    if "turn_off" in hints:
        hints.update({"media_stop", "vacuum_pause"})
    if words & {"prozent", "percent"} or re.search(r"\d\s*%", prompt):
        hints.update({"light_set", "fan_set_percentage", "set_position", "volume_set", "set_value"})
    if strict and "kelvin" in words:
        hints.add("light_set")
    if "light_set" in hints and "turn_off" not in hints:
        hints.discard("turn_on")
    if _QUESTION.search(text) or _EXCEPTION.search(text) or _DEFERRED.search(action_text):
        if trace is not None:
            trace["reason"] = "read_only_expression"
        return candidates, {}, [], "read_only"
    action_words = set(action_text.split())
    if strict and (_UNSUPPORTED_SCOPE.search(action_text) or _LOCAL_QUALIFIER.search(action_text) or (action_words & {"an", "on", "ein"} and action_words & {"aus", "off"})):
        if trace is not None:
            trace["reason"] = "unsupported_scope"
        return candidates, {}, [], "clarify"
    if _NUMBER_LIST.search(text) and not (strict and re.search(r"\d[.,]\d", prompt)):
        if trace is not None:
            trace["reason"] = "unresolved_number_list"
        return candidates[:3], {}, [], "clarify"
    if re.search(r"\b(?:maybe|perhaps|possibly|vielleicht|eventuell|irgend\w*|somewhere|somehow)\b", text):
        return candidates, {}, [], "clarify"
    if not global_scope and (EXPLICIT_LOCATION.search(named_text) or re.search(r"\b(?:im|in|inside)\b", named_text)):
        if trace is not None:
            trace["reason"] = "unresolved_room"
        return [], {}, [], "clarify"
    if len(areas) > 1:
        return candidates, {}, [], "clarify"
    coordinated = bool(spans and named)
    if incomplete and (strict or coordinated and not bulk):
        if trace is not None:
            trace["reason"] = "incomplete_target_set"
        return candidates, {}, [], "clarify"
    # Only unmatched words suggest an unknown target; never turn these suggestions into actions.
    if not named and (hints or explicit_all or domains or areas):
        remainder = text
        for phrase in sorted(room_phrases, key=len, reverse=True):
            remainder = re.sub(rf"\b{re.escape(phrase)}\b", " ", remainder)
        for pattern in _ACTION_PATTERNS.values():
            remainder = pattern.sub(" ", remainder)
        vocabulary = _TARGET_GRAMMAR if strict else _FILLER | _DOMAINS.keys()
        unknown = [word for word in remainder.split() if word not in vocabulary and not word.isdigit()][:20]
        numbered = bool(re.search(r"\b\d+\b", remainder)) and not (words & {"prozent", "percent", "grad", "degree", "degrees", "kelvin"} or "%" in prompt)
        if unknown or numbered:
            pool = [entity for entity in scoped if _matches_domain(entity, domains, words)]
            def score(entity):
                variants = names[entity["id"]]
                return max((SequenceMatcher(None, word, part).ratio() for word in unknown for variant in variants for part in variant.split() if len(word) >= 3), default=0)
            ranked = sorted(((score(entity), entity) for entity in pool), key=lambda item: (-item[0], item[1]["id"]))
            suggestions = [entity for similarity, entity in ranked if areas or domains or similarity >= 0.5][:3]
            if trace is not None:
                trace["reason"] = "unresolved_target"
            return suggestions, {}, [], "clarify"
    if (not named and (_LOCAL_QUALIFIER.search(text) or (areas and not domains))) or {"turn_on", "turn_off"} <= hints:
        return candidates, {}, [], "clarify"
    if bulk:
        if not hints or (named and explicit_all) or len(areas) > 1 or (global_scope and areas):
            return candidates, {}, [], "clarify"
        unparsed_scope = set(_ALL.sub(" ", _GLOBAL.sub(" ", room_command)).split()) - _DOMAINS.keys() - _SWITCH_SCOPE_WORDS
        if unparsed_scope & {"in", "im", "am", "at", "bei", "inside"} or (hints & {"turn_on", "turn_off"} and unparsed_scope):
            return candidates, {}, [], "clarify"
        if trace is not None:
            trace["skipped_groups"] = [entity["id"] for entity in candidates if entity.get("group")]
        candidates = [entity for entity in candidates if not entity.get("group")]
    elif explicit_all or (len(candidates) > 1 and not coordinated) or (not areas and not named and re.search(r"\b(?:im|in|am|bei|neben)\b", text)):
        return candidates, {}, [], "clarify"
    elif domains == {"light"} and not areas and not named and hints & {"turn_on", "turn_off"} and not _GENERIC_LIGHT_SWITCH.fullmatch(text):
        return [], {}, [], "clarify"
    if not candidates:
        return [], {}, [], "clarify" if explicit_all else "no_match"

    if not hints and named and _domain(candidates[0]) in {"scene", "script"} and original in specific:
        hints.add("activate")
    if strict and hints and not hints & {"select_option", "set_value", "climate_hvac_mode"}:
        remainder = _MEASURE_WORDS.sub(" ", _ALL.sub(" ", action_text))
        for pattern in _ACTION_PATTERNS.values():
            remainder = pattern.sub(" ", remainder)
        residue = set(remainder.split())
        mentioned = {_DOMAINS[word] for word in residue & _DOMAINS.keys()}
        if residue - _TARGET_GRAMMAR or mentioned and named and any(_domain(entity) not in mentioned for entity in named):
            if trace is not None:
                trace["reason"] = "unrecognized_command_fragment"
            return candidates, {}, [], "clarify"
    approximate = any(min(group)[0] >= 2 for group in (*area_matches.values(), *matches.values()))
    actions = {entity["id"]: {item["id"]: item.get("parameters", {}) for item in entity["actions"] if item.get("enabled") is True and item["id"] in hints and not (approximate and item.get("sensitive"))} for entity in candidates}
    if trace is not None and strict:
        trace["rejected_actions"] = [{"id": entity["id"], "action": item["id"], "reason": "unsupported" if item.get("supported") is False else "policy" if not item.get("enabled") else "approximate_sensitive"} for entity in candidates for item in entity["actions"] if item["id"] in hints and item["id"] not in actions[entity["id"]]][:50]
    if not bulk and not (coordinated and len(candidates) > 1):
        candidate = candidates[0]
        selected = actions[candidate["id"]]
        desired_action = {"off": "turn_off", "on": "turn_on"}.get(candidate["state"])
        if not keep_noops and _domain(candidate) == "light" and not candidate.get("group") and selected.keys() == {desired_action}:
            return candidates, {}, [], "already_set"
        return candidates, selected, [], "matched" if selected else "no_action"

    if not strict and bulk and domains != {"light"}:
        actions = {target: {action: schema for action, schema in items.items() if action in {"turn_on", "turn_off"}} for target, items in actions.items()}
    available = {action for items in actions.values() for action in items}
    if len(available) != 1:
        return candidates, {}, [], "clarify"
    action = next(iter(available))
    eligible = [entity for entity in candidates if action in actions[entity["id"]]]
    unsupported = any(item["id"] == action and item.get("supported") is False for entity in candidates for item in entity["actions"])
    if (coordinated or strict and (action not in {"turn_on", "turn_off"} or unsupported)) and len(eligible) != len(candidates):
        if trace is not None:
            trace["reason"] = "incompatible_target_set"
            trace["rejected"] = [entity["id"] for entity in candidates if entity not in eligible]
        return candidates, {}, [], "clarify"
    if trace is not None:
        trace["scope"] = [{"id": entity["id"], "name": entity["name"], "area": entity["area"], "state": entity["state"], "status": ("not_allowed" if action not in actions[entity["id"]] else "already_set" if (action, entity["state"]) in {("turn_off", "off"), ("turn_on", "on")} else "target")} for entity in candidates]
    targets = [entity["id"] for entity in eligible if keep_noops or (action, entity["state"]) not in {("turn_off", "off"), ("turn_on", "on")}]
    target_area = candidates[0]["area"] if areas and next(iter(areas))[0] == "area" else candidates[0].get("floor") if areas else None
    selection = {"id": BULK_TARGET, "name": "Alle passenden Geräte" if domains != {"light"} else "Alle passenden Lampen", "area": target_area, "count": len(targets)}
    if not targets:
        return [selection | {"state": eligible[0]["state"], "count": len(eligible)}], {}, [], "already_set"
    schema = actions[targets[0]][action]
    return [selection], {action: schema}, targets, "matched"
