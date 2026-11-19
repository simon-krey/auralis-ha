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
sys.modules["bridge_test.actions"].MAX_ACTION_TARGETS = 100
spec = importlib.util.spec_from_file_location("bridge_test.backend", ROOT / "backend.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)

class Hass:
    async def async_add_executor_job(self, function, *args):
        return function(*args)

class Store:
    async def async_save(self, data): self.data = data

class Policy:
    def __init__(self):
        self.entities = [{"id":"light.room", "name":"Room", "area":None, "state":"off", "actions":[{"id":"turn_on", "enabled":True}, {"id":"unlock", "enabled":False}]}]
        self.calls = []

    def catalog(self, user=None, *, for_retrieval=False):
        return self.entities

    async def run(self, action, entity_ids, parameters, context, *, dry_run=False, user=None):
        assert dry_run and user == "user" and context == "context"
        self.calls.append((action, entity_ids, parameters))
        return {"executed": [], "planned": entity_ids, "skipped": [], "errors": []}


def entity(entity_id, name, area, *actions, state="on", aliases=(), **extra):
    return {"id": entity_id, "name": name, "area": area, "state": state, "aliases": aliases,
            "actions": [{"id": action, "enabled": True} for action in actions], **extra}


def tool_response(action, target):
    import json
    arguments = json.dumps({"action": action, "entity_ids": [target], "parameters": {}})
    return {"choices": [{"message": {"tool_calls": [{"id": "1", "type": "function", "function": {"name": "run_action", "arguments": arguments}}]}}]}

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
        details = {}
        with (
            patch.object(backend, "async_get_clientsession", return_value=session),
            patch.object(backend.aiohttp, "ClientTimeout", create=True),
            patch.object(backend.aiohttp, "ClientError", type("ClientError", (Exception,), {}), create=True),
        ):
            with self.assertRaises(backend.AuralisError) as caught:
                await bridge.chat("Licht an", "de", "context", "user", dry_run=True, details=details)
        self.assertEqual(str(caught.exception), "HTTP 404: model_not_found · Unknown model; token [redigiert] and [redigiert]")
        self.assertEqual(bridge.entries[0]["error"], str(caught.exception))
        self.assertTrue(bridge.entries[0]["test"])
        self.assertEqual(bridge.entries[0]["diagnostics"], details)
        self.assertEqual(list(details["selection"]["action_parameters"]), ["turn_on"])
        self.assertTrue(details["requests"][0]["failed"])
        self.assertGreaterEqual(details["duration_ms"], details["requests"][0]["duration_ms"])
        self.assertNotIn("secret2", str(details))

    async def test_invalid_stt_test_is_logged(self):
        bridge = self.bridge()
        with self.assertRaisesRegex(backend.AuralisError, "Ungültige Audiodaten"):
            await bridge.transcribe(b"", "audio/wav", "de", True)
        self.assertEqual(bridge.entries[0]["kind"], "stt")
        self.assertTrue(bridge.entries[0]["test"])
        self.assertIn("Ungültige Audiodaten", bridge.entries[0]["error"])
        details = bridge.entries[0]["diagnostics"]
        self.assertEqual(details["audio_bytes"], 0)
        self.assertEqual(details["requests"], [])
        self.assertGreaterEqual(details["duration_ms"], 0)

    async def test_stt_success_records_audio_and_api_timing(self):
        bridge = self.bridge()
        class FormData:
            def add_field(self, *_args, **_kwargs):
                pass

        async def request(kind, *, form):
            self.assertEqual(kind, "stt")
            return {"text": "Licht aus"}

        bridge.request = request
        details = {}
        with patch.object(backend.aiohttp, "FormData", FormData, create=True):
            transcript = await bridge.transcribe(b"audio", "audio/wav", "de-DE", True, details=details)
        self.assertEqual(transcript, "Licht aus")
        self.assertEqual(bridge.entries[0]["diagnostics"], details)
        self.assertEqual(details["audio_bytes"], 5)
        self.assertEqual(details["audio_format"], "audio/wav")
        self.assertGreaterEqual(details["duration_ms"], details["requests"][0]["duration_ms"])

    async def test_tool_loop_dry_run_and_disabled_action(self):
        obj = self.bridge()
        requests = []
        async def request(kind, *, payload=None, form=None):
            requests.append(payload)
            if len(requests) == 1:
                return {"choices":[{"message":{"tool_calls":[{"id":"1", "type":"function", "function":{"name":"run_action", "arguments":'{"action":"turn_on","entity_ids":["light.room"],"parameters":{}}'}}]}}]}
            return {"choices":[{"message":{"content":"<think>private</think>Erledigt"}}], "usage": {"prompt_tokens": 128, "completion_tokens": 18, "total_tokens": 146, "provider_debug": "private"}}
        obj.request = request
        result = await obj.chat("Licht an", "de", "context", "user", dry_run=True)
        self.assertEqual(result["answer"], "Erledigt")
        self.assertEqual(result["tools"][0]["result"]["planned"], ["light.room"])
        self.assertEqual(obj.entries[0]["tools"][0]["parameters"], {})
        self.assertTrue(obj.entries[0]["test"])
        self.assertNotIn("unlock", str(requests[0]))
        self.assertNotIn("secret", str(requests[0]))
        details = result["diagnostics"]
        self.assertEqual(obj.entries[0]["diagnostics"], details)
        self.assertEqual(details["selection"]["status"], "matched")
        self.assertEqual(list(details["selection"]["action_parameters"]), ["turn_on"])
        self.assertEqual(details["selection"]["entities"][0]["id"], "light.room")
        self.assertEqual([item["tool_calls"] for item in details["requests"]], [1, 0])
        self.assertEqual(details["requests"][1]["usage"], {"prompt_tokens": 128, "completion_tokens": 18, "total_tokens": 146})
        self.assertGreaterEqual(details["duration_ms"], details["selection_ms"])
        self.assertGreaterEqual(result["tools"][0]["duration_ms"], 0)

    async def test_alias_and_room_send_only_intended_target_and_action(self):
        bridge = self.bridge()
        policy = Policy()
        policy.entities = [
            entity("light.dining_left", "Orange", "Esszimmer", "turn_on", "turn_off", "light_set", aliases=["Tischlampe"], area_aliases={"Essbereich"}),
            entity("light.office", "Orange", "Büro", "turn_on", "turn_off", "light_set", aliases=["Tischlampe"]),
            entity("switch.dining", "Heizdecke", "Esszimmer", "turn_on", "turn_off"),
        ]
        bridge.policy = lambda: policy

        async def request(_kind, *, payload):
            content = payload["messages"][0]["content"]
            self.assertIn("light.dining_left", content)
            for irrelevant in ("light.office", "switch.dining", "light_set", "turn_on"):
                self.assertNotIn(irrelevant, content)
            properties = payload["tools"][0]["function"]["parameters"]["properties"]
            self.assertEqual(properties["action"]["enum"], ["turn_off"])
            self.assertEqual(properties["entity_ids"]["items"]["enum"], ["light.dining_left"])
            return tool_response("turn_off", "light.office")

        bridge.request = request
        with self.assertRaisesRegex(backend.AuralisError, "nicht freigegebenen Werkzeugaufruf"):
            await bridge.chat("Schalte die Tischlampe im Essbereich aus", "de", "context", "user", dry_run=True)
        self.assertEqual(policy.calls, [])

    async def test_bulk_batches_skip_off_disabled_and_unrelated_lights(self):
        bridge = self.bridge()
        policy = Policy()
        targets = [f"light.living_{index}" for index in range(101)]
        policy.entities = [entity(target, f"Leiste {index}", "Wohnzimmer", "turn_off", "light_set") for index, target in enumerate(targets)]
        policy.entities += [entity("light.already_off", "Ausgeschaltet", "Wohnzimmer", "turn_off", state="off"),
                            entity("light.disabled", "Deaktiviert", "Wohnzimmer", "turn_on"),
                            entity("light.office", "Büroleuchte", "Büro", "turn_off"),
                            entity("light.group", "Lichtgruppe", "Wohnzimmer", "turn_off", group=True)]
        bridge.policy = lambda: policy
        requests = []

        async def request(_kind, *, payload):
            requests.append(payload["messages"][-1]["content"])
            self.assertNotIn("light.living_", payload["messages"][0]["content"])
            if len(requests) == 1:
                return tool_response("turn_off", backend.BULK_TARGET)
            return {"choices": [{"message": {"content": "Die Lampen würden ausgeschaltet."}}]}

        bridge.request = request
        result = await bridge.chat("Alle Lichter im Wohnzimmer aus", "de", "context", "user", dry_run=True)
        self.assertEqual([len(call[1]) for call in policy.calls], [100, 1])
        self.assertEqual(result["tools"][0]["result"]["planned"], targets)
        self.assertNotIn("light.living_", requests[1])
        self.assertIn('"planned": 101', requests[1])
        self.assertEqual(result["diagnostics"]["selection"]["resolved_targets"], targets)
        self.assertEqual(result["diagnostics"]["selection"]["entities"][0]["id"], backend.BULK_TARGET)

    def test_collective_room_light_command_and_explicit_single_lamp(self):
        catalog = [entity("light.bed_one", "Leselampe", "Schlafzimmer", "turn_off"),
                   entity("light.bed_two", "Deckenlampe", "Schlafzimmer", "turn_off"),
                   entity("light.bed_off", "Lichtband", "Schlafzimmer", "turn_off", state="off"),
                   entity("light.bed_disabled", "Stehlampe", "Schlafzimmer", "turn_on"),
                   entity("light.hall", "Wandlampe", "Flur", "turn_off")]
        for prompt in ("Schalt das Licht im Schlafzimmer aus", "Schalte im Schlafzimmer das Licht aus", "Turn off the light in the Schlafzimmer"):
            with self.subTest(prompt=prompt):
                _, actions, targets, status = backend.select_context(prompt, catalog)
                self.assertEqual(list(actions), ["turn_off"])
                self.assertEqual(targets, ["light.bed_one", "light.bed_two"])
                self.assertEqual(status, "matched")
        selected, _, targets, _ = backend.select_context("Schalt die Leselampe im Schlafzimmer aus", catalog)
        self.assertEqual([item["id"] for item in selected], ["light.bed_one"])
        self.assertEqual(targets, [])
        for prompt in ("Schalt das Licht im Schlafzimer aus", "Schalt das Licht neben dem Bett im Schlafzimmer aus", "Schalt die Lampe im Schlafzimmer aus"):
            self.assertEqual(backend.select_context(prompt, catalog)[1], {})

    def test_retrieval_status_and_compound_light_commands(self):
        catalog = [entity("light.first", "Ecke", "Wohnzimmer", "turn_on", "turn_off", "light_set"),
                   entity("light.second", "Fenster", "Wohnzimmer", "turn_off", "light_set", state="off"),
                   entity("light.third", "Ecke", "Küche", "turn_off"),
                   entity("cover.garage", "Hof", "Wohnzimmer", "open", device_class="garage")]
        for prompt in ("Licht aus", "Ecke aus", "Öffne die Rollos im Wohnzimmer", "Alle Lichter im Esszimer aus", "Schalte im Wohnzimmer alle Lampen außer Ecke aus", "Alle Lichter neben dem Sofa im Wohnzimmer aus", "Wohnzimmer aus", "Ecke an und aus", "Wie ist das Licht im Wohnzimmer?", "Erzähl mir einen Witz"):
            with self.subTest(prompt=prompt):
                self.assertEqual(backend.select_context(prompt, catalog)[1], {})
        _, actions, targets, _ = backend.select_context("Mach die Wohnzimmerlampen blau und stell sie auf 40 Prozent", catalog)
        self.assertEqual(list(actions), ["light_set"])
        self.assertEqual(targets, ["light.first", "light.second"])
        _, actions, targets, _ = backend.select_context("Alle Lichter aus", catalog)
        self.assertEqual(list(actions), ["turn_off"])
        self.assertEqual(targets, ["light.first", "light.third"])

if __name__ == "__main__": unittest.main()
