<img src="custom_components/auralis/brand/icon.png" width="64" height="64" alt="">

# Auralis für Home Assistant

Auralis verbindet die native Assist-Pipeline mit zwei getrennten OpenAI-kompatiblen APIs: eine für die Spracherkennung und eine für den Konversationsagenten. Der Agent steuert Geräte über einzeln freigebbare Aktionen. Die Sprachausgabe übernimmt Home Assistant, zum Beispiel mit Piper.

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

1. In der Sidebar **Auralis** öffnen. Für beide Anbieter die vollständige Endpoint-URL, das Modell und bei Bedarf einen API-Token eintragen. Typische Pfade sind `/v1/audio/transcriptions` und `/v1/chat/completions`. Das Chat-Modell muss strukturierte Tool-Aufrufe unterstützen.
2. Unter **Einstellungen → Sprachassistenten** einen Assistenten bearbeiten. **Auralis** für Spracherkennung und Konversation auswählen, **Piper** als Sprachausgabe einstellen. **Befehle bevorzugt lokal verarbeiten** für diese Pipeline ausschalten, damit die Aktionsfreigaben greifen. Piper bei Bedarf separat in Home Assistant einrichten.

## Testen und freigeben

- **STT:** Im Panel das Mikrofon freigeben und einen kurzen Satz aufnehmen. Der Test zeigt das Transkript. Die Mikrofonaufnahme benötigt HTTPS oder einen anderen sicheren Browser-Kontext.
- **LLM:** Einen Befehl wie „Mach die Wohnzimmerlampen blau und stell sie auf 40 Prozent“ eingeben. Der Trockenlauf zeigt die geplanten Aufrufe und prüft die Freigaben, ohne Home-Assistant-Dienste aufzurufen.
- **Live:** Erst nach dem Trockenlauf in der nativen Assist-Pipeline sprechen. Den Toggle einer Lampe ausschalten und einen Raum- oder Gruppenbefehl wiederholen: Er sollte nur freigegebene Ziele ändern.

Normale unterstützte Geräteaktionen sind standardmäßig aktiv. Sicherheitsrelevante Aktionen und Szenen oder Skripte mit nicht zuverlässig prüfbaren Auswirkungen bleiben zunächst aus. Wer sie explizit aktiviert, erlaubt ihre Ausführung ohne weitere Rückfrage. Systemverwaltung und beliebige Dienstaufrufe sind nicht als Tools verfügbar. Die Integration unterstützt einen festen Katalog typischer Geräteaktionen, z. B. für Lichter, Schalter, Klima, Medienplayer, Rollläden und Saugroboter. Weitere Aktionen lassen sich über freigegebene Skripte abdecken. Undurchsichtige Szenen und Skripte sind auch nach Freigabe nur für Admin-Kontexte und Sprachsatelliten ohne Benutzer-ID verfügbar. Prüfe den Inhalt einer Szene oder eines Skripts vor der Freigabe; bestehende Automationen können unabhängig vom Agenten auf Zustandsänderungen reagieren.

## Datenschutz und Verlauf

STT sendet Audio an den eingestellten STT-Endpoint. Der LLM-Endpoint erhält den Prompt samt Gerätekontext und Tool-Ergebnissen. Nutze dafür Anbieter, denen du diese Daten anvertrauen möchtest. Tokens werden nach dem Speichern nicht erneut ans Panel gesendet; schütze trotzdem den Home-Assistant-Konfigurationsordner und deine Backups.

Der lokale Admin-Verlauf speichert Audio, Transkripte, finale Antworten, Tool-Aufrufe und Fehler bis zu sieben Tage und höchstens 100 MB. Tests sind markiert. Du kannst die Aufzeichnung ausschalten und Einträge inklusive Audio löschen. Native STT- und LLM-Aufrufe stehen getrennt im Verlauf, weil Assist dem STT-Provider keine gemeinsame Aufruf-ID übergibt. Die native Spracherkennung ist für Deutsch und Englisch registriert. Echte API- und Geräteaufrufe müssen auf der Zielinstallation geprüft werden.
