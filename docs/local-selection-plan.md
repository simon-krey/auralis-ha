# Lokale Aktionsauswahl

## Recherche und Entscheidung

Fünf Terra-Aufträge untersuchten Home Assistants Erkennung, deutsche STT-Varianten, lexikalisches Ranking und Kontextbudget, Hybridsuche sowie die Ausführungsgrenze. Die Luna-Aufträge prüfen dazu einzelne Quellstellen, Testfälle, Vergleichsmetriken und Release-Metadaten.

| Ansatz | Entscheidung |
| --- | --- |
| Namensregeln und HA-Aliasse | Beibehalten und gezielt verbessern. Exakte Treffer, Worttrennung und Lautähnlichkeit bleiben unterscheidbar; Nummern und Negationen dürfen nicht durch unscharfe Erkennung verloren gehen. |
| Lexikalische Suche | Bei fehlender eindeutiger Zuordnung nur wenige Vorschläge zur Rückfrage zulassen. [SQLite FTS5](https://www.sqlite.org/fts5.html) bietet BM25 und Trigramme, belegt aber keinen Vorteil für kurze deutsche Gerätenamen. Dafür jetzt keinen Index einführen. |
| Native Erkennung | [Hassil](https://github.com/home-assistant/hassil) ist als rein lesender Parser interessant. Assist-Interna nicht als stabile API voraussetzen und keine Intent-Handler zur Suche aufrufen. Etagen und erweiterte Registry-Auflösung separat evaluieren. |
| Embeddings, RRF, Lookup | Erst gegen die lokale Baseline messen. [Retrieve/Rerank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html) kann Kandidaten neu sortieren, aber fehlende Treffer nicht zurückholen. Lookup kostet eine weitere Modellrunde. |
| STT-Grenzen | Deutsche Lautähnlichkeit und [HA-Aliasse](https://www.home-assistant.io/voice_control/aliases/) helfen bei Schreibvarianten, nicht bei einem fehlenden „nicht“ oder erfundenen Transkriptteilen; siehe [Whisper-Modellkarte](https://github.com/openai/whisper/blob/main/model-card.md). |

## Umsetzung und Abnahme

1. Negationen und nicht aufgelöste Zahlenlisten abfangen. Kleine deutsche Zahlwörter als Namensvarianten behandeln; unterschiedliche Nummern und kollidierende Varianten dürfen nicht zusammenfallen.
2. Bei fehlendem Namen kurze, lokal gewertete Vorschläge aus dem lesbaren Katalog geben. Aufgelöste Räume und Gerätetypen begrenzen Vorschläge; unbekannte Räume werden nicht durch Fremdräume ersetzt. Vorschläge bekommen kein Werkzeug.
3. Vollkontext nur als zusätzlichen Modellkontext verwenden. Auch dort werden ausschließlich lokal ermittelte Aktionen und Ziele angeboten. Gruppenfilter, Policy-Prüfung, vollständige Sammelziele und Budget-Fallback bleiben erhalten.
4. Offline Regressionen für Zieltreffer, absichtliche Rückfragen, Gruppen, Vollkontext und Sammelvollständigkeit prüfen. Anschließend Version taggen und veröffentlichen. Für einen späteren Suchvergleich Recall@k, exakten Mengen-Recall, falsche Ziele, Rückfragen und p50/p95 auf derselben getrennten Stichprobe messen. Es liegen keine Live- oder Modellbenchmarks vor.
