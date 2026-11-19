"""Offline backend tests; no Home Assistant or provider required."""
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

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

    async def test_models_and_chat_use_base_url(self):
        bridge = self.bridge()
        await bridge.save({"llm_url": "https://example.test/v1/chat/completions/", "llm_model": "chat-a"})
        requests = []

        async def provider(method, url, token, **kwargs):
            requests.append((method, url, token))
            return {"data": [{"id": "chat-z"}, {"id": "chat-a"}, {"id": "chat-a"}, {}]}

        bridge.provider_json = provider
        self.assertEqual(bridge.public_settings()["llm_url"], "https://example.test/v1")
        self.assertEqual(await bridge.list_models("https://example.test/v1/"), ["chat-a", "chat-z"])
        await bridge.list_models("https://other.test/v1")
        await bridge.list_models("https://example.test/v1", "")
        await bridge.request("llm", payload={"model": "chat-a"})
        self.assertEqual(requests, [
            ("GET", "https://example.test/v1/models", "secret2"),
            ("GET", "https://other.test/v1/models", ""),
            ("GET", "https://example.test/v1/models", ""),
            ("POST", "https://example.test/v1/chat/completions", "secret2"),
        ])

    async def test_provider_error_is_redacted_and_saved_for_tests(self):
        bridge = self.bridge()
        await bridge.save({"llm_url": "https://example.test/v1?api_key=private-query", "llm_model": "chat-a"})

        async def chunks(_size):
            yield b'{"error":{"code":"model_not_found","message":"Unknown model; token secret2 and private-query"}}'

        class ProviderResponse:
            status = 404
            content = types.SimpleNamespace(iter_chunked=chunks)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

        session = types.SimpleNamespace(request=lambda *_args, **_kwargs: ProviderResponse())
        with (
            patch.object(backend, "async_get_clientsession", return_value=session),
            patch.object(backend.aiohttp, "ClientTimeout", create=True),
            patch.object(backend.aiohttp, "ClientError", type("ClientError", (Exception,), {}), create=True),
        ):
            with self.assertRaises(backend.AuralisError) as caught:
                await bridge.chat("Licht an", "de", "context", "user", dry_run=True)
        self.assertEqual(str(caught.exception), "HTTP 404: model_not_found · Unknown model; token [redigiert] and [redigiert]")
        self.assertEqual(bridge.entries[0]["error"], str(caught.exception))
        self.assertTrue(bridge.entries[0]["test"])

    async def test_invalid_stt_test_is_logged(self):
        bridge = self.bridge()
        with self.assertRaisesRegex(backend.AuralisError, "Ungültige Audiodaten"):
            await bridge.transcribe(b"", "audio/wav", "de", True)
        self.assertEqual(bridge.entries[0]["kind"], "stt")
        self.assertTrue(bridge.entries[0]["test"])
        self.assertIn("Ungültige Audiodaten", bridge.entries[0]["error"])

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
