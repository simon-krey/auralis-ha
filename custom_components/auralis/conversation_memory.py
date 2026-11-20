"""Expiring, in-process target memory, never a source of action authority."""

from collections import OrderedDict
from dataclasses import dataclass
import time

TTL = 300
MAX_CONVERSATIONS = 128
MAX_REFERENTS = 256
MAX_PENDING_TEXT = 512


def _identity(entity):
    return (entity["id"], entity["name"], entity.get("area_id", entity.get("area")), entity.get("registry_id"), entity.get("floor_id"))


@dataclass(frozen=True)
class PendingChoice:
    prompt: str
    language: str
    signatures: tuple[str, ...]
    kind: str = "action"


@dataclass(frozen=True)
class Referent:
    deadline: float
    identities: tuple
    source: str
    action: str | None = None
    parameters: tuple = ()
    pending: PendingChoice | None = None
    correction: tuple[PendingChoice, int] | None = None

    @property
    def ids(self):
        return tuple(identity[0] for identity in self.identities)


class ConversationMemory:
    def __init__(self):
        self._entries = OrderedDict()

    def forget(self, key):
        self._entries.pop(key, None)

    def get(self, key, catalog):
        entry = self.context(key, catalog)
        return entry.ids if entry and entry.pending is None else ()

    def context(self, key, catalog):
        entry = self._entries.get(key)
        if entry is None:
            return None
        ids = set(entry.ids)
        live = {_identity(entity) for entity in catalog if entity["id"] in ids and entity["state"] not in {"unavailable", "unknown"}}
        if entry.deadline <= time.monotonic() or any(target not in live for target in entry.identities):
            self.forget(key)
            return None
        return entry

    def remember(self, key, targets, catalog, *, source="resolved", action=None, parameters=None, pending=None, correction=None):
        self.forget(key)
        if key is None or not targets or len(targets) > MAX_REFERENTS:
            return
        if pending is not None and len(pending.prompt) > MAX_PENDING_TEXT:
            return
        requested = set(targets)
        index = {entity["id"]: _identity(entity) for entity in catalog if entity["id"] in requested}
        if any(target not in index for target in targets):
            return
        values = tuple((name, tuple(value) if isinstance(value, list) else value) for name, value in (parameters or {}).items())
        self._entries[key] = Referent(time.monotonic() + TTL, tuple(index[target] for target in dict.fromkeys(targets)), source, action, values, pending, correction)
        if len(self._entries) > MAX_CONVERSATIONS:
            self._entries.popitem(last=False)
