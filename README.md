<img src="custom_components/auralis/brand/icon.png" width="64" height="64" alt="">

# Auralis für Home Assistant

Auralis verbindet die native Assist-Pipeline mit zwei getrennten OpenAI-kompatiblen APIs: eine für die Spracherkennung und eine für den Konversationsagenten. Der Agent steuert Geräte über einzeln freigebbare Aktionen. Die Sprachausgabe übernimmt Home Assistant, zum Beispiel mit Piper.

Standardmäßig filtert Auralis Geräte und Aktionen lokal. Mit **Alle Geräte an das LLM senden** bekommt der konfigurierte Endpoint zusätzlich den lesbaren Gerätekatalog mit den freigegebenen Aktionen. Das braucht mehr Modellkontext, gibt dem LLM aber keine weiteren Schaltziele. In beiden Modi kann es nur lokal ermittelte Ziele und Aktionen verwenden. Bei unklarem Umfang oder reinen Statusfragen erhält es kein Schaltwerkzeug.

Das lokale LLM-Kontextbudget beträgt standardmäßig 8.000 Tokens und kann auf 16.000 erhöht werden. Auralis schätzt vor jeder Modellrunde Nachrichten und Werkzeugdefinitionen anhand der UTF-8-Bytes und reserviert zusätzlich Platz für den Anfrage-Rahmen sowie höchstens 768 Ausgabetokens (`max_tokens`, einschließlich Thinking soweit der Anbieter es so zählt). Die konservative Schätzung ist keine exakte Tokenzählung und garantiert keine Anbieter-Kontextlänge; Modell und Anbieter können anders zählen oder kleinere Grenzen haben. Passt der vollständige Katalog nicht hinein, verwendet Auralis die sichere lokale Auswahl und bewahrt vollständig lokal aufgelöste Sammelziele. Passt auch diese Anfrage nicht, meldet Auralis einen Fehler. Der Verlauf enthält Modus, Fallback, Größen und Schätzungen pro Runde. [Recherche zum Kontextbudget](docs/context-budget-research.md).

Standardmäßig sind vor allem Licht, Steckdosen und andere normale Schalter, Klima, Ventilatoren, Luftbefeuchter, geeignete Beschattungen sowie geprüfte Szenen und Gruppen freigegeben. Konfigurations- und Diagnoseentitäten, DnD-Schalter und viele Helfer sind zunächst aus. Bestehende manuelle Freigaben und Sperren bleiben erhalten. **Standardfreigaben wiederherstellen** im Aktionen-Tab entfernt diese Anpassungen nach Rückfrage. Verbindungen, Kontextbudget und Verlauf bleiben unangetastet.

Exakte Geräte- und Raumnamen sowie Home-Assistant-Aliasse haben Vorrang. Die Auswahl erkennt getrennte Wörter wie „Schlaf Zimmer“ und eindeutige Schreibabweichungen. Für deutsche Anfragen kommen Lautregeln, Kölner Phonetik und einfache Zahlwortvarianten wie „Leselampe eins“ hinzu. Unscharfe Namen allein erlauben keine sensiblen Aktionen. Bei unvollständigen Namen kann Auralis passende Geräte für eine Rückfrage vorschlagen, ohne sie zu schalten. [Recherche und Umsetzungsplan](docs/local-selection-plan.md).

Einfache Ein/Aus-Lichtbefehle bereitet Auralis vollständig vor. Bei „Kannst du bitte das Licht im Schlafzimmer ausmachen?“ enthält der Plan alle freigegebenen Lampen im Raum, die noch nicht aus sind. Das LLM kann den Plan nur bestätigen oder ablehnen. Ohne gültige Bestätigung wird nichts geschaltet. Auralis prüft danach die Freigaben erneut und spricht das tatsächliche Ergebnis ohne weiteren LLM-Aufruf aus. Dafür ist kein zweites Modell nötig.

Ein/Aus-Sammelbefehle wie „Schalte alle Lampen aus“ erfassen auch Geräte ohne Raum. Eindeutige Sammelbefehle funktionieren ebenso für Schalter, Ventilatoren und Luftbefeuchter. Auralis überspringt Gruppen, um Geräte nicht doppelt zu schalten. Im Verlauf stehen die passende Geräteauswahl und die tatsächlich zu ändernden Ziele getrennt.

Der **Action Resolver** prüft ausdrücklich genannte Ziele als vollständige Liste: Bei „Schalte die Leselampe und die Sofalampe aus“ wird nichts geschaltet, wenn eines der Ziele unklar, technisch ungeeignet oder nicht freigegeben ist. Unterstützt werden auch „Stelle alle Lampen im Wohnzimmer auf 40 Prozent“, eindeutige Ausnahmen und mehrere Aktionen in einer Anfrage. Ein parametrisierter Sammelbefehl wird nur angeboten, wenn alle Ziele den gewünschten Wert unterstützen und die Aktion erlauben. Bei Ein/Aus-Sammelbefehlen werden bekannte, per Richtlinie gesperrte Ziele übersprungen und als solche gemeldet.

Wenn mehrere Entitäten denselben Gerätenamen teilen, grenzt ein Zusatz wie „Lampe“ oder „Steckdose“ das Ziel ein. In „Schalte die Hue Go Lampe an und ändere ihre Farbe auf lila“ bezieht sich „ihre Farbe“ eindeutig auf die Lampe aus dem ersten Teilschritt. Auralis prüft beide Aktionen vor der Ausführung. Bei einer Rückfrage zeigt der Verlauf auch die erkannten Namen und Ablehnungsgründe der einzelnen Teilschritte.

**Action Resolver 3.0** bietet entweder einen festen Plan, eine begrenzte Auswahl für ein eindeutiges Gerät oder keine Werkzeuge bei notwendiger Rückfrage. Optional kann das Modell den gesamten Plan mit einem Aufruf bestätigen. „Gemütlicher“ fragt standardmäßig nach; nach ausdrücklicher Aktivierung darf das Modell zwischen 30 % Helligkeit und Warmweiß (2700 K) wählen, sofern die Lampe beide Optionen unterstützt und sie freigegeben sind.

Die Sprachsteuerung versteht „auf die Hälfte“, „zweiundzwanzig Grad“, „heller“, „leiser“, „zehn Prozentpunkte“, „Warmweiß“ und „Türkis“. Ein unqualifiziertes „heller“/„dunkler“ oder „lauter“/„leiser“ bedeutet zehn Prozentpunkte; „etwas“ fragt nach. Relative Werte benötigen einen gültigen aktuellen Gerätezustand. Mehrdeutige Rückbezüge und Werte außerhalb des erlaubten Bereichs werden nicht geraten oder still korrigiert. [Details und Benchmarks](docs/action-resolver-v3-results.md).

In derselben nativen Assist-Konversation kann „Mach sie blau“ auf ein zuvor bestätigtes Ziel verweisen. Diese Referenzen verfallen nach fünf Minuten und werden nicht zwischen Benutzern oder Satelliten geteilt. Für bedingte Befehle, uneindeutige Verweise und relative Angaben ohne festgelegten Schritt fragt Auralis nach oder führt nichts aus. Details zur [Architektur und zum Offline-Benchmark](docs/action-resolver-v2-results.md).

<!-- ha-marketplace:exclude-start -->
## Installation

Zielversion: Home Assistant Core 2026.11.2 auf Home Assistant OS.

[![Auralis im Home Assistant Marketplace öffnen](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=simon-krey&repository=auralis-ha&category=integration)

1. In Home Assistant **Einstellungen → Marketplace → Über Link hinzufügen** öffnen oder den Button oben nutzen.
2. `https://github.com/simon-krey/auralis-ha` als benutzerdefiniertes Repository hinzufügen. Wenn der Marketplace danach fragt, GitHub verbinden und als Typ **Integration** wählen.
3. **Auralis** öffnen und **Installieren** wählen. Danach startet der Einrichtungsdialog.

Ohne Marketplace: Den Ordner `custom_components/auralis` aus dem Repository nach `/config/custom_components/auralis` kopieren, Home Assistant neu starten und die Integration unter **Einstellungen → Geräte & Dienste** hinzufügen.
<!-- ha-marketplace:exclude-end -->

## In Assist einrichten

1. In der Sidebar **Auralis → Konfiguration** öffnen. Für STT die vollständige URL (z. B. `https://stt.example.com/v1/audio/transcriptions`), Modell und bei Bedarf Token eintragen. Beim LLM reicht die Basis-URL, z. B. `https://llm.example.com/v1`. Auralis lädt die Modell-IDs automatisch; die ID lässt sich auch manuell eingeben. Das Chat-Modell muss strukturierte Tool-Aufrufe unterstützen.
2. Unter **Einstellungen → Sprachassistenten** einen Assistenten bearbeiten. **Auralis** für Spracherkennung und Konversation auswählen, **Piper** als Sprachausgabe einstellen. **Befehle bevorzugt lokal verarbeiten** für diese Pipeline ausschalten, damit die Aktionsfreigaben greifen. Piper bei Bedarf separat in Home Assistant einrichten.

Unter **Konfiguration → Reasoning (Thinking)** steuert **An** das Modell mit `reasoning_effort: "low"` und **Aus** mit `reasoning_effort: "none"`. **Anbieterstandard** sendet keinen Zusatzparameter und ist die Voreinstellung. Ob sich Thinking wirklich umschalten lässt, hängt vom Endpoint und Modell ab. Bei einer Ablehnung **Anbieterstandard** wählen. Thinking bleibt von der Sprachausgabe getrennt.

Fehlt die Auswahl nach einem Update, prüfe, ob Version 1.3.0 oder neuer vollständig installiert ist, starte Home Assistant neu und lade die Home-Assistant-Seite im Browser vollständig neu. Falls nötig, den Browser-Cache für Home Assistant leeren.

## Testen und freigeben

- **STT:** Im Panel das Mikrofon freigeben und einen kurzen Satz aufnehmen. Der Test zeigt das Transkript. Die Mikrofonaufnahme benötigt HTTPS oder einen anderen sicheren Browser-Kontext.
- **LLM:** Einen Befehl wie „Mach die Wohnzimmerlampen blau und stell sie auf 40 Prozent“ eingeben. Der Trockenlauf zeigt die geplanten Aufrufe und prüft die Freigaben, ohne Home-Assistant-Dienste aufzurufen.
- **Live:** Erst nach dem Trockenlauf in der nativen Assist-Pipeline sprechen. Unter **Erlaubte Aktionen** den Schalter einer Lampe ausschalten und einen Raum- oder Gruppenbefehl wiederholen: Er sollte nur freigegebene Ziele ändern.

Sicherheitsrelevante Aktionen und Szenen oder Skripte mit nicht zuverlässig prüfbaren Auswirkungen bleiben zunächst aus. Wer sie explizit aktiviert, erlaubt ihre Ausführung ohne weitere Rückfrage. Systemverwaltung und beliebige Dienstaufrufe sind nicht als Tools verfügbar. Die Integration unterstützt einen festen Katalog typischer Geräteaktionen, z. B. für Lichter, Schalter, Klima, Medienplayer, Rollläden und Saugroboter. Weitere Aktionen lassen sich über freigegebene Skripte abdecken. Undurchsichtige Szenen und Skripte sind auch nach Freigabe nur für Admin-Kontexte und Sprachsatelliten ohne Benutzer-ID verfügbar. Prüfe den Inhalt einer Szene oder eines Skripts vor der Freigabe; bestehende Automationen können unabhängig vom Agenten auf Zustandsänderungen reagieren.

## Datenschutz und Verlauf

STT sendet Audio an den eingestellten STT-Endpoint. Der LLM-Endpoint erhält die Anfrage, den dafür ausgewählten Gerätekontext und die Tool-Ergebnisse. Nutze dafür Anbieter, denen du diese Daten anvertrauen möchtest. Tokens werden nach dem Speichern nicht erneut ans Panel gesendet; schütze trotzdem den Home-Assistant-Konfigurationsordner und deine Backups.

Der lokale Admin-Verlauf speichert Audio, Transkripte, finale Antworten, Tool-Aufrufe und Fehler bis zu sieben Tage und höchstens 100 MB. Bei neuen Test- und Live-Einträgen zeigt er außerdem die Verarbeitungszeit ohne Aufnahme und Speicherung, die Dauer einzelner API-Anfragen und die Tokenzahlen, sofern der Anbieter sie meldet. Unter **Verarbeitungsdetails** stehen die lokale Geräteauswahl, die dem LLM angebotenen Aktionen samt Parametern und bei Sammelbefehlen die passenden lesbaren Geräte mit ihrem jeweiligen Status. Das kompakte Sammelziel im Modellkontext steht für die lokal aufgelöste Liste; ausgeführt werden die einzelnen Geräte in Batches. Die tatsächlich angeforderten Werkzeugaufrufe und ihre Ergebnisse stehen getrennt darunter. Bei API-Fehlern zeigt der Verlauf den HTTP-Status und, falls vorhanden, die Meldung des Anbieters. Bekannte Zugangsdaten werden in Fehlertexten redigiert. Du kannst die Aufzeichnung ausschalten und Einträge inklusive Audio löschen. Native STT- und LLM-Aufrufe stehen getrennt im Verlauf, weil Assist dem STT-Provider keine gemeinsame Aufruf-ID übergibt. Die native Spracherkennung ist für Deutsch und Englisch registriert. Echte API- und Geräteaufrufe müssen auf der Zielinstallation geprüft werden.

Die Verarbeitungsdetails zeigen auch, welche Raum- und Gerätenamen erkannt wurden, wie sie übereinstimmten und wann Auswahl, API-Aufrufe und Werkzeugausführung begannen. Bei vorbereiteten Aktionen steht dabei, ob das Modell den Plan bestätigt hat. Jede neue API-Runde zeigt Methode, Endpunkt ohne Query-Parameter und den vollständigen redigierten LLM-JSON-Body bzw. die STT-Formularfelder. Audio bleibt separat abspielbar; Zugangsdaten und HTTP-Header werden dabei nicht protokolliert. Wenn der Modellanbieter Thinking als eigenes Feld oder als markierten Block liefert, erscheint der redigierte Text an derselben API-Runde. Andernfalls steht dort, dass kein Thinking geliefert wurde. Diese Felder und markierten Blöcke werden nicht vorgelesen. Unmarkierter Text in der normalen Antwort lässt sich nicht zuverlässig von einer beabsichtigten Sprachausgabe unterscheiden.
