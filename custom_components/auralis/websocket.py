"""Admin-only panel API."""
import base64
import binascii
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import Context
from .const import DOMAIN, MAX_AUDIO


def register(hass):
    for handler in (get, save, action, test_llm, test_stt, history, audio, clear):
        websocket_api.async_register_command(hass, handler)


def command(name, schema):
    def decorate(func):
        return websocket_api.websocket_command({vol.Required("type"): "auralis/" + name, **schema})(websocket_api.require_admin(websocket_api.async_response(func)))
    return decorate

async def execute(connection, msg, operation):
    try:
        result = await operation()
        connection.send_result(msg["id"], result)
    except (ValueError, TypeError, KeyError, binascii.Error):
        connection.send_error(msg["id"], "invalid_request", "Anfrage konnte nicht verarbeitet werden")
    except Exception:
        connection.send_error(msg["id"], "unknown_error", "Anfrage konnte nicht verarbeitet werden")

@command("get", {})
async def get(hass, connection, msg):
    async def run():
        bridge = hass.data[DOMAIN]
        return {"settings": bridge.public_settings(), "entities": bridge.policy().catalog(user=connection.user)}
    await execute(connection, msg, run)

@command("save", {vol.Required("settings"): dict})
async def save(hass, connection, msg):
    async def run():
        bridge = hass.data[DOMAIN]
        await bridge.save(msg["settings"])
        return {"settings": bridge.public_settings()}
    await execute(connection, msg, run)

@command("action", {vol.Required("entity_id"): str, vol.Required("action"): str, vol.Required("enabled"): bool})
async def action(hass, connection, msg):
    async def run():
        bridge = hass.data[DOMAIN]
        entity = next((e for e in bridge.policy().catalog(user=connection.user) if e["id"] == msg["entity_id"]), None)
        if not entity or not any(a["id"] == msg["action"] for a in entity["actions"]): raise ValueError()
        bridge.overrides[f'{msg["entity_id"]}:{msg["action"]}'] = msg["enabled"]
        await bridge.persist()
        return {}
    await execute(connection, msg, run)

@command("test_llm", {vol.Required("prompt"): str, vol.Optional("language", default="de"): str})
async def test_llm(hass, connection, msg):
    async def run():
        try:
            return await hass.data[DOMAIN].chat(msg["prompt"], msg["language"], Context(user_id=connection.user.id), connection.user, dry_run=True)
        except ValueError:
            return {"answer":"", "tools":[], "error":"Anfrage fehlgeschlagen"}
    await execute(connection, msg, run)

@command("test_stt", {vol.Required("audio"): str, vol.Required("mime"): str, vol.Optional("language", default="de"): str})
async def test_stt(hass, connection, msg):
    async def run():
        try:
            if len(msg["audio"]) > ((MAX_AUDIO + 2) // 3) * 4: raise ValueError()
            data = base64.b64decode(msg["audio"], validate=True)
            transcript = await hass.data[DOMAIN].transcribe(data, msg["mime"], msg["language"], True)
            return {"transcript":transcript}
        except (ValueError, binascii.Error):
            return {"transcript":"", "error":"Transkription fehlgeschlagen"}
    await execute(connection, msg, run)

@command("history", {vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=100)), vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0))})
async def history(hass, connection, msg):
    async def run():
        bridge = hass.data[DOMAIN]
        async with bridge.lock:
            await bridge.prune()
            entries = list(reversed(bridge.entries))
            return {"entries":[{k:v for k,v in e.items() if not k.startswith("_")} for e in entries[msg["offset"]:msg["offset"] + msg["limit"]]], "total":len(entries)}
    await execute(connection, msg, run)

@command("audio", {vol.Required("id"): str})
async def audio(hass, connection, msg):
    async def run():
        bridge = hass.data[DOMAIN]
        async with bridge.lock:
            await bridge.prune()
            entry = next(e for e in bridge.entries if e["id"] == msg["id"] and e.get("_audio"))
            return {"audio":entry["_audio"], "mime":entry["_mime"]}
    await execute(connection, msg, run)

@command("clear", {})
async def clear(hass, connection, msg):
    async def run():
        bridge = hass.data[DOMAIN]
        async with bridge.lock:
            bridge.entries = []
            await bridge.prune(persist=True)
        return {}
    await execute(connection, msg, run)
