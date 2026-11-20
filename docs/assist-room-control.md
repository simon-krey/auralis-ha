# Assist-Raumsteuerung und abgesicherte Ausführung (1.8.0)

## Home Assistant und Sicherheitsgrenzen

- Geprüft gegen Core **2026.11.2**. Die native Satelliten-Pipeline liefert `device_id` und `satellite_id`; WebSocket-, HTTP- und andere Assist-Eingaben können nur eine oder keine dieser Angaben liefern. Die Agentenschnittstelle kennt keine Mikrofonposition außerhalb dieser Metadaten. Die Entity-Registry liefert die Satelliten-Entität, die Device-Registry prüft das zugeordnete Gerät und dessen effektive Area (inklusive Parent-Device-Vererbung), und die Area-Registry prüft die Existenz des Raums. Widersprüche führen bei Ortsbezug zur Rückfrage. Die manuelle Zuordnung ergänzt fehlende Angaben; sie überschreibt keine HA-Area oder einen Konflikt. Explizite Raumnamen haben Vorrang.
- Auralis verwendet den nativen `ConversationEntity._async_handle_message`-Hook und die HA-`ChatLog`-Sitzungs-ID. Nur die abschließende Antwort wird als Assistenteninhalt hinzugefügt. Auralis übergibt weder HA-Tools an das Modell noch den ChatLog-Transcript an den Anbieter. Der kurzlebige Zielbezug bleibt separat nach Sitzung, Benutzer, Eingabegerät und Raum isoliert. HA-Tools oder alternative Intent-Handler würden die Auralis-Freigaben und die Bindung des vollständigen Plans nicht automatisch übernehmen und wurden daher nicht aktiviert.
- Assist-Exposition, HA-Lese-/Steuerrechte und Auralis-Freigabe sind voneinander unabhängig. Gruppen und bekannte Szenen dürfen keine nicht freigegebenen Mitglieder einschließen. Anonyme Assist-Aktionen benötigen ein bekanntes Eingabegerät und dürfen keine sensiblen Aktionen oder Szenen ausführen. Bestehende explizite Admin-Freigaben für undurchsichtige Szenen und Skripte bleiben erhalten; deren interne Auswirkungen kann Auralis nicht vollständig überprüfen.
- Die Werkzeugnamen und die vollständigen Schemata sind vor dem HTTP-Versand an den Plan gebunden, auch nach dem Budget-Fallback. Text, abgeschnittene Antworten oder doppelte, fehlende und veränderte Tool-Calls bestätigen nichts. Jeder vollständige Plan wird vor dem ersten Dienstaufruf geprüft. Vor weiteren Dienstaufrufen werden Benutzer, Freigaben, Fähigkeit, Zielgruppe und der relevante Raum erneut geprüft. Ein Raum-Sammelziel wird auch bei einer geänderten Geräteliste vor dem ersten Dienst verworfen.
- Es gibt keine Transaktion über mehrere HA-Dienste: Die Ausführung stoppt beim ersten ungeplanten Fehler, meldet bereits abgeschlossene Aufrufe separat und wiederholt keine Aktion. Ein erfolgreicher HA-Dienstaufruf ist keine physische Zustandsbestätigung. Alle Tests benutzten synthetische HA-Zustände, Registries und Dienstaufrufe; eine reale HA-Installation mit Hardware war nicht verfügbar.

Quellstand: [ConversationEntity][conversation], [Satelliten-Pipeline][satellite], [Conversation-Eingänge][inputs], [Device-Vererbung][device] und [Assist-Exposition][exposure].

## Messungen

**Offline:** 219 Held-out-Fälle (169 ausführbare Pläne im Pipeline-Trockenlauf), gleiche Eingaben für `00fa2ce` und 1.8.0. Das Modell ist simuliert, kein HA-Dienst wird aufgerufen. Folgende Messwerte sind ein lokaler Einzelmesslauf, keine Produktionslatenz:

| Stand | Richtige vollständige Pläne | Unbeabsichtigte/inkomplette Pläne | E2E p50/p95 | Modellrunden pro Pipeline-Fall |
| --- | ---: | ---: | ---: | ---: |
| `00fa2ce`, `multi` | 169/169 | 0 | 5,4 / 11,2 ms | 1,00 |
| 1.8.0, `multi` | 169/169 | 0 | 8,5 / 11,7 ms | 1,00 |
| 1.8.0, `multi` + Fast-Path | 169/169 | 0 | 5,6 / 10,5 ms | 0,48 |

110 Pläne brauchten mit Fast-Path keinen Modellaufruf (lokale Entscheidung p50/p95 5,3 / 10,2 ms). Die verbleibenden 59 benötigten und erhielten vollständige Werkzeuge. HTTP-Tests mit einem Loopback-Anbieter prüfen zusätzlich die serialisierten Requests, darunter Hue Go, Raumwechsel, fehlende Exposition, Rechteentzug und Teilfehler.

Mit 1.041 Zuständen und aktiviertem Vollkontext blieben beide Versionen bei 169/169 korrekten Plänen. Offline-E2E p50/p95 lag bei 31,9 / 46,7 ms für `00fa2ce` und 35,2 / 47,1 ms für 1.8.0. Die zusätzlichen Sicherheitsprüfungen sind nicht kostenlos; die Ergebnisse rechtfertigen keinen Cache für Rechte, Fähigkeiten oder live gebundene Bereiche.

**Realer Anbieter:** Lokales Ollama mit `gemma4:e2b`, fünf deutsche Tool-Calling-Fälle je Lauf, Reasoning ausdrücklich aus, 60 Sekunden HTTP-Zeitlimit und synthetische HA-Entitäten. Keine reale Geräteausführung. E2E-Perzentile beziehen sich auf vollständige, validierte Pläne; Tokenmittel auf Antworten mit Nutzungsangaben.

| Stand und Strategie | Bestätigte Werkzeugzustellung / gültige Calls / vollständige Pläne | E2E p50/p95 | Prompt / Ausgabe (Ø Tokens) |
| --- | --- | ---: | ---: |
| `00fa2ce`, `single` | 3/5 · 3/5 · 3/5 | 38,8 / 43,8 s | 271 / 14,3 |
| 1.8.0, `multi` | 4/5 · 4/5 · 4/5 | 41,0 / 51,1 s | 264,8 / 18,3 |
| 1.8.0, `single` | 5/5 · 5/5 · 5/5 | 30,1 / 39,6 s | 234 / 13,8 |

Nicht vollständige Fälle endeten mit HTTP-Timeout; es gab keine unerwarteten validierten Pläne und keine Wiederholungen. „Bestätigte Zustellung“ setzt eine erfolgreiche HTTP-Antwort voraus, nicht nur einen Sendeversuch. Der letzte Single-Lauf startete nach dem Vorwärmen; die übrigen Läufe wurden nicht temperaturkontrolliert. Deshalb sind diese kleinen Stichproben kein kausaler Nachweis einer Geschwindigkeitssteigerung. In einem separaten Minimalaufruf brauchte Ollama nach vollständigem Entladen 29,3 s, warm 7,1 s. Das Panel wärmt auf Wunsch mit genau einer Anfrage ohne Geräte oder Werkzeuge vor; wie lange das Modell geladen bleibt, legt der Anbieter fest. `reasoning_effort: none` und `max_tokens` funktionierten an diesem Endpoint. Es wurden keine automatischen Anbieteroptionen erzwungen. Ein zweites installiertes Tool-Calling-Modell stand nicht zur Verfügung.

```bash
python -m pip install -r tests/requirements.txt
python -m unittest discover -s tests -q
python tests/benchmark_resolver_v3.py --suite held-out --strategy multi
python tests/benchmark_resolver_v3.py --suite held-out --strategy multi --fast-path
python tests/benchmark_resolver_v3.py --suite held-out --strategy multi --target /path/to/00fa2ce-checkout
python tests/benchmark_resolver_v3.py --delivery-cases --endpoint http://127.0.0.1:11434/v1 --model gemma4:e2b --reasoning off --strategy single --timeout 65
```

[conversation]: https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/conversation/entity.py
[satellite]: https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/assist_satellite/entity.py
[inputs]: https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/conversation/http.py
[device]: https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/helpers/device_registry.py
[exposure]: https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/homeassistant/exposed_entities.py
