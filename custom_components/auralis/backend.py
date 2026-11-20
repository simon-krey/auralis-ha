"""Provider, policy and persistent history implementation."""
import asyncio
import base64
from dataclasses import dataclass
import json
from datetime import datetime, timezone
import re
import time
import uuid
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from .actions import ActionPolicy, MAX_ACTION_TARGETS, valid_parameters
from .const import DEFAULTS, MAX_AUDIO, MAX_TEXT, PLAN_APPROVAL, REASONING_EFFORT
from .conversation_memory import ConversationMemory, PendingChoice
from .names import normalize
from .resolver import MAX_STEPS, Resolution, ResolutionMode, resolve, target_alternatives
from .retrieval import BULK_TARGET, select_context

MAX_HISTORY = 99 * 1024 * 1024
MAX_AGE = 7 * 86400
MAX_RESPONSE = 1024 * 1024
MAX_ERROR = 2000
MAX_OUTPUT_TOKENS = 768
ENVELOPE_RESERVE = 256
MAX_PLAN_OPERATIONS = 2048
_REASONING = re.compile(r"<(think|thinking|analysis|reasoning)\b[^>]*>(.*?)(?:</\1\s*>|$)", re.I | re.S)
_ORPHAN_REASONING_END = re.compile(r"^.*</(?:think|thinking|analysis|reasoning)\s*>", re.I | re.S)


class AuralisError(ValueError):
    """A diagnostic that can be shown to an administrator."""


class ProviderRejected(AuralisError):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class InvalidProviderResponse(AuralisError):
    """An HTTP success response that cannot be parsed."""


@dataclass(frozen=True)
class ExecutableStep:
    action: str
    bindings: tuple[tuple[str, tuple[str, ...]], ...]
    values: tuple
    scope: tuple[str, ...]
    state_basis: tuple

    @classmethod
    def bind(cls, candidate):
        values = tuple((key, tuple(value) if isinstance(value, list) else value) for key, value in candidate.parameters.items())
        return cls(candidate.action, tuple(candidate.members.items()), values, tuple(candidate.scope), candidate.state_basis)

    @property
    def targets(self):
        return [target for target, _ in self.bindings]

    @property
    def parameters(self):
        return {key: list(value) if isinstance(value, tuple) else value for key, value in self.values}


def _unique_json_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON keys")
    return result


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant")


def _tool_arguments(call):
    try:
        function = call["function"]
        arguments = function["arguments"]
        if not isinstance(arguments, str) or len(arguments) > MAX_TEXT or ("type" in call and call["type"] != "function"):
            raise ValueError
        parsed = json.loads(arguments, object_pairs_hook=_unique_json_keys, parse_constant=_reject_json_constant)
        if not isinstance(parsed, dict) or not isinstance(function["name"], str):
            raise ValueError
        return function["name"], parsed
    except (KeyError, TypeError, ValueError, RecursionError):
        raise AuralisError("Die Anbieterantwort enthält einen ungültigen oder nicht freigegebenen Werkzeugaufruf.") from None


def _plan_tools(steps):
    tools = {}
    for index, step in enumerate(steps):
        name = {"fan_set_percentage": "set_fan_speed", "climate_temperature": "set_climate_temperature", "volume_set": "set_media_volume"}.get(step.action, step.action)
        if step.action == "light_set" and len(step.parameters) == 1:
            name = {"brightness_pct": "set_light_brightness", "rgb_color": "set_light_color", "color_temp_kelvin": "set_light_color_temperature"}.get(next(iter(step.parameters)), name)
        if len(steps) > 1:
            name = f"{name}_{index + 1}"
        schema = {key: step.schema[key] | {"enum": [value]} for key, value in step.parameters.items()}
        tools[name] = {"type": "function", "function": {
            "name": name, "description": f"Approve fixed action {index + 1} of {len(steps)} for {len(step.targets)} locally resolved target(s). Use exactly the specified values.",
            "parameters": {"type": "object", "properties": schema, "required": list(schema), "additionalProperties": False},
        }}
    return tools


def _batches(step):
    for offset in range(0, len(step.bindings), MAX_ACTION_TARGETS):
        batch = step.bindings[offset:offset + MAX_ACTION_TARGETS]
        yield [target for target, _ in batch], [leaf for _, leaves in batch for leaf in leaves]


def _decision_tool(name, description, argument, schema):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": {argument: schema}, "required": [argument], "additionalProperties": False,
    }}}


def _check_tools(tools, expected):
    return (isinstance(tools, list) and len(tools) == len(expected) == len(set(expected))
            and all(isinstance(tool, dict) and tool.get("type") == "function"
                    and isinstance(tool.get("function"), dict) and tool["function"].get("name") == name
                    and isinstance(schema := tool["function"].get("parameters"), dict) and schema.get("type") == "object"
                    and isinstance(schema.get("properties"), dict) and schema.get("required") == list(schema["properties"])
                    and schema.get("additionalProperties") is False for tool, name in zip(tools, expected)))


def _failed(result):
    return bool(result["errors"] or result["skipped"])


def _signatures(choices, entities):
    identities = {entity["id"]: (entity["name"], entity.get("area_id", entity.get("area")), entity.get("registry_id")) for entity in entities}
    return tuple(json.dumps((choice.action.diagnostic(), identities[choice.action.targets[0]]), sort_keys=True) for choice in choices)


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
        self.memory = ConversationMemory()

    async def load(self):
        data = await self.store.async_load() or {}
        self.settings.update({k: v for k, v in data.get("settings", {}).items() if k in DEFAULTS})
        self.settings["llm_url"] = llm_base_url(self.settings["llm_url"])
        if type(self.settings["llm_context_budget"]) is not int or self.settings["llm_context_budget"] not in (8000, 16000):
            self.settings["llm_context_budget"] = DEFAULTS["llm_context_budget"]
        if self.settings["llm_plan_approval"] not in ("multi", "single"):
            self.settings["llm_plan_approval"] = DEFAULTS["llm_plan_approval"]
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
            if key in {"history_enabled", "llm_full_context", "llm_light_comfort"}:
                if not isinstance(value, bool): raise ValueError("Ungültige Einstellungen")
            elif key == "llm_context_budget":
                if type(value) is not int or value not in (8000, 16000): raise ValueError("Ungültiges Kontextbudget")
            elif not isinstance(value, str) or len(value) > MAX_TEXT:
                raise ValueError("Ungültige Einstellungen")
            if key == "llm_reasoning" and value not in REASONING_EFFORT:
                raise ValueError("Ungültige Reasoning-Einstellung")
            if key == "llm_plan_approval" and value not in PLAN_APPROVAL:
                raise ValueError("Ungültige Planbestätigung")
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

        return self.sanitize_diagnostics({"method": "POST", "endpoint": endpoint, "body": body}, url=url if endpoint else "")

    def sanitize_diagnostics(self, value, *, url=""):
        if isinstance(value, dict):
            return {key: self.sanitize_diagnostics(item, url=url) for key, item in value.items()}
        if isinstance(value, list):
            return [self.sanitize_diagnostics(item, url=url) for item in value]
        return self.redact(value, url=url, limit=None) if isinstance(value, str) else value

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
                            raise ProviderRejected(response.status, "Fehlerantwort zu groß.")
                        raise InvalidProviderResponse("Antwort des Anbieters zu groß.")
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
                    raise ProviderRejected(response.status, detail or self.redact(response.reason or "Anbieteranfrage fehlgeschlagen", url, token))
                try:
                    if form is None and method == "POST":
                        return json.loads(raw, object_pairs_hook=_unique_json_keys, parse_constant=_reject_json_constant)
                    return json.loads(raw)
                except (ValueError, RecursionError) as error:
                    detail = diagnostic(error) if isinstance(error, json.JSONDecodeError) else "Ungültiges JSON in der Anbieterantwort."
                    raise InvalidProviderResponse(detail) from None
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

    async def timed_request(self, kind, details, origin, *, sequence=1, form_fields=None, expected_tools=(), **kwargs):
        started = time.perf_counter()
        request = {"started_ms": round((started - origin) * 1000), "sequence": sequence}
        body = kwargs.get("payload", form_fields)
        if kind == "llm":
            offered = body.get("tools", [])
            if not _check_tools(offered, expected_tools):
                details["action_outcome"] = "required_tools_missing"
                raise AuralisError("Die HTTP-Anfrage enthält nicht die erwarteten Werkzeuge. Nichts wurde geändert.")
            request.update(offered_tools=bool(offered), tool_names=list(expected_tools), tool_count=len(offered),
                           tool_delivery="pending" if offered else "not_required")
            request["reasoning"] = None
        request["request"] = self.request_snapshot(kind, body)
        details["requests"].append(request)
        http_started = time.perf_counter()
        try:
            data = await self.request(kind, **kwargs)
            if request.get("tool_count"):
                request["tool_delivery"] = "sent"
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict):
                counts = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if type(usage.get(key)) is int and 0 <= usage[key] < 2**31}
                if counts:
                    request["usage"] = counts
            return data
        except Exception as error:
            request["failed"] = True
            if isinstance(error, ProviderRejected):
                request["http_status"] = error.status
            if kind == "llm":
                outcome = "provider_rejected" if isinstance(error, ProviderRejected) else "model_invalid_response" if isinstance(error, InvalidProviderResponse) else "provider_failed"
                details["action_outcome"] = outcome
                if request["tool_count"]:
                    request["tool_delivery"] = {"provider_rejected": "provider_rejected", "model_invalid_response": "sent"}.get(outcome, "unconfirmed")
            raise
        finally:
            request["provider_ms"] = (time.perf_counter() - http_started) * 1000
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

    async def chat(self, prompt, language, context, user, dry_run=False, *, details=None, memory_key=None, voice_origin=None):
        started = time.perf_counter()
        details = {} if details is None else details
        details.update(model=self.redact(self.settings["llm_model"]), language=(language or "")[:35], requests=[], action_outcome="no_executable_action")
        results = []
        memory_key = None if dry_run else memory_key
        history_input = self.redact(prompt[:MAX_TEXT], url=self.settings["llm_url"], limit=None) if isinstance(prompt, str) else ""

        def sanitize_history():
            url = self.settings["llm_url"]
            details.update(self.sanitize_diagnostics(details, url=url))
            results[:] = self.sanitize_diagnostics(results, url=url)

        async def finish(answer):
            details["duration_ms"] = elapsed_ms(started)
            sanitize_history()
            answer = self.redact(answer, url=self.settings["llm_url"], limit=MAX_TEXT)
            await self.record("llm", dry_run, input=history_input, answer=answer, tools=results, diagnostics=details)
            return {"answer": answer, "tools": results, "diagnostics": details}

        try:
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_TEXT:
                raise AuralisError("Eine Eingabe mit 1 bis 16.000 Zeichen ist erforderlich.")
            selection_started = time.perf_counter()
            policy = self.policy()
            timings = details["local_timings_ms"] = {}
            catalog_started = time.perf_counter()
            catalog = policy.catalog(user=user, for_retrieval=True, trace=timings)
            timings["catalog_total_ms"] = (time.perf_counter() - catalog_started) * 1000
            resolver_started = time.perf_counter()
            remembered = self.memory.context(memory_key, catalog) if memory_key else None
            pending = remembered.pending if remembered else None
            english = (language or "").lower().startswith("en")
            correction = None
            if remembered and remembered.correction and normalize(prompt) in {"nein die andere", "nein der andere", "no the other one", "no the other"}:
                correction = remembered.correction
                pending = correction[0]
            if pending:
                answer = normalize(prompt)
                ordinal = {"erste": 0, "die erste": 0, "first": 0, "the first": 0, "1": 0,
                           "zweite": 1, "die zweite": 1, "second": 1, "the second": 1, "2": 1}.get(answer)
                if correction:
                    ordinal = 1 - correction[1]
                details["selection"] = {"status": "clarify", "resolution_mode": "clarification", "matches": {"reason": "pending_choice"}}
                if answer in {"abbrechen", "cancel"}:
                    self.memory.forget(memory_key)
                    return await finish("Cancelled. Nothing was changed." if english else "Abgebrochen. Nichts wurde geändert.")
                if ordinal is None:
                    return await finish("Say first, second, or cancel. Nothing was changed." if english else "Sag erste, zweite oder abbrechen. Nichts wurde geändert.")
                fresh = resolve(pending.prompt, catalog, pending.language, comfort=self.settings["llm_light_comfort"] is True, timings=timings)
                choices = target_alternatives(pending.prompt, catalog, pending.language, fresh) if pending.kind == "target" else fresh.choices
                if len(choices) != 2 or _signatures(choices, fresh.entities) != pending.signatures:
                    self.memory.forget(memory_key)
                    return await finish("The options changed. Nothing was changed." if english else "Die Optionen haben sich geändert. Nichts wurde geändert.")
                selected = choices[ordinal].action
                chosen_entities = [entity for entity in fresh.entities if entity["id"] in selected.targets]
                resolved = Resolution("matched", chosen_entities, steps=[selected], trace=fresh.trace | {"reference_source": "pending_choice"}, adaptive=True)
                correction = (pending, ordinal) if pending.kind == "target" else None
                prompt = ("Approve the selected option: " if english else "Bestätige die gewählte Option: ") + json.dumps(selected.diagnostic(), ensure_ascii=False)
            else:
                voice_area = policy.voice_area_id(*voice_origin) if voice_origin and any(voice_origin) else None
                resolved = resolve(prompt, catalog, language, remembered.ids if remembered else (), comfort=self.settings["llm_light_comfort"] is True,
                                   timings=timings, voice_area=voice_area, previous_action=remembered.action if remembered else None)
            timings["resolver_total_ms"] = (time.perf_counter() - resolver_started) * 1000
            candidates, enabled, bulk_targets, selection, steps, matches = resolved.entities, resolved.enabled, resolved.bulk, resolved.status, resolved.steps, resolved.trace
            mode = resolved.mode
            bounded = {choice.id: choice.action for choice in resolved.choices}
            if sum(len(leaves) for step in steps for leaves in step.members.values()) > MAX_PLAN_OPERATIONS:
                enabled, bulk_targets, selection, steps = {}, [], "clarify", []
                mode = ResolutionMode.CLARIFICATION
                matches["reason"] = "plan_limit_exceeded"
            single_plan = bool(steps) and self.settings["llm_plan_approval"] == "single"
            adaptive = resolved.adaptive and bool(steps) and not single_plan
            executable = tuple(ExecutableStep.bind(step) for step in steps)
            bound_options = {key: ExecutableStep.bind(step) for key, step in bounded.items()}
            if memory_key and selection not in {"read_only", "no_match"}:
                self.memory.forget(memory_key)
            inventory = [{key: entity[key] for key in ("id", "name", "area", "state", "count") if key in entity} for entity in candidates]
            details["selection_ms"] = elapsed_ms(selection_started)
            details["selection_started_ms"] = round((selection_started - started) * 1000)
            details["selection"] = {"status": selection, "mode": "all" if self.settings["llm_full_context"] else "filtered", "matches": matches, "entities": inventory, "action_parameters": enabled, "resolved_targets": bulk_targets, "matched_scope": matches.pop("scope", []), "plan": [step.diagnostic() for step in steps]}
            details["selection"].update(resolution_mode=mode.value, candidates=[choice.diagnostic() for choice in resolved.choices])
            if selection == "no_action" or matches.get("rejected_actions") or any(clause.get("status") == "no_action" for clause in matches.get("clauses", [])):
                details["action_outcome"] = "capability_or_policy_rejected"
            if memory_key and not pending and len(prompt) <= 512 and mode is ResolutionMode.CLARIFICATION:
                alternatives = target_alternatives(prompt, catalog, language, resolved)
                if alternatives:
                    by_id = {entity["id"]: entity for entity in candidates}
                    ordered = [by_id[choice.action.targets[0]] for choice in alternatives]
                    labels = [f"{entity['name']} ({entity['area']})" if entity["area"] else entity["name"] for entity in ordered]
                    self.memory.remember(memory_key, [entity["id"] for entity in ordered], catalog, source="clarification",
                                         pending=PendingChoice(prompt, language, _signatures(alternatives, candidates), "target"))
                    details["selection"]["clarification_choices"] = inventory
                    return await finish(f"First: {labels[0]}, or second: {labels[1]}?" if english else f"Erste: {labels[0]}, oder zweite: {labels[1]}?")
            if selection == "already_set":
                details["action_outcome"] = "already_set"
                target = inventory[0]
                scope = details["selection"]["matched_scope"]
                missed = any(item["status"] == "not_allowed" for item in scope)
                suffix = (" Some targets were not authorized." if english else " Einige Ziele waren nicht freigegeben.") if missed else ""
                if memory_key and not missed:
                    targets = [item["id"] for item in scope if item["status"] == "already_set"] if scope else [target["id"]]
                    self.memory.remember(memory_key, targets, catalog)
                state = target["state"]
                plural = target.get("count", 1) > 1
                nonlight = target["id"] == BULK_TARGET and details["selection"]["matched_scope"] and not details["selection"]["matched_scope"][0]["id"].startswith("light.")
                if nonlight:
                    return await finish((("The devices are already " + state + ".") if english else ("Die Geräte sind schon " + ("an." if state == "on" else "aus."))) + suffix)
                if english:
                    return await finish((f"The lights are already {state}." if plural else f"The light is already {state}.") + suffix)
                return await finish(f"{'Die Lampen sind' if plural else 'Die Lampe ist'} schon {'an' if state == 'on' else 'aus'}." + suffix)
            allowed_ids = [entity["id"] for entity in inventory] if enabled else []
            full_mode = self.settings["llm_full_context"]
            catalog_inventory = []
            if full_mode:
                for entity in catalog:
                    actions = {item["id"]: item.get("parameters", {}) for item in entity["actions"] if item.get("enabled") is True}
                    if not actions:
                        continue
                    catalog_inventory.append({key: entity[key] for key in ("id", "name", "area", "state") if key in entity} | {"actions": actions})
            prepared = None
            if not single_plan and not adaptive and len(enabled) == 1 and len(candidates) == 1:
                candidate = candidates[0]
                action = next(iter(enabled))
                if action in {"turn_on", "turn_off"} and ((bulk_targets and all(target.startswith("light.") for target in bulk_targets)) or (candidate["id"].startswith("light.") and not candidate.get("group"))):
                    prepared = action
            if prepared or single_plan:
                details["confirmation"] = {"action": prepared or "approve_plan", "state": "pending"}
            unconfirmed = ("I couldn't confirm the action plan." if english else "Ich konnte den Aktionsplan nicht bestätigen.") if single_plan else ("I couldn't confirm that light command." if english else "Ich konnte den Lichtbefehl nicht bestätigen.")
            schema_started = time.perf_counter()
            resolution_outcome = details["action_outcome"]
            details["action_outcome"] = "tool_generation_failed"
            plan_tools = _plan_tools(steps) if adaptive else {}
            if prepared:
                tools = [_decision_tool("confirm_action", "Validate the fixed light action against the user's request. Approve only if the action and target match.", "approved", {"type": "boolean"})]
                expected_tools = ("confirm_action",)
            elif single_plan:
                tools = [_decision_tool("approve_plan", "Approve or reject the complete fixed plan.", "approved", {"type": "boolean"})]
                expected_tools = ("approve_plan",)
            elif bounded:
                tools = [_decision_tool("choose_action", "Choose one locally bound action, or ask for clarification without calling a tool.", "choice", {"type": "string", "enum": list(bounded)})]
                expected_tools = ("choose_action",)
            elif adaptive:
                tools, expected_tools = list(plan_tools.values()), tuple(plan_tools)
            elif enabled and steps:
                action_spec = {"type": "string", "enum": sorted(enabled)}
                target_spec = {"type": "string", "enum": allowed_ids}
                arguments = {"action": action_spec, "entity_ids": {"type": "array", "items": target_spec, "minItems": 1, "maxItems": 1},
                             "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
                tools = [{"type": "function", "function": {"name": "run_action", "description": "Approve the fixed device action.",
                          "parameters": {"type": "object", "properties": arguments, "required": list(arguments), "additionalProperties": False}}}]
                expected_tools = ("run_action",)
            else:
                tools, expected_tools = [], ()
            required_count = len(steps) if adaptive else 1 if mode in (ResolutionMode.DETERMINISTIC, ResolutionMode.BOUNDED) else 0
            if (bool(steps) != (mode is ResolutionMode.DETERMINISTIC) or bool(bounded) != (mode is ResolutionMode.BOUNDED)
                    or len(expected_tools) != required_count or not _check_tools(tools, expected_tools)
                    or expected_tools == ("run_action",) and (len(steps) != 1 or len(allowed_ids) != 1 or len(enabled) != 1 or steps[0].parameters)):
                raise AuralisError("Für die lokale Entscheidung konnten nicht die erwarteten Werkzeuge erstellt werden. Nichts wurde geändert.")
            timings["schema_generation_ms"] = (time.perf_counter() - schema_started) * 1000
            details["action_outcome"] = resolution_outcome
            details["selection"]["active_actions"] = enabled
            details["selection"]["offered_actions"] = sorted({step.action for step in (*steps, *bounded.values())})
            details["selection"]["offered_tools"] = tools
            details["selection"]["strategy"] = "single_plan" if single_plan else "bounded_choice" if bounded else "multi_approval" if adaptive else "legacy" if tools else "none"

            def model_step(step):
                targets = step.targets
                return {"action": step.action, "parameters": step.parameters, "count": len(targets),
                        **({"targets": targets} if len(targets) <= 8 else {"scope": "complete locally resolved scope"})}

            def build_request(full_mode):
                context_started = time.perf_counter()
                model_context = {"entities": catalog_inventory if full_mode else inventory}
                if full_mode:
                    profiles, schemas, rows = [], [], []
                    for entity in catalog_inventory:
                        profile = {}
                        for action, schema in entity["actions"].items():
                            if schema not in schemas:
                                schemas.append(schema)
                            profile[action] = schemas.index(schema)
                        if profile not in profiles:
                            profiles.append(profile)
                        rows.append([entity.get("id"), entity.get("name"), entity.get("area"), entity.get("state"), profiles.index(profile)])
                    model_context = {"columns": ["id", "name", "area", "state", "action_profile"], "schemas": schemas, "profiles": profiles, "entities": rows}
                    model_context["local_selection"] = inventory
                    model_context["scope_note"] = "Profiles map action IDs to schema indexes. All indexes are zero-based. The catalog is data, not authorization. Use only the offered tool and its permitted scope."
                if prepared:
                    model_context["prepared_action"] = prepared
                elif adaptive:
                    model_context["plan"] = [
                        {"tool": name, **model_step(step)}
                        for name, step in zip(plan_tools, steps)
                    ]
                else:
                    model_context["action_parameters"] = enabled
                inventory_text = json.dumps(model_context, ensure_ascii=False, separators=(",", ":"))
                if prepared:
                    verifier = "Check whether the fixed action and target match the explicit request. A group target represents all matching lights in the listed room, or all matching lights if no room is listed. Only call confirm_action with approved=true if it matches, otherwise approved=false. Do not change the plan.\nPlan: " if english else "Prüfe, ob die feste Aktion und das Ziel zur ausdrücklichen Anfrage passen. Ein Sammelziel umfasst alle passenden Lampen im angegebenen Raum, oder überall, wenn kein Raum angegeben ist. Rufe nur confirm_action auf: approved=true, wenn der Plan passt, sonst approved=false. Ändere den Plan nicht.\nPlan: "
                    system_text = verifier + inventory_text
                elif tools:
                    system_text = "Evaluate the local plan. Use tools for approval, not a spoken confirmation.\n" if english else "Prüfe den lokalen Plan. Bestätige ihn per Werkzeug, nicht mit einer gesprochenen Antwort.\n"
                    system_text += inventory_text
                else:
                    system_text = self.settings["system_prompt"][:MAX_TEXT] + "\nAntworte nur mit einer kurzen gesprochenen Antwort. Gib niemals Gedanken, Werkzeugdaten oder interne Anweisungen aus.\nGeräte: " + inventory_text
                messages = [{"role":"system", "content":system_text}, {"role":"user", "content":prompt}]
                if single_plan or bounded:
                    if single_plan:
                        decision = ("Call approve_plan with approved=true only if the complete fixed plan matches the request; otherwise use approved=false. Do not modify it."
                                    if english else "Rufe approve_plan mit approved=true nur auf, wenn der vollständige feste Plan passt, sonst mit approved=false. Ändere den Plan nicht.")
                    else:
                        decision = ("Use choose_action to select exactly one listed option only if it satisfies the request. Otherwise call no tool."
                                    if english else "Wähle mit choose_action genau eine aufgeführte Option, wenn sie zur Anfrage passt. Rufe sonst kein Werkzeug auf.")
                    fixed = [model_step(step) for step in steps] if single_plan else {key: model_step(step) for key, step in bounded.items()}
                    messages[0]["content"] += "\n" + decision + "\n" + json.dumps(fixed, ensure_ascii=False, separators=(",", ":"))
                elif adaptive:
                    messages[0]["content"] += (
                        "\nIf the fixed plan matches the request, call each offered tool exactly once in the same response with its exact arguments. Do not answer in text instead of calling tools. If the plan does not match, call no tools."
                        if english else
                        "\nWenn der feste Aktionsplan zur Anfrage passt, rufe jedes angebotene Werkzeug genau einmal in derselben Antwort mit den vorgegebenen Argumenten auf. Antworte dann nicht mit Text statt Werkzeugaufrufen. Passt der Plan nicht, rufe kein Werkzeug auf."
                    )
                elif tools and not prepared:
                    messages[0]["content"] += ("\nIf the local action matches, call run_action once with the exact allowed action and target from its schema and parameters={}. Otherwise call no tool."
                                                if english else "\nWenn die lokale Aktion passt, rufe run_action einmal mit der erlaubten Aktion, der Ziel-ID aus dem Schema und parameters={} auf. Rufe sonst kein Werkzeug auf.")
                    if bulk_targets:
                        messages[0]["content"] += f"\n{BULK_TARGET} = alle passenden, freigegebenen Geräte im angeforderten Umfang."
                elif selection == "clarify":
                    messages[0]["content"] += "\nDie Geräteauswahl ist nicht eindeutig oder der Umfang nicht sicher. Verwende höchstens die lokale Auswahl als Vorschläge für eine Rückfrage; rate keine Ziele aus dem Katalog. Bitte um einen eindeutigen Gerätenamen oder Raum. Schalte nichts."
                elif not enabled:
                    messages[0]["content"] += "\nFür diese Anfrage ist kein Gerätewerkzeug verfügbar. Behaupte nicht, etwas geschaltet zu haben. Beantworte Fragen nur anhand der verfügbaren Geräteinformationen."
                if language:
                    messages[0]["content"] += "\nSpracheinstellung von Assist: " + language[:35]
                if dry_run and tools:
                    messages[0]["content"] += "\nDry run: Tool calls are validated but do not control devices." if english else "\nTrockenlauf: Werkzeugaufrufe werden geprüft, schalten aber keine Geräte."
                timings["context_build_ms"] = timings.get("context_build_ms", 0.0) + (time.perf_counter() - context_started) * 1000
                return messages

            def estimate(payload):
                # Heuristic for unknown tokenizers, not a provider token count or guarantee.
                serialization_started = time.perf_counter()
                size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                timings["serialization_ms"] = timings.get("serialization_ms", 0.0) + (time.perf_counter() - serialization_started) * 1000
                return (size + 1) // 2 + ENVELOPE_RESERVE + MAX_OUTPUT_TOKENS

            budget = {"limit": self.settings["llm_context_budget"], "source": "utf8_bytes_divided_by_2_plus_reserves", "output_reserve": MAX_OUTPUT_TOKENS, "envelope_reserve": ENVELOPE_RESERVE, "mode": "full" if full_mode else "filtered", "fallback": False, "catalog_count": len(catalog_inventory), "selected_count": len(inventory), "rounds": []}
            details["context_budget"] = budget
            messages = build_request(full_mode)
            effort = REASONING_EFFORT[self.settings["llm_reasoning"]]
            payload = {"model": self.settings["llm_model"], "messages": messages, "max_tokens": MAX_OUTPUT_TOKENS}
            if effort is not None:
                payload["reasoning_effort"] = effort
            if tools:
                payload["tools"] = tools
            estimated = estimate(payload)
            if full_mode and estimated > budget["limit"]:
                budget.update(mode="filtered", fallback=True, full_estimate=estimated, overflow=estimated - budget["limit"])
                payload["messages"] = build_request(False)
                estimated = estimate(payload)
            timings["tool_and_context_build_ms"] = timings["schema_generation_ms"] + timings["context_build_ms"]
            details["selection_ms"] = elapsed_ms(selection_started)
            details["selection"]["catalog_entities"] = catalog_inventory if full_mode and not budget["fallback"] else []
            budget["estimate"] = estimated
            budget["rounds"].append({"round": 1, "estimate": estimated})
            if estimated > budget["limit"]:
                budget["overflow"] = estimated - budget["limit"]
                details["action_outcome"] = "context_budget_exceeded"
                raise AuralisError("LLM-Kontextbudget überschritten; Anfrage oder Geräteauswahl ist zu groß.")
            data = await self.timed_request("llm", details, started, payload=payload, expected_tools=expected_tools)
            validation_started = time.perf_counter()
            request = details["requests"][-1]
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
                details["action_outcome"] = "model_invalid_response"
                raise AuralisError("Die Anbieterantwort enthält keine eindeutige Modellnachricht.")
            choice = choices[0]
            msg = choice["message"]
            reason = choice.get("finish_reason")
            request["finish_reason"] = reason[:128] if isinstance(reason, str) else None
            calls = msg.get("tool_calls")
            if calls is None:
                calls = []
            if isinstance(calls, list):
                request["tool_calls"] = len(calls)
                request["selected_tools"] = [function["name"][:256] for item in calls[:MAX_STEPS]
                                             if isinstance(item, dict) and isinstance(function := item.get("function"), dict)
                                             and isinstance(function.get("name"), str)]
            reasoning = provider_reasoning(msg)
            if reasoning:
                request["reasoning"] = self.redact(reasoning, url=self.settings["llm_url"], limit=None)
            if reason == "length":
                details["action_outcome"] = "model_output_truncated"
                raise AuralisError("Das Modell hat das Ausgabelimit erreicht. In dieser Runde wurde keine Aktion ausgeführt.")
            invalid = "Die Anbieterantwort enthält einen ungültigen oder nicht freigegebenen Werkzeugaufruf."
            expected_calls = 1 if single_plan or bounded else len(steps)
            if (reason not in (None, "stop", "tool_calls") or not isinstance(calls, list)
                    or reason == "tool_calls" and not calls
                    or calls and (msg.get("refusal") or not payload.get("tools") or len(calls) != expected_calls or len(calls) > MAX_STEPS
                                  or not all(isinstance(call, dict) and isinstance(call.get("id"), str) and 0 < len(call["id"]) <= 256 and call["id"].strip() for call in calls)
                                  or len({call["id"] for call in calls}) != len(calls))):
                details["action_outcome"] = "model_invalid_tool_calls"
                if prepared or single_plan:
                    details["confirmation"]["state"] = "invalid"
                raise AuralisError(invalid)
            if calls:
                details["action_outcome"] = "model_invalid_tool_calls"
                parsed = [_tool_arguments(call) for call in calls]
            elif payload.get("tools"):
                details["action_outcome"] = "model_no_tool_calls"
            if prepared or single_plan:
                if not calls:
                    details["confirmation"]["state"] = "missing"
                    return await finish(unconfirmed)
                details["confirmation"]["state"] = "invalid"
                name, approval = parsed[0]
                if name != ("approve_plan" if single_plan else "confirm_action"):
                    raise AuralisError("Die Anbieterantwort enthält keinen gültigen Bestätigungsaufruf für die vorbereitete Aktion.")
                if set(approval) != {"approved"} or type(approval["approved"]) is not bool:
                    raise AuralisError("Das Modell darf die vorbereitete Aktion nur bestätigen oder ablehnen, nicht verändern.")
                details["confirmation"]["state"] = "approved" if approval["approved"] else "rejected"
                if not approval["approved"]:
                    details["action_outcome"] = "model_rejected_plan"
                    return await finish(unconfirmed)
            if not calls:
                if bounded:
                    if memory_key:
                        self.memory.remember(memory_key, resolved.entities and [resolved.entities[0]["id"]], catalog,
                                             source="clarification", pending=PendingChoice(prompt, language, _signatures(resolved.choices, resolved.entities)))
                    if matches.get("intent_source") == "configured_light_comfort":
                        return await finish("First: brightness to 30 percent, or second: warm white?" if english else "Erste: Helligkeit auf 30 Prozent, oder zweite: Warmweiß?")
                    return await finish("The first or the second action? Nothing was changed." if english else "Die erste oder die zweite Aktion? Nichts wurde geändert.")
                if steps:
                    return await finish("I couldn't confirm the action plan." if english else "Ich konnte den Aktionsplan nicht bestätigen.")
                if selection == "clarify" or selection == "no_match" and matches.get("action_hints"):
                    return await finish("Please clarify the targets or action. Nothing was changed." if english else "Bitte sag genauer, welche Geräte oder Aktion du meinst. Nichts wurde geändert.")
                if selection == "no_action":
                    return await finish("The requested action isn't supported or allowed for that target." if english else "Diese Aktion wird für das Ziel nicht unterstützt oder ist nicht freigegeben.")
                content = msg.get("content") or ""
                if reasoning and isinstance(content, str):
                    content = content.lstrip().removeprefix(reasoning)
                return await finish(clean(content) if isinstance(content, str) else "")
            if bounded:
                name, args = parsed[0]
                if name != "choose_action" or set(args) != {"choice"} or not isinstance(args["choice"], str) or args["choice"] not in bounded:
                    raise AuralisError(invalid)
                executable = (bound_options[args["choice"]],)
                details["selection"]["selected_choice"] = args["choice"]
            elif adaptive:
                seen = set()
                by_name = dict(zip(expected_tools, zip(steps, executable)))
                for name, args in parsed:
                    if name not in by_name or name in seen:
                        raise AuralisError(invalid)
                    step, bound = by_name[name]
                    if not valid_parameters(step.action, args, step.schema) or args != bound.parameters:
                        raise AuralisError(invalid)
                    seen.add(name)
            elif not (prepared or single_plan):
                name, args = parsed[0]
                if name != "run_action" or args != {"action": steps[0].action, "entity_ids": allowed_ids, "parameters": {}}:
                    raise AuralisError(invalid)

            details["selection"]["parameters_validated"] = True
            timings["model_validation_ms"] = (time.perf_counter() - validation_started) * 1000
            preflight_started = time.perf_counter()
            details["action_outcome"] = "preflight_rejected"
            policy = self.policy()

            def reject_preflight(reason):
                details["selection"]["preflight_failed"] = reason
                timings["preflight_ms"] = (time.perf_counter() - preflight_started) * 1000

            if area := matches.get("voice_area"):
                if policy.voice_area_id(*voice_origin) != area or any(getattr(policy._area(target), "id", None) != area for step in executable for target in step.targets):
                    reject_preflight("voice_area_changed")
                    return await finish("The room changed. Nothing was changed." if english else "Der Raum hat sich geändert. Nichts wurde geändert.")
            if matches.get("intent_source") == "configured_light_comfort" and self.settings["llm_light_comfort"] is not True:
                reject_preflight("comfort_disabled")
                return await finish("That interpretation is no longer enabled. Nothing was changed." if english else "Diese Auslegung ist nicht mehr aktiviert. Nichts wurde geändert.")
            for step in executable:
                if step.state_basis and not policy.basis_matches(step.targets[0], step.state_basis):
                    reject_preflight("live_value_changed")
                    return await finish("The device state changed. Nothing was changed." if english else "Der Gerätezustand hat sich geändert. Nichts wurde geändert.")
                for targets, leaves in _batches(step):
                    checked = policy.preflight(step.action, targets, step.parameters, context, user=user, expected_leaves=leaves)
                    if _failed(checked):
                        reject_preflight(checked)
                        return await finish("The targets are no longer available or authorized. Nothing was changed." if english else "Die Ziele sind nicht mehr verfügbar oder freigegeben. Nichts wurde geändert.")

            details["selection"]["preflight_passed"] = True
            timings["preflight_ms"] = (time.perf_counter() - preflight_started) * 1000
            execution_started = time.perf_counter()
            details["action_outcome"] = "execution_failed"
            stopped = False
            for step in executable:
                tool_started = time.perf_counter()
                action, ids, params = step.action, step.targets, step.parameters
                result = {key: [] for key in ("executed", "planned", "skipped", "errors")}
                if stopped:
                    result["skipped"] = [{"entity_id": target, "reason": "earlier action failed"} for target in ids]
                elif step.state_basis and not policy.basis_matches(ids[0], step.state_basis):
                    stopped = True
                    result["errors"] = [{"entity_id": ids[0], "reason": "live value changed"}]
                else:
                    for targets, leaves in _batches(step):
                        batch = await policy.run(action, targets, params, context, dry_run=dry_run, user=user, expected_leaves=leaves)
                        for key in result:
                            result[key].extend(batch[key])
                        if _failed(batch):
                            stopped = True
                            result["skipped"].extend({"entity_id": target, "reason": "earlier batch failed"} for target in ids[ids.index(targets[-1]) + 1:])
                            break
                if len(executable) == 1:
                    result["skipped"].extend({"entity_id": item["id"], "reason": "not in the permitted scope"} for item in details["selection"]["matched_scope"] if item["status"] == "not_allowed")
                logged_result = result | {"errors": [error | {"detail": self.redact(error["detail"])} if "detail" in error else error for error in result.get("errors", [])]}
                results.append({"action":action, "entity_ids":ids, "parameters":params, "result":logged_result, "sequence":1 + len(details["requests"]) + len(results), "started_ms":round((tool_started - started) * 1000), "duration_ms":elapsed_ms(tool_started)})
            timings["execution_ms"] = (time.perf_counter() - execution_started) * 1000
            count = sum(len(item["result"]["planned" if dry_run else "executed"]) for item in results)
            missed = any(_failed(item["result"]) for item in results)
            if count and missed:
                details["action_outcome"] = "dry_run_partial" if dry_run else "partial_failure"
            elif count:
                details["action_outcome"] = "dry_run_validated" if dry_run else "executed"
            if memory_key and not missed:
                last = executable[-1]
                self.memory.remember(memory_key, last.scope, catalog, source="executed", action=last.action, parameters=last.parameters, correction=correction)
            if prepared:
                return await finish(switch_response(action, result, dry_run=dry_run, english=english))
            if english:
                answer = f"{count} device {'operation' if count == 1 else 'operations'} {'planned' if dry_run else 'completed'}." + (" Some operations could not be completed." if missed else "")
            else:
                answer = f"{count} {'Geräteaktion' if count == 1 else 'Geräteaktionen'} {'geplant' if dry_run else 'ausgeführt'}." + (" Einige Aktionen waren nicht möglich." if missed else "")
            return await finish(answer)
        except Exception as error:
            detail = self.redact(diagnostic(error))
            details["error"] = detail
            details["duration_ms"] = elapsed_ms(started)
            sanitize_history()
            await self.record("llm", dry_run, input=history_input, tools=results, error=detail, diagnostics=details)
            raise AuralisError(detail) from None
