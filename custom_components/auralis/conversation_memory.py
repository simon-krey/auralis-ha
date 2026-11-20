"""Expiring, in-process target memory, never a source of action authority."""

from collections import OrderedDict
import time

TTL = 300
MAX_CONVERSATIONS = 128
MAX_REFERENTS = 256


def _identity(entity):
    return (entity["id"], entity["name"], entity.get("area_id", entity.get("area")), entity.get("registry_id"))


class ConversationMemory:
    def __init__(self):
        self._entries = OrderedDict()

    def forget(self, key):
        self._entries.pop(key, None)

    def get(self, key, catalog):
        entry = self._entries.get(key)
        if entry is None:
            return ()
        deadline, targets = entry
        live = {_identity(entity) for entity in catalog if entity["state"] not in {"unavailable", "unknown"}}
        if deadline <= time.monotonic() or any(target not in live for target in targets):
            self.forget(key)
            return ()
        return tuple(target[0] for target in targets)

    def remember(self, key, targets, catalog):
        self.forget(key)
        if key is None or not targets or len(targets) > MAX_REFERENTS:
            return
        index = {entity["id"]: _identity(entity) for entity in catalog}
        if any(target not in index for target in targets):
            return
        self._entries[key] = (time.monotonic() + TTL, tuple(index[target] for target in dict.fromkeys(targets)))
        if len(self._entries) > MAX_CONVERSATIONS:
            self._entries.popitem(last=False)
