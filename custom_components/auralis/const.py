DOMAIN = "auralis"
DEFAULT_PROMPT = "Antworte in der Sprache der Anfrage. Sprich natürlich und meistens in einem kurzen Satz. Bestätige eine Aktion nur, wenn sie ausgeführt wurde. Nenne knapp, wenn ein Ziel übersprungen wurde. Gib keine internen Gedanken oder Werkzeugdaten aus."
REASONING_EFFORT = {"default": None, "on": "low", "off": "none"}
DEFAULTS = {"stt_url": "", "stt_model": "whisper-1", "stt_token": "", "llm_url": "", "llm_model": "", "llm_token": "", "llm_reasoning": "default", "system_prompt": DEFAULT_PROMPT, "history_enabled": True, "llm_full_context": False, "llm_context_budget": 8000}
MAX_AUDIO = 10 * 1024 * 1024
MAX_TEXT = 16000
