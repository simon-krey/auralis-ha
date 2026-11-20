"""Bounded matching of spoken Home Assistant names."""

import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from itertools import groupby

_WORDS = re.compile(r"[a-z0-9]+")
_NEXT_NUMBER = re.compile(r"\d+\b")
_NUMBER_WORDS = dict(zip("null eins zwei drei vier funf sechs sieben acht neun zehn elf zwolf".split(), map(str, range(13))))
_LOCATION = r"(?:im|in(?: dem| der| the)?|am|at(?: the)?|inside(?: the)?)"
LOCATION_PREFIX = re.compile(rf"\b{_LOCATION} $")
EXPLICIT_LOCATION = re.compile(r"\b(?:im|in dem|in der|in the|inside the)\b")


def normalize(value):
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(_WORDS.findall("".join(char for char in decomposed if not unicodedata.combining(char))))


def number_words(value):
    return " ".join(_NUMBER_WORDS.get(word, word) for word in value.split())


def contains(text, phrase):
    return bool(phrase and f" {phrase} " in f" {text} ")


def remove_location(text, phrase):
    return re.sub(rf"\b{_LOCATION} {re.escape(phrase)}\b", " ", text)


def _phonetic(value):
    for pattern, replacement in (("sch", "S"), ("ch", "C"), ("ph|v", "f"), ("ck|qu", "k"), ("tz|ts", "z"), ("ae", "a"), ("oe", "o"), ("ue", "u"), ("ei|ai|ey|ay", "Y"), ("ie|ih", "i"), ("(?<=[aeiou])h", ""), ("er$", "a")):
        value = re.sub(pattern, replacement, value)
    return re.sub(r"(.)\1+", r"\1", value)


def _cologne(value):
    letters = re.sub(r"[^a-z]", "", value)
    codes = []
    for index, letter in enumerate(letters):
        before = letters[index - 1] if index else " "
        after = letters[index + 1:index + 2] or " "
        if letter in "aeijouy":
            codes.append("0")
        elif letter == "b" or (letter == "p" and after != "h"):
            codes.append("1")
        elif letter in "dt" and after not in "csz":
            codes.append("2")
        elif letter in "fvw" or letter == "p":
            codes.append("3")
        elif letter in "gkq":
            codes.append("4")
        elif letter == "c":
            hard = after in "ahkloqrux" if not codes else before not in "sz" and after in "ahkoqux"
            codes.append("4" if hard else "8")
        elif letter == "x":
            codes.extend("8" if before in "ckq" else "48")
        elif letter != "h":
            codes.append({"l": "5", "m": "6", "n": "6", "r": "7"}.get(letter, "8"))
    reduced = "".join(code for code, _ in groupby(codes))
    return reduced[:1] + reduced[1:].replace("0", "")


def _sound_keys(value, *, rooms):
    simple = _phonetic(value)
    if len(simple) >= (4 if rooms else 5):
        yield "simple", simple
    if len(value) >= 7:
        cologne = _cologne(value)
        if len(cologne) >= 4:
            yield "cologne", cologne


def _within_edits(left, right, limit):
    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index] + [limit + 1] * len(right)
        for column in range(max(1, index - limit), min(len(right), index + limit) + 1):
            current[column] = min(previous[column] + 1, current[column - 1] + 1, previous[column - 1] + (char != right[column - 1]))
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


MATCH_METHODS = ("exact", "word_join", "edit_distance", "phonetic")


@lru_cache(maxsize=8192)
def _alias_sound_keys(compact_name, rooms):
    return tuple(_sound_keys(compact_name, rooms=rooms))


def _specific(matches):
    phrases = {phrase for group in matches.values() for _, phrase in group}
    specific = {phrase for phrase in phrases if not any(other != phrase and contains(other, phrase) for other in phrases)}
    return {key: {(method, phrase) for method, phrase in group if phrase in specific} for key, group in matches.items() if any(phrase in specific for _, phrase in group)}


def match_names(text, names, *, rooms=False, generic=(), german=True):
    surrounded = f" {text} "

    def exact_match(variant):
        if not variant:
            return False
        needle = f" {variant} "
        offset = surrounded.find(needle)
        while offset >= 0:
            if not _NEXT_NUMBER.match(surrounded, offset + len(needle)):
                return True
            offset = surrounded.find(needle, offset + 1)
        return False

    exact = {key: {(0, variant) for variant in variants if variant not in generic and exact_match(variant)} for key, variants in names.items()}
    exact = {key: matches for key, matches in exact.items() if matches}
    if exact:
        return _specific(exact)
    words = text.split()
    joined = defaultdict(set)
    sounds = defaultdict(set)
    edits = defaultdict(set)
    if len(words) <= 64:
        for start in range(len(words)):
            explicit = rooms and bool(LOCATION_PREFIX.search(" ".join(words[:start]) + " "))
            for end in range(start + 1, min(len(words), start + 5) + 1):
                phrase = " ".join(words[start:end])
                compact = "".join(words[start:end])
                if 4 <= len(compact) <= 48 and phrase not in generic and not (end < len(words) and words[end].isdigit()):
                    joined[compact].add(phrase)
                    if not rooms or explicit:
                        digits = tuple(re.findall(r"\d+", compact))
                        if german:
                            for sound in _sound_keys(compact, rooms=rooms):
                                sounds[(sound, digits)].add(phrase)
                        edits[(compact[0], len(compact), digits)].add((compact, phrase))

    joined_matches = defaultdict(set)
    fuzzy_matches = defaultdict(set)
    for key, variants in names.items():
        for variant in variants:
            if not variant or variant in generic:
                continue
            compact_name = variant.replace(" ", "")
            if compact_name in joined:
                joined_matches[key].update((1, phrase) for phrase in joined[compact_name])
            if not (sounds or edits) or not 5 <= len(compact_name) <= 48:
                continue
            digits = tuple(re.findall(r"\d+", compact_name))
            if german:
                for sound in _alias_sound_keys(compact_name, rooms):
                    fuzzy_matches[key].update((3, phrase) for phrase in sounds.get((sound, digits), ()))
            if len(compact_name) < 6:
                continue
            limit = 1 if len(compact_name) < 11 else 2
            for length in range(len(compact_name) - limit, len(compact_name) + limit + 1):
                for compact, phrase in edits.get((compact_name[0], length, digits), ()):
                    if _within_edits(compact, compact_name, limit):
                        fuzzy_matches[key].add((2, phrase))

    return _specific(joined_matches or fuzzy_matches)
