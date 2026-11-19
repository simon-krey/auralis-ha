"""Offline backend tests; no Home Assistant or provider required."""
import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).parents[1] / "custom_components" / "auralis"
package = types.ModuleType("bridge_test")
package.__path__ = [str(ROOT)]
sys.modules["bridge_test"] = package
for name in ("aiohttp", "homeassistant", "homeassistant.helpers", "homeassistant.helpers.aiohttp_client", "homeassistant.helpers.storage", "bridge_test.actions"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = lambda hass: None
sys.modules["homeassistant.helpers.storage"].Store = object
sys.modules["bridge_test.actions"].ActionPolicy = object
spec = importlib.util.spec_from_file_location("bridge_test.backend", ROOT / "backend.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)

class Hass:
    async def async_add_executor_job(self, function, *args):
        return function(*args)

class Store:
    async def async_save(self, data): self.data = data

class Policy:
    def catalog(self, user=None):
        return [{"id":"light.room", "name":"Room", "area":None, "state":"off", "actions":[{"id":"turn_on", "enabled":True}, {"id":"unlock", "enabled":False}]}]
    async def run(self, action, entity_ids, parameters, context, *, dry_run=False, user=None):
        assert dry_run and user == "user" and context == "context"
        return {"status":"dry_run"}

class BackendTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self):
        obj = backend.Bridge.__new__(backend.Bridge)
        obj.hass = Hass()
        obj.settings = dict(backend.DEFAULTS)
        obj.settings.update(stt_token="secret", llm_token="secret2")
        obj.overrides, obj.entries = {}, []
        obj.store, obj.history_store = Store(), Store()
        import asyncio
        obj.lock = asyncio.Lock()
        obj.policy = lambda: Policy()
        return obj

    def test_urls_and_reasoning(self):
        self.assertTrue(backend.valid_url("http://localhost:1234/v1/chat/completions"))
        self.assertFalse(backend.valid_url("http://user:secret@host/path"))
        self.assertEqual(backend.clean("<think>secret</think>Hallo"), "Hallo")
        self.assertEqual(backend.clean("Hallo <thinking>secret"), "Hallo")

    async def test_settings_and_history(self):
        obj = self.bridge()
        self.assertNotIn("stt_token", obj.public_settings())
        self.assertTrue(obj.public_settings()["stt_has_token"])
        await obj.save({"stt_model":"new"})
        self.assertEqual(obj.settings["stt_token"], "secret")
        await obj.save({"stt_token":""})
        self.assertFalse(obj.public_settings()["stt_has_token"])
        await obj.record("stt", audio=b"abc", mime="audio/wav", transcript="test")
        self.assertTrue(obj.entries[0]["has_audio"])
        obj.entries[0]["_timestamp"] = 0
        await obj.prune()
        self.assertEqual(obj.entries, [])

    async def test_tool_loop_dry_run_and_disabled_action(self):
        obj = self.bridge()
        requests = []
        async def request(kind, *, payload=None, form=None):
            requests.append(payload)
            if len(requests) == 1:
                return {"choices":[{"message":{"tool_calls":[{"id":"1", "type":"function", "function":{"name":"run_action", "arguments":'{"action":"turn_on","entity_ids":["light.room"],"parameters":{}}'}}]}}]}
            return {"choices":[{"message":{"content":"<think>private</think>Erledigt"}}]}
        obj.request = request
        result = await obj.chat("Licht an", "de", "context", "user", dry_run=True)
        self.assertEqual(result["answer"], "Erledigt")
        self.assertEqual(result["tools"][0]["result"]["status"], "dry_run")
        self.assertEqual(obj.entries[0]["tools"][0]["parameters"], {})
        self.assertTrue(obj.entries[0]["test"])
        self.assertNotIn("unlock", str(requests[0]))
        self.assertNotIn("secret", str(requests[0]))

if __name__ == "__main__": unittest.main()
