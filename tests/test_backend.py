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
for name in ("aiohttp", "homeassistant", "homeassistant.helpers", "homeassistant.helpers.aiohttp_client", "homeassistant.helpers.storage"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = lambda hass: None
sys.modules["homeassistant.helpers.storage"].Store = object
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

    def catalog(self, user=None, *, for_retrieval=False, trace=None):
        return self.entities

    def preflight(self, action, entity_ids, parameters, context, *, user=None, expected_leaves=None):
        return {"executed": [], "planned": expected_leaves, "skipped": [], "errors": []}

    async def run(self, action, entity_ids, parameters, context, *, dry_run=False, user=None, expected_leaves=None):
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


def confirmation_response(**approval):
    import json
    return {"choices": [{"message": {"content": "Diese Antwort nicht vorlesen.", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "confirm_action", "arguments": json.dumps(approval)}}]}}]}


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
        if hasattr(backend, "ConversationMemory"):
            obj.memory = backend.ConversationMemory()
        obj.policy = lambda: Policy()
        return obj

    def test_urls_and_reasoning(self):
        self.assertTrue(backend.valid_url("http://localhost:1234/v1/chat/completions"))
        self.assertFalse(backend.valid_url("http://user:secret@host/path"))
        self.assertEqual(backend.clean("<think>secret</think>Hallo"), "Hallo")
        self.assertEqual(backend.clean("Hallo <thinking>secret"), "Hallo")
        self.assertEqual(backend.clean("Interne Notiz</think>Hallo"), "Hallo")

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

    async def test_reasoning_setting_applies_to_every_llm_round(self):
        bridge = self.bridge()
        policy = Policy()
        policy.entities = [entity("switch.room", "Room", None, "turn_on", state="off")]
        bridge.policy = lambda: policy
        payloads = []

        async def request(kind, *, payload):
            self.assertEqual(kind, "llm")
            payloads.append(payload)
            if len(payloads) == 1:
                return tool_response("turn_on", "switch.room")
            return {"choices": [{"message": {"content": "Erledigt"}}]}

        bridge.request = request
        self.assertEqual(bridge.public_settings()["llm_reasoning"], "default")
        for mode, expected in (("default", None), ("on", "low"), ("off", "none"), ("default", None)):
            with self.subTest(mode=mode):
                await bridge.save({"llm_reasoning": mode})
                self.assertEqual(bridge.store.data["settings"]["llm_reasoning"], mode)
                payloads.clear()
                await bridge.chat("Room an", "de", "context", "user", dry_run=True)
                self.assertEqual(len(payloads), 2)
                for payload in payloads:
                    self.assertEqual(payload.get("reasoning_effort"), expected)
                    self.assertEqual("reasoning_effort" in payload, expected is not None)
        with self.assertRaises(ValueError):
            await bridge.save({"llm_reasoning": "invalid"})

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
        prompt = "A" * 3500 + " secret2 private-query, Bearer credential; Ende."

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
                await bridge.chat(prompt, "de", "context", "user", dry_run=True, details=details)
        self.assertEqual(str(caught.exception), "HTTP 404: model_not_found · Unknown model; token [redigiert] and [redigiert]")
        self.assertEqual(bridge.entries[0]["error"], str(caught.exception))
        self.assertTrue(bridge.entries[0]["test"])
        self.assertEqual(bridge.entries[0]["diagnostics"], details)
        self.assertTrue(details["requests"][0]["failed"])
        request = details["requests"][0]["request"]
        self.assertEqual(request["endpoint"], "https://example.test/v1/chat/completions")
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["body"]["messages"][-1]["content"], "A" * 3500 + " [redigiert] [redigiert], Bearer [redigiert]; Ende.")
        self.assertEqual(bridge.entries[0]["input"], request["body"]["messages"][-1]["content"])
        self.assertNotIn("credential", str(details))
        self.assertNotIn("private-query", str(details))
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
        self.assertEqual(details["requests"][0]["request"]["body"], {
            "model": "whisper-1", "language": "de", "file": {"filename": "audio.wav", "content_type": "audio/wav", "bytes": 5},
        })
        self.assertGreaterEqual(details["duration_ms"], details["requests"][0]["duration_ms"])

    async def test_tool_loop_dry_run_and_disabled_action(self):
        obj = self.bridge()
        policy = Policy()
        policy.entities[0]["id"] = "switch.room"
        obj.policy = lambda: policy
        requests = []
        async def request(kind, *, payload=None, form=None):
            requests.append(payload)
            if len(requests) == 1:
                return {"choices":[{"message":{"reasoning_content":"Ich prüfe secret2 vor dem Aufruf", "tool_calls":[{"id":"1", "type":"function", "function":{"name":"run_action", "arguments":'{"action":"turn_on","entity_ids":["switch.room"],"parameters":{}}'}}]}}]}
            return {"choices":[{"message":{"content":"<think>private</think>Erledigt"}}], "usage": {"prompt_tokens": 128, "completion_tokens": 18, "total_tokens": 146, "provider_debug": "private"}}
        obj.request = request
        result = await obj.chat("Room an", "de", "context", "user", dry_run=True)
        self.assertEqual(result["answer"], "Erledigt")
        self.assertEqual(result["tools"][0]["result"]["planned"], ["switch.room"])
        self.assertEqual(obj.entries[0]["tools"][0]["parameters"], {})
        self.assertTrue(obj.entries[0]["test"])
        self.assertNotIn("unlock", str(requests[0]))
        self.assertNotIn("secret", str(requests[0]))
        details = result["diagnostics"]
        self.assertEqual(obj.entries[0]["diagnostics"], details)
        self.assertEqual(details["selection"]["status"], "matched")
        self.assertEqual(list(details["selection"]["action_parameters"]), ["turn_on"])
        self.assertEqual(details["selection"]["entities"][0]["id"], "switch.room")
        self.assertEqual([item["tool_calls"] for item in details["requests"]], [1, 0])
        self.assertEqual(details["requests"][1]["usage"], {"prompt_tokens": 128, "completion_tokens": 18, "total_tokens": 146})
        self.assertEqual(details["requests"][0]["reasoning"], "Ich prüfe [redigiert] vor dem Aufruf")
        self.assertEqual(details["requests"][1]["reasoning"], "private")
        first, second = [item["request"]["body"] for item in details["requests"]]
        self.assertEqual([message["role"] for message in first["messages"]], ["system", "user"])
        self.assertEqual(first["tools"], requests[0]["tools"])
        self.assertEqual([message["role"] for message in second["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual([details["requests"][0]["sequence"], result["tools"][0]["sequence"], details["requests"][1]["sequence"]], [1, 2, 3])
        self.assertNotIn("Ich prüfe", str(requests[1]["messages"]))
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
            function = payload["tools"][0]["function"]
            self.assertEqual(function["name"], "confirm_action")
            self.assertEqual(function["parameters"]["properties"], {"approved": {"type": "boolean"}})
            return tool_response("turn_off", "light.office")

        bridge.request = request
        with self.assertRaisesRegex(backend.AuralisError, "keinen gültigen Bestätigungsaufruf"):
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
            requests.append(payload)
            self.assertNotIn("light.living_", payload["messages"][0]["content"])
            return confirmation_response(approved=True)

        bridge.request = request
        result = await bridge.chat("Alle Lichter im Wohnzimmer aus", "de", "context", "user", dry_run=True)
        self.assertEqual([len(call[1]) for call in policy.calls], [100, 1])
        self.assertEqual(result["tools"][0]["result"]["planned"], targets)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["answer"], "101 Lampen würden ausgeschaltet. Nicht alle Ziele waren schaltbar.")
        self.assertEqual(result["diagnostics"]["confirmation"], {"action": "turn_off", "state": "approved"})
        self.assertEqual(result["diagnostics"]["selection"]["resolved_targets"], targets)
        self.assertEqual(result["diagnostics"]["selection"]["entities"][0]["id"], backend.BULK_TARGET)

    async def test_full_context_validates_action_entity_pair(self):
        bridge = self.bridge()
        bridge.settings["llm_full_context"] = True
        policy = Policy()
        policy.entities = [entity("switch.one", "One", None, "turn_on"), entity("fan.two", "Two", None, "turn_off")]
        bridge.policy = lambda: policy
        async def request(_kind, *, payload):
            self.assertIn("fan.two", payload["messages"][0]["content"])
            return tool_response("turn_on", "fan.two")
        bridge.request = request
        with self.assertRaisesRegex(backend.AuralisError, "ungültigen"):
            await bridge.chat("turn on One", "en", "context", "user", dry_run=True)
        self.assertEqual(policy.calls, [])

    async def test_context_budget_settings_and_compact_profiles(self):
        bridge = self.bridge()
        for value in (True, 100000, "8000"):
            with self.assertRaises(ValueError):
                await bridge.save({"llm_context_budget": value})
        await bridge.save({"llm_context_budget": 16000})
        policy = Policy()
        schema = {"type": "object", "properties": {"brightness": {"type": "integer", "minimum": 0, "maximum": 255}}}
        policy.entities = [entity(f"light.l{i}", f"Lamp {i}", "Room", "light_set") for i in range(100)]
        for item in policy.entities:
            item["actions"][0]["parameters"] = schema
        policy.entities.append(entity("sensor.disabled", "Disabled", None))
        bridge.policy = lambda: policy
        bridge.settings["llm_full_context"] = True
        async def request(_kind, *, payload):
            import json
            text = payload["messages"][0]["content"]
            self.assertEqual(text.count('"brightness"'), 1)
            self.assertNotIn("sensor.disabled", text)
            self.assertEqual(payload["max_tokens"], 768)
            self.assertLess(len(text), len(json.dumps(policy.entities, ensure_ascii=False)) * 0.65)
            return {"choices": [{"message": {"content": "OK"}}]}
        bridge.request = request
        result = await bridge.chat("What devices are available?", "en", "context", "user", dry_run=True)
        self.assertFalse(result["diagnostics"]["context_budget"]["fallback"])

    async def test_full_overflow_falls_back_without_full_authority(self):
        bridge = self.bridge()
        bridge.settings["llm_full_context"] = True
        policy = Policy()
        policy.entities = [entity(f"fan.device_{i}", "Device " + str(i) + "x" * 50, "Room", "turn_on") for i in range(500)]
        bridge.policy = lambda: policy
        async def request(_kind, *, payload):
            self.assertNotIn("tools", payload)
            self.assertNotIn("fan.device_499", payload["messages"][0]["content"])
            return tool_response("turn_on", "fan.device_499")
        bridge.request = request
        details = {}
        with self.assertRaises(backend.AuralisError):
            await bridge.chat("Start the breeze device", "en", "context", "user", dry_run=True, details=details)
        self.assertTrue(details["context_budget"]["fallback"])
        self.assertFalse(policy.calls)

    async def test_full_profiles_keep_fan_and_light_parameters_and_pairing(self):
        import json
        bridge = self.bridge()
        bridge.settings["llm_full_context"] = True
        policy = Policy()
        light = entity("light.desk", "Desk", "Office", "light_set")
        fan = entity("fan.desk", "Fan", "Office", "fan_set")
        light["actions"][0]["parameters"] = {"properties": {"brightness": {"type": "integer"}}}
        fan["actions"][0]["parameters"] = {"properties": {"percentage": {"type": "integer"}}}
        policy.entities = [light, fan]
        bridge.policy = lambda: policy
        async def request(_kind, *, payload):
            content = payload["messages"][0]["content"]
            catalog = json.loads(content.split("Geräte: ", 1)[1].split("\n", 1)[0])
            rows = {row[0]: row for row in catalog["entities"]}
            for entity_id, action, parameter in (("light.desk", "light_set", "brightness"), ("fan.desk", "fan_set", "percentage")):
                profile = catalog["profiles"][rows[entity_id][4]]
                self.assertIn(parameter, catalog["schemas"][profile[action]]["properties"])
            return tool_response("light_set", "fan.desk")
        bridge.request = request
        with self.assertRaises(backend.AuralisError):
            await bridge.chat("Adjust the device", "en", "context", "user", dry_run=True)
        self.assertFalse(policy.calls)

    async def test_full_overflow_preserves_fixed_bulk_targets(self):
        bridge = self.bridge()
        bridge.settings["llm_full_context"] = True
        policy = Policy()
        targets = [entity(f"light.l{i}", "Lamp " + str(i), None, "turn_off") for i in range(3)]
        policy.entities = targets + [entity(f"fan.f{i}", "Fan " + str(i) + "x" * 60, None, "turn_on") for i in range(500)]
        bridge.policy = lambda: policy
        async def request(_kind, *, payload):
            self.assertEqual(payload["tools"][0]["function"]["name"], "confirm_action")
            return confirmation_response(approved=True)
        bridge.request = request
        result = await bridge.chat("Turn off all lights", "en", "context", "user", dry_run=True)
        self.assertTrue(result["diagnostics"]["context_budget"]["fallback"])
        self.assertEqual(policy.calls[0][1], [item["id"] for item in targets])

    async def test_output_limit_does_not_execute_tool_calls(self):
        bridge = self.bridge()
        policy = Policy()
        policy.entities = [entity("switch.room", "Room", None, "turn_on", state="off")]
        bridge.policy = lambda: policy

        async def request(_kind, *, payload):
            response = tool_response("turn_on", "switch.room")
            response["choices"][0]["finish_reason"] = "length"
            return response

        bridge.request = request
        with self.assertRaisesRegex(backend.AuralisError, "Ausgabelimit"):
            await bridge.chat("Room an", "de", "context", "user", dry_run=True)
        self.assertEqual(policy.calls, [])

    async def test_followup_budget_blocks_second_request(self):
        bridge = self.bridge()
        policy = Policy()
        policy.entities = [entity("switch.room", "Room", None, "turn_on")]
        bridge.policy = lambda: policy
        calls = 0
        async def request(_kind, *, payload):
            nonlocal calls
            calls += 1
            response = tool_response("turn_on", "switch.room")
            response["choices"][0]["message"]["tool_calls"][0]["extra"] = "x" * 16000
            return response
        bridge.request = request
        details = {}
        with self.assertRaisesRegex(backend.AuralisError, "Kontextbudget"):
            await bridge.chat("Room on", "en", "context", "user", dry_run=True, details=details)
        self.assertEqual(calls, 1)
        self.assertEqual(len(policy.calls), 1)
        self.assertEqual(len(details["context_budget"]["rounds"]), 2)

    async def test_full_catalog_is_present_without_granting_tools(self):
        bridge = self.bridge()
        bridge.settings["llm_full_context"] = True
        policy = Policy()
        policy.entities = [entity("light.room", "Desk Lamp", "Room", "turn_off"), entity("fan.obscure", "Obscure", None, "turn_on")]
        bridge.policy = lambda: policy
        async def request(_kind, *, payload):
            content = payload["messages"][0]["content"]
            self.assertIn('"fan.obscure"', content)
            self.assertIn('"turn_on"', content)
            self.assertIn('"local_selection"', content)
            if payload["messages"][1]["content"].startswith("Which"):
                self.assertNotIn("tools", payload)
                return {"choices": [{"message": {"content": "Lamp is on"}}]}
            self.assertEqual(payload["tools"][0]["function"]["name"], "confirm_action")
            return confirmation_response(approved=True)
        bridge.request = request
        await bridge.chat("Which lights are on?", "en", "context", "user", dry_run=True)
        await bridge.chat("Turn off Desk Lamp", "en", "context", "user", dry_run=True)
        self.assertEqual(len(policy.calls), 1)

    async def test_full_unmatched_request_cannot_use_catalog_pair(self):
        bridge = self.bridge()
        bridge.settings["llm_full_context"] = True
        policy = Policy()
        policy.entities = [entity("fan.hidden", "Hidden", None, "turn_on")]
        bridge.policy = lambda: policy
        async def request(_kind, *, payload):
            self.assertIn('"fan.hidden"', payload["messages"][0]["content"])
            self.assertNotIn("tools", payload)
            return tool_response("turn_on", "fan.hidden")
        bridge.request = request
        with self.assertRaises(backend.AuralisError):
            await bridge.chat("Start the breeze device", "en", "context", "user", dry_run=True)
        self.assertFalse(policy.calls)

    async def test_full_context_only_executes_local_action_and_target(self):
        bridge = self.bridge()
        bridge.settings["llm_full_context"] = True
        policy = Policy()
        policy.entities = [entity("switch.one", "One", None, "turn_on", "turn_off"),
                           entity("switch.two", "Two", None, "turn_on")]
        bridge.policy = lambda: policy
        for action, target in (("turn_on", "switch.two"), ("turn_off", "switch.one")):
            async def invalid(_kind, *, payload):
                props = payload["tools"][0]["function"]["parameters"]["properties"]
                self.assertEqual(props["action"]["enum"], ["turn_on"])
                self.assertEqual(props["entity_ids"]["items"]["enum"], ["switch.one"])
                return tool_response(action, target)
            bridge.request = invalid
            with self.assertRaises(backend.AuralisError):
                await bridge.chat("Turn on One", "en", "context", "user", dry_run=True)
            self.assertFalse(policy.calls)

        requests = 0
        async def valid(_kind, *, payload):
            nonlocal requests
            requests += 1
            if requests == 1:
                return tool_response("turn_on", "switch.one")
            return {"choices": [{"message": {"content": "Done"}}]}
        bridge.request = valid
        await bridge.chat("Turn on One", "en", "context", "user", dry_run=True)
        self.assertEqual(policy.calls[0][0:2], ("turn_on", ["switch.one"]))

    async def test_full_read_only_rejects_unoffered_tool(self):
        bridge = self.bridge()
        bridge.settings["llm_full_context"] = True
        policy = Policy()
        policy.entities = [entity("switch.one", "One", None, "turn_on")]
        bridge.policy = lambda: policy
        async def request(_kind, *, payload):
            self.assertNotIn("tools", payload)
            return tool_response("turn_on", "switch.one")
        bridge.request = request
        with self.assertRaises(backend.AuralisError):
            await bridge.chat("Is One on?", "en", "context", "user", dry_run=True)
        self.assertFalse(policy.calls)

    async def test_nonlight_bulk_uses_fixed_scope_and_batches(self):
        bridge = self.bridge()
        policy = Policy()
        policy.entities = [entity(f"switch.s{i}", f"Switch {i}", None, "turn_off") for i in range(101)]
        policy.entities.append(entity("switch.group", "Group", None, "turn_off", group=True))
        bridge.policy = lambda: policy
        requests = 0
        async def request(_kind, *, payload):
            nonlocal requests
            requests += 1
            if requests == 1:
                return tool_response("turn_off", backend.BULK_TARGET)
            return {"choices": [{"message": {"content": "Done"}}]}
        bridge.request = request
        result = await bridge.chat("Turn off all switches", "en", "context", "user", dry_run=True)
        self.assertEqual([len(call[1]) for call in policy.calls], [100, 1])
        self.assertEqual(result["diagnostics"]["selection"]["matches"]["skipped_groups"], ["switch.group"])

    def test_ambiguous_context_keeps_candidates_without_actions(self):
        catalog = [entity("fan.one", "Fan one", "Kitchen", "turn_off"), entity("fan.two", "Fan two", "Kitchen", "turn_off")]
        candidates, actions, targets, status = backend.select_context("Turn off the fan near the Kitchen", catalog, language="en")
        self.assertEqual(len(candidates), 2)
        self.assertEqual((actions, targets, status), ({}, [], "clarify"))

    def test_global_bulk_scope_and_read_only_recall(self):
        catalog = [entity(f"light.l{i}", f"Device {i}", None if i % 2 else "Room", "turn_off", state="off" if i == 0 else "on") for i in range(12)]
        trace = {}
        _, _, targets, status = backend.select_context("schalte alle lampen aus", catalog, trace=trace)
        self.assertEqual(status, "matched")
        self.assertEqual(len(targets), 11)
        self.assertEqual(len(trace["scope"]), 12)
        self.assertEqual(trace["scope"][0]["status"], "already_set")
        selected, actions, _, _ = backend.select_context("Welche Lampen sind an?", catalog)
        self.assertEqual(len(selected), 12)
        self.assertFalse(actions)

    def test_bulk_never_expands_unknown_rooms_or_device_qualifiers(self):
        catalog = [entity("light.one", "Deckenlampe", "Küche", "turn_off"),
                   entity("light.two", "Stehlampe", "Flur", "turn_off"),
                   entity("fan.one", "Ventilator", "Küche", "turn_off")]
        for prompt in ("Schalte alle Lampen in Garage aus", "Schalte alle roten Lampen aus", "Turn off all fans in Garage"):
            with self.subTest(prompt=prompt):
                _, actions, targets, status = backend.select_context(prompt, catalog)
                self.assertEqual((actions, targets, status), ({}, [], "clarify"))
        self.assertEqual(backend.select_context("Schalte alle Lampen aus im ganzen Haus", catalog)[2], ["light.one", "light.two"])
        self.assertEqual(backend.select_context("Schalte alle Ventilatoren in Küche aus", catalog)[2], ["fan.one"])

    def test_collective_room_light_command_and_explicit_single_lamp(self):
        catalog = [entity("light.bed_one", "Leselampe", "Schlafzimmer", "turn_off"),
                   entity("light.bed_two", "Deckenlampe", "Schlafzimmer", "turn_off"),
                   entity("light.bed_off", "Lichtband", "Schlafzimmer", "turn_off", state="off"),
                   entity("light.bed_disabled", "Stehlampe", "Schlafzimmer", "turn_on"),
                   entity("light.hall", "Wandlampe", "Flur", "turn_off")]
        for prompt in ("Schalt das Licht im Schlafzimmer aus", "Schalte im Schlafzimmer das Licht aus", "Turn off the light in the Schlafzimmer", "Kannst du bitte das Licht im Schlafzimmer ausmachen?", "Könntest du das Licht im Schlaf Zimmer aus machen?", "Schalt das Licht im Schlafzimer aus", "Schlafzimmer Licht aus"):
            with self.subTest(prompt=prompt):
                _, actions, targets, status = backend.select_context(prompt, catalog)
                self.assertEqual(list(actions), ["turn_off"])
                self.assertEqual(targets, ["light.bed_one", "light.bed_two"])
                self.assertEqual(status, "matched")
        selected, _, targets, _ = backend.select_context("Schalt die Leselampe im Schlafzimmer aus", catalog)
        self.assertEqual([item["id"] for item in selected], ["light.bed_one"])
        self.assertEqual(targets, [])
        for prompt in ("Schalt das Licht im Garten aus", "Schalt die Leselampe im Garten aus", "Schalt das Licht neben dem Bett im Schlafzimmer aus", "Schalt die Lampe im Schlafzimmer aus", "Schalt die Leselampe um 22 Uhr aus", "Schalt die Leselampe aus, wenn ich einschlafe"):
            self.assertEqual(backend.select_context(prompt, catalog)[1], {})

    def test_forgiving_names_are_ranked_scoped_and_auditable(self):
        catalog = [entity("light.bed_main", "Deckenlampe", "Schlafzimmer", "turn_off"),
                   entity("light.hall_main", "Deckenlampe", "Flur", "turn_off"),
                   entity("light.phokus", "Phokuslicht", "Schlafzimmer", "turn_off"),
                   entity("light.vokus", "Vokuslicht", "Flur", "turn_off"),
                   entity("light.one", "Leselampe 1", "Schlafzimmer", "turn_off")]
        for prompt, target, method in (
            ("Schalt die Decken Lampe im Schlaf Zimmer aus", "light.bed_main", "word_join"),
            ("Schalt die Deckenlame im Schlafzimer aus", "light.bed_main", "edit_distance"),
            ("Schalt das Fokuslicht im Schlaf Zimmer aus", "light.phokus", "phonetic"),
            ("Schalt das Phokuslicht aus", "light.phokus", "exact"),
        ):
            with self.subTest(prompt=prompt):
                trace = {}
                selected, actions, _, _ = backend.select_context(prompt, catalog, trace=trace)
                self.assertEqual(list(actions), ["turn_off"])
                self.assertEqual([item["id"] for item in selected], [target])
                self.assertEqual(trace["names"][0]["method"], method)
        for prompt in ("Schalt die Decken Lame aus", "Schalt das Fokuslicht aus", "Schalt die Lese Lampe 2 aus"):
            with self.subTest(prompt=prompt):
                self.assertFalse(backend.select_context(prompt, catalog)[1])

    def test_fuzzy_names_do_not_enable_sensitive_actions(self):
        garage = entity("cover.phokustor", "Phokustor", "Garage", "open", device_class="garage")
        garage["actions"][0]["sensitive"] = True
        self.assertEqual(list(backend.select_context("Öffne das Phokustor", [garage])[1]), ["open"])
        self.assertFalse(backend.select_context("Öffne das Fokustor", [garage])[1])

    def test_short_room_normalization_and_ambiguous_sound_alikes(self):
        catalog = [entity("light.kitchen", "Leuchte A", "Küche", "turn_off"),
                   entity("light.focus_one", "Leuchte B", "Phokusraum", "turn_off"),
                   entity("light.focus_two", "Leuchte C", "Vokusraum", "turn_off")]
        self.assertEqual(backend.select_context("Schalt das Licht in der Kueche aus", catalog)[2], ["light.kitchen"])
        self.assertEqual(backend.select_context("Schalt das Licht im Fokusraum aus", catalog)[3], "clarify")

    async def test_room_request_from_stt_reaches_dry_run_as_single_tool(self):
        bridge = self.bridge()
        policy = Policy()
        policy.entities = [entity("light.bed_main", "Deckenlampe", "Schlafzimmer", "turn_off", area_id="bedroom"),
                           entity("light.bed_side", "Leselampe", "Schlafzimmer", "turn_off", area_id="bedroom"),
                           entity("light.hall_main", "Deckenlampe", "Flur", "turn_off", area_id="hall")]
        bridge.policy = lambda: policy

        async def request(_kind, *, payload):
            self.assertFalse(policy.calls)
            self.assertIn('"prepared_action":"turn_off"', payload["messages"][0]["content"])
            self.assertIn('"area":"Schlafzimmer"', payload["messages"][0]["content"])
            self.assertEqual(payload["tools"][0]["function"]["name"], "confirm_action")
            return confirmation_response(approved=True)

        bridge.request = request
        result = await bridge.chat("Kannst du bitte das Licht im Schlaf Zimmer aus machen?", "de", "context", "user", dry_run=True)
        self.assertEqual(policy.calls, [("turn_off", ["light.bed_main", "light.bed_side"], {})])
        self.assertEqual(result["diagnostics"]["selection"]["matches"]["rooms"], [{"id": "bedroom", "heard": "schlaf zimmer", "method": "word_join"}])

    async def test_colloquial_room_command_and_already_off_from_screenshots(self):
        bridge = self.bridge()
        policy = Policy()
        desk = entity("light.schreibtisch_lampe", "Schreibtischlampe", "Schlafzimmer Theo", "turn_off", "turn_on", state="off")
        ceiling = entity("light.deckenlampe", "Deckenlampe", "Schlafzimmer Theo", "turn_off")
        policy.entities = [desk, ceiling, entity("light.hall", "Flurlampe", "Flur", "turn_off")]
        bridge.policy = lambda: policy
        payloads = []

        async def request(_kind, *, payload):
            payloads.append(payload)
            return confirmation_response(approved=True)

        bridge.request = request
        room_prompt = "Mach ma licht in Schlafzimmer Theo aus."
        result = await bridge.chat(room_prompt, "de", "context", "user", dry_run=True)
        self.assertEqual(result["diagnostics"]["selection"]["resolved_targets"], [ceiling["id"]])
        self.assertEqual(policy.calls, [("turn_off", [ceiling["id"]], {})])
        ceiling["state"] = "off"
        for prompt, answer in (("Mach die Schreibtischlampe aus.", "Die Lampe ist schon aus."),
                               (room_prompt, "Die Lampen sind schon aus.")):
            with self.subTest(prompt=prompt):
                result = await bridge.chat(prompt, "de", "context", "user", dry_run=True)
                self.assertEqual(result["answer"], answer)
                self.assertEqual(result["diagnostics"]["selection"]["status"], "already_set")
                self.assertEqual(result["diagnostics"]["requests"], [])
        self.assertEqual(len(payloads), 1)
        self.assertEqual(len(policy.calls), 1)

        result = await bridge.chat("Mach die Schreibtisch schlampe an.", "de", "context", "user", dry_run=True)
        selection = result["diagnostics"]["selection"]
        self.assertEqual(selection["matches"]["names"][0]["method"], "phonetic")
        self.assertEqual(policy.calls[-1], ("turn_on", [desk["id"]], {}))

    async def test_prepared_actions_require_a_boolean_and_an_unchanged_plan(self):
        for response, state in (
            (confirmation_response(approved=False), "rejected"),
            ({"choices": [{"message": {"content": "Ja, erledigt"}}]}, "missing"),
            (confirmation_response(approved="true"), "invalid"),
            (confirmation_response(approved=True, entity_ids=["light.other"]), "invalid"),
        ):
            with self.subTest(state=state, response=response):
                bridge = self.bridge()
                policy = Policy()
                bridge.policy = lambda: policy

                async def request(_kind, *, payload):
                    return response

                bridge.request = request
                if state == "invalid":
                    with self.assertRaisesRegex(backend.AuralisError, "nur bestätigen oder ablehnen"):
                        await bridge.chat("Licht an", "de", "context", "user", dry_run=True)
                else:
                    result = await bridge.chat("Licht an", "de", "context", "user", dry_run=True)
                    self.assertIn("nicht bestätigen", result["answer"])
                self.assertEqual(bridge.entries[0]["diagnostics"]["confirmation"]["state"], state)
                self.assertEqual(policy.calls, [])

    def test_german_cologne_phonetics_not_used_for_english(self):
        catalog = [entity("light.study", "Schröder", None, "turn_off")]
        trace = {}
        chosen, actions, _, _ = backend.select_context("Schalt Schroeter aus", catalog, language="de-DE", trace=trace)
        self.assertEqual(chosen[0]["id"], "light.study")
        self.assertEqual(list(actions), ["turn_off"])
        self.assertEqual(trace["names"][0]["method"], "phonetic")
        self.assertFalse(backend.select_context("Turn off Schroeter", catalog, language="en-GB")[1])

    async def test_duplicate_untagged_reasoning_is_not_spoken(self):
        bridge = self.bridge()

        async def request(_kind, *, payload):
            return {"choices": [{"message": {"reasoning": "Interne Notiz", "content": "Interne Notiz\nHallo!"}}]}

        bridge.request = request
        result = await bridge.chat("Hallo", "de", "context", "user", dry_run=True)
        self.assertEqual(result["answer"], "Hallo!")
        self.assertEqual(result["diagnostics"]["requests"][0]["reasoning"], "Interne Notiz")

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
