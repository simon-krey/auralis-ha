"""Anchored value expressions; target names and unsupported words stay intact."""

from dataclasses import dataclass
import math
import re

from .names import normalize


_UNITS = dict(zip("null eins zwei drei vier funf sechs sieben acht neun zehn elf zwolf".split(), range(13)))
_UNITS.update(ein=1, eine=1, fuenf=5, zwoelf=12, dreizehn=13, vierzehn=14, funfzehn=15,
              sechzehn=16, siebzehn=17, achtzehn=18, neunzehn=19)
_TENS = dict(zip("zwanzig dreissig vierzig funfzig sechzig siebzig achtzig neunzig".split(), range(20, 100, 10)))
_WORDS = r"[a-zäöüß]+"
_FINAL = r"(?=[.!?]*\s*$)"
_WORDED_ABSOLUTE = re.compile(rf"(?P<prefix>\b(?:auf|to)\s+)({_WORDS})\s+(prozent|percent|grad|degrees?|kelvin){_FINAL}", re.I)
_FRACTION = re.compile(rf"\b(auf|to)\s+(?:(?:die|the|a|ein)\s+)?(hälfte|haelfte|viertel|half|quarter){_FINAL}", re.I)
_LOWEST = re.compile(rf"\b(?:auf\s+die\s+niedrigste\s+geschwindigkeit|to\s+the\s+lowest\s+speed){_FINAL}", re.I)
_WARMTH = re.compile(rf"\b(?:warmwei(?:ß|ss)|kaltwei(?:ß|ss)|warm white|cool white|cold white){_FINAL}", re.I)
_COMPARATIVE = re.compile(rf"\s+(heller|dunkler|lauter|leiser|brighter|darker|louder|quieter){_FINAL}", re.I)
_EXPLICIT_DELTA = re.compile(rf"\s+(?:um|by)\s+(\d+(?:[.,]\d+)?|{_WORDS})\s+(prozentpunkte[n]?|percentage points){_FINAL}", re.I)
_TEMPERATURE_DELTA = re.compile(rf"\s+(\d+(?:[.,]\d+)?|{_WORDS})\s+(?:grad|degrees?)\s+(höher|hoeher|tiefer|niedriger|lower|higher){_FINAL}", re.I)
_BRIGHTNESS_CHANGE = re.compile(r"^(erhöhe|erhoehe|senke|increase|decrease)\s+(?:die|the)\s+(helligkeit|brightness)\s+(?:(?:der|des|of)(?:\s+the)?\s+(.+))?$", re.I)
_BRIGHTNESS_ABSOLUTE = re.compile(r"^(mach(?:e)?|stell(?:e)?|setze|set)\s+(?:die|the)\s+(helligkeit|brightness)\s+(?:auf|to)\s+", re.I)
_COMPARATIVES = {"heller": ("brightness_pct", 1), "brighter": ("brightness_pct", 1),
                 "dunkler": ("brightness_pct", -1), "darker": ("brightness_pct", -1),
                 "lauter": ("volume_level", 1), "louder": ("volume_level", 1),
                 "leiser": ("volume_level", -1), "quieter": ("volume_level", -1)}
_LIVE_FIELDS = {"brightness_pct": "brightness", "volume_level": "volume_level", "temperature": "temperature", "percentage": "percentage_step"}


def _number(value):
    if re.fullmatch(r"\d+(?:[.,]\d+)?", value):
        return float(value.replace(",", "."))
    value = normalize(value).replace("fuenf", "funf")
    unit, separator, tens = value.partition("und")
    if separator and unit in _UNITS and 1 <= _UNITS[unit] <= 9 and tens in _TENS:
        return _UNITS[unit] + _TENS[tens]
    return _UNITS.get(value, _TENS.get(value))


@dataclass(frozen=True)
class LiveValue:
    parameter: str
    delta: float = 0

    def apply(self, entity, schema):
        field = _LIVE_FIELDS[self.parameter]
        current = entity.get("live_values", {}).get(field)
        if type(current) not in (int, float) or not math.isfinite(current) or entity.get("group"):
            return None
        if self.parameter != "percentage" and entity["state"] in {"off", "unknown", "unavailable"}:
            return None
        if self.parameter == "brightness_pct" and 0 < current <= 255:
            value = round(current * 100 / 255 + self.delta)
            if value == 0:
                return None
        elif self.parameter == "volume_level" and 0 <= current <= 1:
            value = round(current + self.delta / 100, 6)
        elif self.parameter == "percentage" and 0 < current <= 100:
            value = max(1, int(current))
        elif self.parameter == "temperature" and schema["minimum"] <= current <= schema["maximum"]:
            value = current + self.delta
        else:
            return None
        if not schema["minimum"] <= value <= schema["maximum"]:
            return None
        return value, (field, current) if self.parameter == "percentage" else (field, current, entity["state"])


def value_expression(prompt, *, allow_context=False):
    """Return text for normal target parsing and, if needed, a live-value operation."""
    if match := _WORDED_ABSOLUTE.search(prompt):
        value = _number(match[2])
        if value is not None:
            prompt = f"{prompt[:match.start()]}{match['prefix']}{value} {match[3]}{prompt[match.end():]}"
    if match := _FRACTION.search(prompt):
        percent = 50 if normalize(match[2]) in {"half", "halfte", "haelfte"} else 25
        prompt = f"{prompt[:match.start()]}{match[1]} {percent} percent{prompt[match.end():]}"
    if allow_context and (match := _BRIGHTNESS_ABSOLUTE.match(prompt)):
        prompt = "set it brightness to " + prompt[match.end():]
    if (match := _WARMTH.search(prompt)) and not _power_fragment(prompt[:match.start()]):
        warmth = normalize(match[0])
        kelvin = 2700 if warmth in {"warmweiss", "warm white"} else 6500
        suffix = re.sub(r"\s+(?:color|colour|farbe)$", "", prompt[:match.start()].rstrip(), flags=re.I)
        connector = " " if re.search(r"\b(?:auf|to)$", suffix, re.I) else " to "
        return suffix + connector + f"{kelvin} kelvin", None
    if (match := _LOWEST.search(prompt)) and not _power_fragment(prompt[:match.start()]):
        return prompt[:match.start()] + " speed to 1 percent", LiveValue("percentage")
    if (match := _TEMPERATURE_DELTA.search(prompt)) and not _power_fragment(prompt[:match.start()]):
        delta = _number(match[1])
        if delta is not None and 0 < delta <= 10:
            sign = 1 if normalize(match[2]) in {"hoher", "hoeher", "higher"} else -1
            return prompt[:match.start()] + " to 20 degrees", LiveValue("temperature", sign * delta)
    if (match := _EXPLICIT_DELTA.search(prompt)) and (head := _BRIGHTNESS_CHANGE.fullmatch(prompt[:match.start()] + " ")):
        delta = _number(match[1])
        target = head[3] or ("it" if allow_context else None)
        if target and delta is not None and 0 < delta <= 100 and not _power_fragment(target):
            sign = -1 if normalize(head[1]) in {"senke", "decrease"} else 1
            return f"set {target} brightness to 50 percent", LiveValue("brightness_pct", sign * delta)
    if match := _COMPARATIVE.search(prompt):
        head = prompt[:match.start()]
        if not _power_fragment(head) and not re.search(r"\b(?:etwas|ein bisschen|a bit|slightly|viel|much|wenig|little)$", head, re.I):
            parameter, direction = _COMPARATIVES[normalize(match[1])]
            qualifier = "brightness" if parameter == "brightness_pct" else "volume"
            return f"{head} {qualifier} to 50 percent", LiveValue(parameter, 10 * direction)
    return prompt, None


def _power_fragment(text):
    return bool(re.search(r"\b(?:an|ein|aus|on|off|ein\s*schalt\w*|aus\s*schalt\w*)\b", text, re.I))
