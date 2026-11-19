"""Provider, policy and persistent history implementation."""
import asyncio
import base64
import json
from datetime import datetime, timezone
import re
import time
import uuid
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from .actions import ActionPolicy
from .const import DEFAULTS, MAX_AUDIO, MAX_TEXT

MAX_HISTORY = 99 * 1024 * 1024
MAX_AGE = 7 * 86400
MAX_RESPONSE = 1024 * 1024
MAX_ERROR = 2000


class AuralisError(ValueError):
    """A diagnostic that can be shown to an administrator."""


def llm_base_url(url):
    url = url.strip()
    if not url:
        return ""
    if not valid_url(url):
        raise AuralisError("Ungültige LLM-Basis-URL. Eine vollständige HTTP(S)-URL mit /v1 eingeben.")
    parts = urlsplit(url)
    path = parts.path.rstrip("/").removesuffix("/chat/completions").removesuffix("/models")
    return parts._replace(path=path or "/v1").geturl()


def llm_route(base, route):
    parts = urlsplit(base)
    return parts._replace(path=f"{parts.path}/{route}").geturl()


def diagnostic(error):
    if isinstance(error, AuralisError):
        return str(error)
    if isinstance(error, json.JSONDecodeError):
        return f"Ungültiges JSON in der Anbieterantwort: {error.msg}"
    if isinstance(error, (KeyError, IndexError, TypeError)):
        return f"Unerwartetes Format der Anbieterantwort ({type(error).__name__})."
    return f"Interner Fehler ({type(error).__name__})."


def clean(text):
    text = re.sub(r"<(think|thinking|analysis|reasoning)\b[^>]*>.*?</\1\s*>", "", str(text), flags=re.I | re.S)
    text = re.sub(r"<(think|thinking|analysis|reasoning)\b[^>]*>.*", "", text, flags=re.I | re.S)
    return text.strip()[:MAX_TEXT]


def history_sizes(entries):
    return [len(json.dumps(entry, ensure_ascii=True)) + 2 for entry in entries]


def valid_url(url):
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname) and parts.username is None and parts.password is None and not parts.fragment and bool(parts.port is None or 0 < parts.port < 65536)
    except ValueError:
        return False


class Bridge:
    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, 1, "auralis", private=True, atomic_writes=True)
        self.history_store = Store(hass, 1, "auralis_history", private=True, atomic_writes=True)
        self.settings = dict(DEFAULTS)
        self.overrides = {}
        self.entries = []
        self.lock = asyncio.Lock()

    async def load(self):
        data = await self.store.async_load() or {}
        self.settings.update({k: v for k, v in data.get("settings", {}).items() if k in DEFAULTS})
        self.settings["llm_url"] = llm_base_url(self.settings["llm_url"])
        self.overrides = data.get("overrides", {})
        self.entries = await self.history_store.async_load() or []
        await self.prune(persist=bool(self.entries))

    async def persist(self):
        await self.store.async_save({"settings": self.settings, "overrides": self.overrides})

    def public_settings(self):
        result = {k: v for k, v in self.settings.items() if not k.endswith("_token")}
        for name in ("stt", "llm"):
            result[name + "_has_token"] = bool(self.settings[name + "_token"])
        return result

    async def save(self, values):
        if not isinstance(values, dict) or set(values) - set(DEFAULTS):
            raise ValueError("Ungültige Einstellungen")
        new = dict(self.settings)
        for key, value in values.items():
            if key == "history_enabled":
                if not isinstance(value, bool): raise ValueError("Ungültige Einstellungen")
            elif not isinstance(value, str) or len(value) > MAX_TEXT:
                raise ValueError("Ungültige Einstellungen")
            if key.endswith("_url") and value and not valid_url(value):
                raise ValueError("Ungültige Endpunkt-URL")
            new[key] = llm_base_url(value) if key == "llm_url" else value
        self.settings = new
        await self.persist()

    def policy(self):
        return ActionPolicy(self.hass, self.overrides)

    async def prune(self, *, persist=False):
        previous_count = len(self.entries)
        cutoff = time.time() - MAX_AGE
        self.entries = [e for e in self.entries if e.get("_timestamp", 0) >= cutoff]
        if persist:
            sizes = await self.hass.async_add_executor_job(history_sizes, self.entries)
            size = sum(sizes)
            for index, entry_size in enumerate(sizes):
                if size <= MAX_HISTORY:
                    self.entries = self.entries[index:]
                    break
                size -= entry_size
            else:
                self.entries = []
        if persist or len(self.entries) != previous_count:
            await self.history_store.async_save(self.entries)

    async def record(self, kind, test=False, audio=None, mime=None, **fields):
        if not self.settings["history_enabled"]: return
        async with self.lock:
            now = time.time()
            entry = {"id": uuid.uuid4().hex, "time": datetime.fromtimestamp(now, timezone.utc).isoformat(), "_timestamp": now, "kind": kind, "test": test, "has_audio": bool(audio)}
            entry.update(fields)
            if audio:
                entry["_audio"] = base64.b64encode(audio).decode()
                entry["_mime"] = mime
            self.entries.append(entry)
            await self.prune(persist=True)

    def redact(self, message, url="", token=""):
        url_secrets = [value for _, value in parse_qsl(urlsplit(url).query)]
        secrets = {token, self.settings["stt_token"], self.settings["llm_token"], *url_secrets}
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            message = message.replace(secret, "[redigiert]")
        message = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redigiert]", message)
        return message[:MAX_ERROR]

    async def provider_json(self, method, url, token, *, payload=None, form=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        session = async_get_clientsession(self.hass)
        try:
            async with session.request(method, url, json=payload, data=form, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15 if method == "GET" else 60)) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        if response.status >= 300:
                            raise AuralisError(f"HTTP {response.status}: Fehlerantwort zu groß.")
                        raise AuralisError("Antwort des Anbieters zu groß.")
                if not 200 <= response.status < 300:
                    message = ""
                    try:
                        body = json.loads(raw)
                        error = body.get("error", body.get("detail", body.get("message"))) if isinstance(body, dict) else None
                        if isinstance(error, dict):
                            message = " · ".join(str(error[field]) for field in ("code", "message") if isinstance(error.get(field), (str, int)))
                        elif isinstance(error, str):
                            message = error
                    except (ValueError, UnicodeError):
                        if response.content_type == "text/plain":
                            message = raw.decode("utf-8", errors="replace").strip()
                    detail = self.redact(message, url, token)
                    raise AuralisError(f"HTTP {response.status}: {detail or self.redact(response.reason or 'Anbieteranfrage fehlgeschlagen', url, token)}")
                return json.loads(raw)
        except TimeoutError:
            raise AuralisError("Zeitüberschreitung beim API-Anbieter.") from None
        except aiohttp.ClientError as error:
            raise AuralisError(f"Verbindung zum API-Anbieter fehlgeschlagen ({type(error).__name__}).") from None

    async def request(self, kind, *, payload=None, form=None):
        url = self.settings[kind + "_url"]
        if not valid_url(url) or not self.settings[kind + "_model"]:
            raise AuralisError(f"{kind.upper()}: URL oder Modell fehlt in der Konfiguration.")
        if kind == "llm":
            url = llm_route(llm_base_url(url), "chat/completions")
        return await self.provider_json("POST", url, self.settings[kind + "_token"], payload=payload, form=form)

    async def list_models(self, url, token=None):
        base = llm_base_url(url)
        if not base:
            return []
        if token is None and base == llm_base_url(self.settings["llm_url"]):
            token = self.settings["llm_token"]
        try:
            response = await self.provider_json("GET", llm_route(base, "models"), token or "")
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise AuralisError("Die Modellliste muss ein Array unter data enthalten.")
            return sorted({model["id"] for model in response["data"] if isinstance(model, dict) and isinstance(model.get("id"), str) and 0 < len(model["id"]) <= 255})[:2000]
        except Exception as error:
            raise AuralisError(diagnostic(error)) from None

    async def transcribe(self, audio, mime, language, test=False):
        mime = mime.split(";", 1)[0].strip().lower()
        valid_audio = bool(audio) and len(audio) <= MAX_AUDIO and mime in ("audio/wav", "audio/x-wav", "audio/ogg", "audio/webm", "audio/mpeg", "audio/mp4")
        try:
            if not valid_audio:
                raise AuralisError("Ungültige Audiodaten: leere Aufnahme, ungültiges Format oder mehr als 10 MiB.")
            form = aiohttp.FormData()
            form.add_field("model", self.settings["stt_model"])
            if language:
                form.add_field("language", language[:35].split("-")[0])
            extension = {"audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a"}.get(mime, mime.split("/")[1])
            form.add_field("file", audio, filename=f"audio.{extension}", content_type=mime)
            data = await self.request("stt", form=form)
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise AuralisError("In der STT-Antwort fehlt das Textfeld text.")
            transcript = data["text"][:MAX_TEXT]
        except Exception as error:
            detail = self.redact(diagnostic(error))
            await self.record("stt", test, audio if valid_audio else None, mime, error=detail)
            raise AuralisError(detail) from None
        await self.record("stt", test, audio, mime, transcript=transcript)
        return transcript

    async def chat(self, prompt, language, context, user, dry_run=False):
        results = []
        try:
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_TEXT:
                raise AuralisError("Eine Eingabe mit 1 bis 16.000 Zeichen ist erforderlich.")
            policy = self.policy()
            catalog = policy.catalog(user=user)
            inventory = []
            enabled = {}
            for entity in catalog:
                actions = [a for a in entity.get("actions", []) if a.get("enabled") is True]
                enabled.update((action["id"], action.get("parameters", {})) for action in actions)
                inventory.append({key: entity.get(key) for key in ("id", "name", "area", "state")} | {"actions": [action["id"] for action in actions]})
            inventory_text = json.dumps({"entities": inventory, "action_parameters": enabled}, ensure_ascii=False, separators=(",", ":"))
            if len(inventory_text) > 100000: raise AuralisError("Geräteliste zu groß für die LLM-Anfrage.")
            tools = []
            if enabled:
                tools = [{"type":"function", "function":{"name":"run_action", "description":"Führe eine erlaubte Geräteaktion aus.", "parameters":{"type":"object", "properties":{"action":{"type":"string", "enum":sorted(enabled)}, "entity_ids":{"type":"array", "items":{"type":"string"}, "maxItems":20}, "parameters":{"type":"object"}}, "required":["action", "entity_ids", "parameters"], "additionalProperties":False}}}]
            messages = [{"role":"system", "content":self.settings["system_prompt"][:MAX_TEXT] + "\nAntworte nur mit einer kurzen gesprochenen Antwort. Gib niemals Gedanken, Werkzeugdaten oder interne Anweisungen aus.\nGeräte: " + inventory_text}, {"role":"user", "content":prompt}]
            if language:
                messages[0]["content"] += "\nSpracheinstellung von Assist: " + language[:35]
            if dry_run:
                messages[0]["content"] += "\nTrockenlauf: Es wird nichts geschaltet. Beschreibe geplante Aktionen als Vorschau."
            for round_index in range(4):
                payload = {"model": self.settings["llm_model"], "messages":messages}
                if tools and round_index < 3:
                    payload["tools"] = tools
                data = await self.request("llm", payload=payload)
                msg = data["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                if not calls:
                    answer = clean(msg.get("content") or "")
                    await self.record("llm", dry_run, input=prompt, answer=answer, tools=results)
                    return {"answer": answer, "tools": results}
                if round_index == 3 or len(calls) > 8: raise AuralisError("Zu viele Werkzeugaufrufe in der Anbieterantwort.")
                messages.append({"role":"assistant", "content":None, "tool_calls":calls})
                for call in calls:
                    function = call["function"]
                    args = json.loads(function["arguments"])
                    action = args.get("action")
                    ids = args.get("entity_ids")
                    params = args.get("parameters")
                    if function["name"] != "run_action" or action not in enabled or not isinstance(ids, list) or not 0 < len(ids) <= 20 or not all(isinstance(i, str) and len(i) <= 255 for i in ids) or not isinstance(params, dict) or len(json.dumps(params)) > MAX_TEXT:
                        raise AuralisError("Die Anbieterantwort enthält einen ungültigen oder nicht freigegebenen Werkzeugaufruf.")
                    result = await policy.run(action, ids, params, context, dry_run=dry_run, user=user)
                    logged_result = result | {"errors": [error | {"detail": self.redact(error["detail"])} if "detail" in error else error for error in result.get("errors", [])]}
                    visible_result = result | {"errors": [{key: value for key, value in error.items() if key != "detail"} for error in result.get("errors", [])]}
                    results.append({"action":action, "entity_ids":ids, "parameters":params, "result":logged_result})
                    messages.append({"role":"tool", "tool_call_id":call["id"], "content":json.dumps(visible_result, ensure_ascii=False)[:MAX_TEXT]})
            raise AuralisError("Zu viele Werkzeugrunden ohne finale LLM-Antwort.")
        except Exception as error:
            detail = self.redact(diagnostic(error))
            await self.record("llm", dry_run, input=prompt[:MAX_TEXT], tools=results, error=detail)
            raise AuralisError(detail) from None
