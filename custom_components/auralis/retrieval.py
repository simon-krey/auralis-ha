"""Local selection of entities and actions before a model request."""

import re
import unicodedata

BULK_TARGET = "__all_matching__"
MAX_CONTEXT_ENTITIES = 6
_WORDS = re.compile(r"[a-z0-9]+")
_TYPES = {
    "light": "licht lichter lampe lampen leuchte leuchten light lights lamp lamps",
    "switch": "schalter steckdose steckdosen switch switches plug plugs",
    "fan": "ventilator ventilatoren lufter fan fans",
    "humidifier": "luftbefeuchter humidifier",
    "cover": "rollladen rollos rollo jalousie jalousien vorhang vorhange blinds blind curtain curtains cover garage garagentor",
    "valve": "ventil ventile valve valves",
    "lock": "schloss schlosser lock locks",
    "alarm_control_panel": "alarm alarmanlage",
    "climate": "heizung thermostat thermostate klimaanlage klima heating thermostats ac",
    "media_player": "lautsprecher fernseher speaker speakers tv",
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
_GENERIC_LIGHT_SWITCH = re.compile(r"(?:(?:bitte|please) )?(?:(?:schalte?|mache?|turn|switch) )?(?:(?:die|das|the|alle|samtliche|all|every) )?(?:(?:die|the) )?(?:licht|lichter|lampe|lampen|leuchten|light|lights|lamp|lamps) (?:an|ein|aus|on|off|einschalten|ausschalten|anmachen|ausmachen)(?: bitte| please)?")
_COLLECTIVE_LIGHT_SWITCH = re.compile(r"\b(?:licht|light)\b")
_ENGLISH_LIGHT_SWITCH = re.compile(r"(?:please )?(?:turn|switch) (?:on|off) (?:the )?lights?(?: please)?")
_EXCEPTION = re.compile(r"\b(?:nicht|kein\w*|not|don t|except|ausser|ausgenommen|bis auf|nur wenn|only if|unless|statt|instead|ohne|without)\b")
_LOCAL_QUALIFIER = re.compile(r"\b(?:neben|zwischen|uber dem|unter dem|auf dem|gegenuber|near|next to|between|above|below|opposite|nur|only)\b")
_QUESTION = re.compile(r"^(?:ist|sind|war|was|welch\w*|wie|wo|is|are|what|which|how|where|tell me)\b")
_ACTION_WORDS = {
    "turn_on": r"einschalt\w*|anschalt\w*|anmach\w*|turn on|switch on|(?:an|ein|on)(?= *(?:bitte|please)?(?:$| (?:und|and|im|in)\b))",
    "turn_off": r"ausschalt\w*|abschalt\w*|ausmach\w*|turn off|switch off|(?:aus|off)(?= *(?:bitte|please)?(?:$| (?:und|and|im|in)\b))",
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


def _normalize(value):
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(_WORDS.findall("".join(char for char in decomposed if not unicodedata.combining(char))))


def _contains(text, phrase):
    return bool(phrase and f" {phrase} " in f" {text} ")


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


def select_context(prompt, catalog):
    original = _normalize(prompt)
    rooms = {}
    for entity in catalog:
        if entity["area"]:
            rooms.setdefault(entity.get("area_id", entity["area"]), set()).update(_normalize(value) for value in (entity["area"], *entity.get("area_aliases", ())) if value)
    room_prefixes = {phrase for phrases in rooms.values() for phrase in phrases if phrase and " " not in phrase}
    prefix_lengths = sorted({len(phrase) for phrase in room_prefixes})
    compounds = {word: f"{word[:split]} {word[split:]}" for word in set(original.split()) for split in prefix_lengths if word[:split] in room_prefixes and word[split:] in _DOMAINS}
    text = " ".join(compounds.get(word, word) for word in original.split())
    areas = {area for area, phrases in rooms.items() if any(_contains(text, phrase) for phrase in phrases)}
    room_phrases = {phrase for area in areas for phrase in rooms[area] if phrase}
    named_text = original
    for phrase in sorted(room_phrases, key=len, reverse=True):
        named_text = re.sub(rf"\b(?:im|in(?: dem| der| the)?|am|at(?: the)?) {re.escape(phrase)}\b", " ", named_text)
    words = set(text.split())
    domains = {_DOMAINS[word] for word in words & _DOMAINS.keys()}
    if domains & {"climate", "light"} and not words & {"sensor", "sensoren"}:
        domains.discard("sensor")
    matches = {}
    for entity in catalog:
        phrases = {_normalize(value) for value in (entity["name"], entity["id"], *entity.get("aliases", ()))}
        matches[entity["id"]] = {phrase for phrase in phrases if phrase not in _DOMAINS and (_contains(named_text, phrase) or (phrase not in room_phrases and _contains(text, phrase)))}
    matched_phrases = set().union(*matches.values())
    specific = {phrase for phrase in matched_phrases if not any(other != phrase and _contains(other, phrase) for other in matched_phrases)}
    named = [entity for entity in catalog if matches[entity["id"]] & specific]
    global_scope = bool(_GLOBAL.search(text))
    explicit_all = bool(_ALL.search(text))
    room_command = " ".join(named_text.split())
    collective_switch = _COLLECTIVE_LIGHT_SWITCH.search(room_command) and _GENERIC_LIGHT_SWITCH.fullmatch(room_command)
    room_lights = len(areas) == 1 and not named and (words & _PLURAL_LIGHTS or collective_switch or _ENGLISH_LIGHT_SWITCH.fullmatch(room_command))
    bulk = domains == {"light"} and ((explicit_all and (bool(areas) or global_scope or _GENERIC_LIGHT_SWITCH.fullmatch(text))) or room_lights)
    candidates = []
    if named or areas or domains:
        for entity in (named if named and not bulk else catalog):
            if areas and entity.get("area_id", entity["area"]) not in areas:
                continue
            if (named and not bulk) or _matches_domain(entity, domains, words):
                candidates.append(entity)

    action_text = text
    for phrase in sorted(specific | room_phrases, key=len, reverse=True):
        action_text = re.sub(rf"\b{re.escape(phrase)}\b", " ", action_text)
    action_text = " ".join(action_text.split())
    hints = {action for action, pattern in _ACTION_PATTERNS.items() if pattern.search(action_text)}
    if "turn_on" in hints:
        hints.update({"activate", "vacuum_start"})
    if "turn_off" in hints:
        hints.update({"media_stop", "vacuum_pause"})
    if words & {"prozent", "percent"} or re.search(r"\d\s*%", prompt):
        hints.update({"light_set", "fan_set_percentage", "set_position", "volume_set", "set_value"})
    if "light_set" in hints and "turn_off" not in hints:
        hints.discard("turn_on")
    if _QUESTION.search(text) or _EXCEPTION.search(text):
        return candidates[:MAX_CONTEXT_ENTITIES], {}, [], "read_only"
    if (not named and (_LOCAL_QUALIFIER.search(text) or (areas and not domains))) or {"turn_on", "turn_off"} <= hints:
        return [], {}, [], "clarify"
    if bulk:
        if not hints or (named and explicit_all) or len(areas) > 1 or (global_scope and areas):
            return [], {}, [], "clarify"
        candidates = [entity for entity in candidates if not entity.get("group")]
    elif explicit_all or len(candidates) > 1 or (not areas and not named and re.search(r"\b(?:im|in|am|bei|neben)\b", text)):
        return [], {}, [], "clarify"
    elif domains == {"light"} and not areas and not named and hints & {"turn_on", "turn_off"} and not _GENERIC_LIGHT_SWITCH.fullmatch(text):
        return [], {}, [], "clarify"
    if not candidates:
        return [], {}, [], "no_match"

    if not hints and named and _domain(candidates[0]) in {"scene", "script"} and original in specific:
        hints.add("activate")
    actions = {entity["id"]: {item["id"]: item.get("parameters", {}) for item in entity["actions"] if item.get("enabled") is True and item["id"] in hints} for entity in candidates}
    if not bulk:
        selected = actions[candidates[0]["id"]]
        return candidates, selected, [], "matched" if selected else "no_action"

    available = {action for items in actions.values() for action in items}
    if len(available) != 1:
        return [], {}, [], "clarify" if available else "no_match"
    action = next(iter(available))
    targets = [entity["id"] for entity in candidates if action in actions[entity["id"]] and (action, entity["state"]) not in {("turn_off", "off"), ("turn_on", "on")}]
    if not targets:
        return [], {}, [], "already_set"
    schema = actions[targets[0]][action]
    selection = {"id": BULK_TARGET, "name": "Alle passenden Lampen", "area": candidates[0]["area"] if areas else None, "count": len(targets)}
    return [selection], {action: schema}, targets, "matched"
