DOMAIN = "auralis"
DEFAULT_PROMPT = "Antworte in der Sprache der Anfrage. Sprich natürlich und meistens in einem kurzen Satz. Bestätige eine Aktion nur, wenn sie ausgeführt wurde. Nenne knapp, wenn ein Ziel übersprungen wurde. Gib keine internen Gedanken oder Werkzeugdaten aus."
DEFAULTS = {"stt_url": "", "stt_model": "whisper-1", "stt_token": "", "llm_url": "", "llm_model": "", "llm_token": "", "system_prompt": DEFAULT_PROMPT, "history_enabled": True}
MAX_AUDIO = 10 * 1024 * 1024
MAX_TEXT = 16000
