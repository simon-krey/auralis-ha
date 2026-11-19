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
from .actions import ActionPolicy, MAX_ACTION_TARGETS
from .const import DEFAULTS, MAX_AUDIO, MAX_TEXT
from .retrieval import BULK_TARGET, select_context

MAX_HISTORY = 99 * 1024 * 1024
MAX_AGE = 7 * 86400
MAX_RESPONSE = 1024 * 1024
MAX_ERROR = 2000
_REASONING = re.compile(r"<(think|thinking|analysis|reasoning)\b[^>]*>(.*?)(?:</\1\s*>|$)", re.I | re.S)
_ORPHAN_REASONING_END = re.compile(r"^.*</(?:think|thinking|analysis|reasoning)\s*>", re.I | re.S)


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
    text = _ORPHAN_REASONING_END.sub("", _REASONING.sub("", str(text)))
    return text.strip()[:MAX_TEXT]


def provider_reasoning(message):
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    snippets = [match[2].strip() for match in _REASONING.finditer(content)]
    orphan = _ORPHAN_REASONING_END.search(_REASONING.sub("", content))
    if orphan:
        snippets.append(orphan[0].rsplit("</", 1)[0].strip())
    return "\n".join(filter(None, snippets))


def history_sizes(entries):
    return [len(json.dumps(entry, ensure_ascii=True)) + 2 for entry in entries]


def elapsed_ms(started):
    return round((time.perf_counter() - started) * 1000)


def switch_response(action, result, *, dry_run, english):
    count = len(result["planned" if dry_run else "executed"])
    missed = len(result["skipped"]) + len(result["errors"])
    if not count:
        return "I couldn't switch any of the lights." if english else "Ich konnte keine der Lampen schalten."
    if english:
        state = "on" if action == "turn_on" else "off"
        return f"{count} {'light' if count == 1 else 'lights'} {'would be' if dry_run else 'were' if count != 1 else 'was'} turned {state}." + (" Some targets couldn't be switched." if missed else "")
    subject = "Eine Lampe" if count == 1 else f"{count} Lampen"
    verb = ("würde" if count == 1 else "würden") if dry_run else ("wurde" if count == 1 else "wurden")
    state = "eingeschaltet" if action == "turn_on" else "ausgeschaltet"
    return f"{subject} {verb} {state}." + (" Nicht alle Ziele waren schaltbar." if missed else "")


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

    def redact(self, message, url="", token="", *, limit=MAX_ERROR):
        url_secrets = [value for _, value in parse_qsl(urlsplit(url).query)]
        secrets = {token, self.settings["stt_token"], self.settings["llm_token"], *url_secrets}
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            message = message.replace(secret, "[redigiert]")
        message = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redigiert]", message)
        return message[:limit]

    def request_snapshot(self, kind, body):
        url = self.settings[kind + "_url"]
        endpoint = ""
        if valid_url(url):
            endpoint = llm_route(llm_base_url(url), "chat/completions") if kind == "llm" else url
            endpoint = urlsplit(endpoint)._replace(query="").geturl()

        def snapshot(value):
            if isinstance(value, dict):
                return {key: snapshot(item) for key, item in value.items()}
            if isinstance(value, list):
                return [snapshot(item) for item in value]
            return self.redact(value, url=url if endpoint else "", limit=None) if isinstance(value, str) else value

        return snapshot({"method": "POST", "endpoint": endpoint, "body": body})

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

    async def timed_request(self, kind, details, origin, *, sequence=1, form_fields=None, **kwargs):
        started = time.perf_counter()
        request = {"started_ms": round((started - origin) * 1000), "sequence": sequence}
        request["request"] = self.request_snapshot(kind, kwargs.get("payload", form_fields))
        if kind == "llm":
            request["offered_tools"] = bool(kwargs["payload"].get("tools"))
            request["reasoning"] = None
        details["requests"].append(request)
        try:
            data = await self.request(kind, **kwargs)
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict):
                counts = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if type(usage.get(key)) is int and 0 <= usage[key] < 2**31}
                if counts:
                    request["usage"] = counts
            return data
        except Exception:
            request["failed"] = True
            raise
        finally:
            request["duration_ms"] = elapsed_ms(started)

    async def transcribe(self, audio, mime, language, test=False, *, details=None):
        started = time.perf_counter()
        details = {} if details is None else details
        mime = mime.split(";", 1)[0].strip().lower()
        valid_audio = bool(audio) and len(audio) <= MAX_AUDIO and mime in ("audio/wav", "audio/x-wav", "audio/ogg", "audio/webm", "audio/mpeg", "audio/mp4")
        details.update(model=self.redact(self.settings["stt_model"]), language=(language or "")[:35], audio_bytes=len(audio), audio_format=mime[:100], requests=[])
        try:
            if not valid_audio:
                raise AuralisError("Ungültige Audiodaten: leere Aufnahme, ungültiges Format oder mehr als 10 MiB.")
            form = aiohttp.FormData()
            fields = {"model": self.settings["stt_model"]}
            if language:
                fields["language"] = language[:35].split("-")[0]
            for key, value in fields.items():
                form.add_field(key, value)
            extension = {"audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a"}.get(mime, mime.split("/")[1])
            form.add_field("file", audio, filename=f"audio.{extension}", content_type=mime)
            fields["file"] = {"filename": f"audio.{extension}", "content_type": mime, "bytes": len(audio)}
            data = await self.timed_request("stt", details, started, form=form, form_fields=fields)
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise AuralisError("In der STT-Antwort fehlt das Textfeld text.")
            transcript = data["text"][:MAX_TEXT]
        except Exception as error:
            detail = self.redact(diagnostic(error))
            details["duration_ms"] = elapsed_ms(started)
            await self.record("stt", test, audio if valid_audio else None, mime, error=detail, diagnostics=details)
            raise AuralisError(detail) from None
        details["duration_ms"] = elapsed_ms(started)
        await self.record("stt", test, audio, mime, transcript=transcript, diagnostics=details)
        return transcript

    async def chat(self, prompt, language, context, user, dry_run=False, *, details=None):
        started = time.perf_counter()
        details = {} if details is None else details
        details.update(model=self.redact(self.settings["llm_model"]), language=(language or "")[:35], requests=[])
        results = []
        history_input = self.redact(prompt[:MAX_TEXT], url=self.settings["llm_url"], limit=None) if isinstance(prompt, str) else ""

        async def finish(answer):
            details["duration_ms"] = elapsed_ms(started)
            await self.record("llm", dry_run, input=history_input, answer=answer, tools=results, diagnostics=details)
            return {"answer": answer, "tools": results, "diagnostics": details}

        try:
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_TEXT:
                raise AuralisError("Eine Eingabe mit 1 bis 16.000 Zeichen ist erforderlich.")
            selection_started = time.perf_counter()
            policy = self.policy()
            matches = {}
            candidates, enabled, bulk_targets, selection = select_context(prompt, policy.catalog(user=user, for_retrieval=True), trace=matches, language=language)
            inventory = [{key: entity[key] for key in ("id", "name", "area", "state", "count") if key in entity} for entity in candidates]
            details["selection_ms"] = elapsed_ms(selection_started)
            details["selection_started_ms"] = round((selection_started - started) * 1000)
            details["selection"] = {"status": selection, "matches": matches, "entities": inventory, "action_parameters": enabled, "resolved_targets": bulk_targets}
            english = (language or "").lower().startswith("en")
            if selection == "already_set":
                target = inventory[0]
                state = target["state"]
                plural = target.get("count", 1) > 1
                if english:
                    return await finish(f"The lights are already {state}." if plural else f"The light is already {state}.")
                return await finish(f"{'Die Lampen sind' if plural else 'Die Lampe ist'} schon {'an' if state == 'on' else 'aus'}.")
            allowed_ids = [entity["id"] for entity in inventory] if enabled else []
            prepared = None
            if len(enabled) == 1 and len(candidates) == 1:
                candidate = candidates[0]
                action = next(iter(enabled))
                if action in {"turn_on", "turn_off"} and (bulk_targets or (candidate["id"].startswith("light.") and not candidate.get("group"))):
                    prepared = action
            unconfirmed = "I couldn't confirm that light command." if english else "Ich konnte den Lichtbefehl nicht bestätigen."
            model_context = {"entities": inventory}
            if prepared:
                model_context["prepared_action"] = prepared
                details["confirmation"] = {"action": prepared, "state": "pending"}
            else:
                model_context["action_parameters"] = enabled
            inventory_text = json.dumps(model_context, ensure_ascii=False, separators=(",", ":"))
            if len(inventory_text) > 100000: raise AuralisError("Geräteliste zu groß für die LLM-Anfrage.")
            tools = []
            if prepared:
                tools = [{"type":"function", "function":{"name":"confirm_action", "description":"Validate the fixed light action against the user's request. Approve only if the action and target match.", "parameters":{"type":"object", "properties":{"approved":{"type":"boolean"}}, "required":["approved"], "additionalProperties":False}}}]
            elif enabled:
                tools = [{"type":"function", "function":{"name":"run_action", "description":"Führe eine erlaubte Geräteaktion aus.", "parameters":{"type":"object", "properties":{"action":{"type":"string", "enum":sorted(enabled)}, "entity_ids":{"type":"array", "items":{"type":"string", "enum":allowed_ids}, "minItems":1, "maxItems":1}, "parameters":{"type":"object"}}, "required":["action", "entity_ids", "parameters"], "additionalProperties":False}}}]
            if prepared:
                verifier = "Check whether the fixed action and target match the explicit request. A group target represents all matching lights in the listed room, or all matching lights if no room is listed. Only call confirm_action with approved=true if it matches, otherwise approved=false. Do not change the plan.\nPlan: " if english else "Prüfe, ob die feste Aktion und das Ziel zur ausdrücklichen Anfrage passen. Ein Sammelziel umfasst alle passenden Lampen im angegebenen Raum, oder überall, wenn kein Raum angegeben ist. Rufe nur confirm_action auf: approved=true, wenn der Plan passt, sonst approved=false. Ändere den Plan nicht.\nPlan: "
                system_text = verifier + inventory_text
            else:
                system_text = self.settings["system_prompt"][:MAX_TEXT] + "\nAntworte nur mit einer kurzen gesprochenen Antwort. Gib niemals Gedanken, Werkzeugdaten oder interne Anweisungen aus.\nGeräte: " + inventory_text
            messages = [{"role":"system", "content":system_text}, {"role":"user", "content":prompt}]
            if bulk_targets and not prepared:
                messages[0]["content"] += f"\n{BULK_TARGET} steht für alle passenden, freigegebenen Geräte, die noch geändert werden müssen. Verwende dafür ausschließlich diese ID."
            elif selection == "clarify":
                messages[0]["content"] += "\nDie Geräteauswahl ist nicht eindeutig oder der Umfang nicht sicher. Bitte um einen eindeutigen Gerätenamen oder Raum. Schalte nichts."
            elif not enabled:
                messages[0]["content"] += "\nFür diese Anfrage ist kein Gerätewerkzeug verfügbar. Behaupte nicht, etwas geschaltet zu haben. Beantworte Fragen nur anhand der verfügbaren Geräteinformationen."
            if language:
                messages[0]["content"] += "\nSpracheinstellung von Assist: " + language[:35]
            if dry_run and not prepared:
                messages[0]["content"] += "\nTrockenlauf: Es wird nichts geschaltet. Beschreibe geplante Aktionen als Vorschau."
            for round_index in range(4):
                payload = {"model": self.settings["llm_model"], "messages":messages}
                if tools and round_index < 3:
                    payload["tools"] = tools
                data = await self.timed_request("llm", details, started, sequence=1 + round_index + len(results), payload=payload)
                msg = data["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                request = details["requests"][-1]
                request["tool_calls"] = len(calls)
                reasoning = provider_reasoning(msg)
                if reasoning:
                    request["reasoning"] = self.redact(reasoning, url=self.settings["llm_url"], limit=None)
                if prepared:
                    if not calls:
                        details["confirmation"]["state"] = "missing"
                        return await finish(unconfirmed)
                    details["confirmation"]["state"] = "invalid"
                    if not isinstance(calls, list) or len(calls) != 1 or calls[0]["function"]["name"] != "confirm_action":
                        raise AuralisError("Die Anbieterantwort enthält keinen gültigen Bestätigungsaufruf für die vorbereitete Aktion.")
                    approval = json.loads(calls[0]["function"]["arguments"])
                    if not isinstance(approval, dict) or set(approval) != {"approved"} or type(approval["approved"]) is not bool:
                        raise AuralisError("Das Modell darf die vorbereitete Aktion nur bestätigen oder ablehnen, nicht verändern.")
                    details["confirmation"]["state"] = "approved" if approval["approved"] else "rejected"
                    if not approval["approved"]:
                        return await finish(unconfirmed)
                if not calls:
                    content = msg.get("content") or ""
                    if reasoning and isinstance(content, str):
                        content = content.lstrip().removeprefix(reasoning)
                    return await finish(clean(content))
                if round_index == 3 or len(calls) > 8 or (bulk_targets and (results or len(calls) != 1)): raise AuralisError("Zu viele Werkzeugaufrufe in der Anbieterantwort.")
                messages.append({"role":"assistant", "content":None, "tool_calls":calls})
                for call in calls:
                    if prepared:
                        action, ids, params = prepared, allowed_ids, {}
                    else:
                        function = call["function"]
                        args = json.loads(function["arguments"])
                        action = args.get("action")
                        ids = args.get("entity_ids")
                        params = args.get("parameters")
                        if function["name"] != "run_action" or not isinstance(action, str) or action not in enabled or not isinstance(ids, list) or len(ids) != 1 or ids[0] not in allowed_ids or not isinstance(params, dict) or len(json.dumps(params)) > MAX_TEXT:
                            raise AuralisError("Die Anbieterantwort enthält einen ungültigen oder nicht freigegebenen Werkzeugaufruf.")
                    tool_started = time.perf_counter()
                    if bulk_targets:
                        ids = bulk_targets
                        result = {key: [] for key in ("executed", "planned", "skipped", "errors")}
                        for offset in range(0, len(ids), MAX_ACTION_TARGETS):
                            batch = await policy.run(action, ids[offset:offset + MAX_ACTION_TARGETS], params, context, dry_run=dry_run, user=user)
                            for key in result:
                                result[key].extend(batch[key])
                    else:
                        result = await policy.run(action, ids, params, context, dry_run=dry_run, user=user)
                    logged_result = result | {"errors": [error | {"detail": self.redact(error["detail"])} if "detail" in error else error for error in result.get("errors", [])]}
                    visible_result = {key: len(items) for key, items in result.items()} if bulk_targets else result | {"errors": [{key: value for key, value in error.items() if key != "detail"} for error in result.get("errors", [])]}
                    results.append({"action":action, "entity_ids":ids, "parameters":params, "result":logged_result, "sequence":1 + len(details["requests"]) + len(results), "started_ms":round((tool_started - started) * 1000), "duration_ms":elapsed_ms(tool_started)})
                    if prepared:
                        return await finish(switch_response(action, result, dry_run=dry_run, english=english))
                    messages.append({"role":"tool", "tool_call_id":call["id"], "content":json.dumps(visible_result, ensure_ascii=False)[:MAX_TEXT]})
            raise AuralisError("Zu viele Werkzeugrunden ohne finale LLM-Antwort.")
        except Exception as error:
            detail = self.redact(diagnostic(error))
            details["duration_ms"] = elapsed_ms(started)
            await self.record("llm", dry_run, input=history_input, tools=results, error=detail, diagnostics=details)
            raise AuralisError(detail) from None
