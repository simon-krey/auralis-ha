class AuralisPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._loaded = false;
    this._urls = new Set();
    this._offset = 0;
    this._modelRequest = 0;
  }

  set hass(value) {
    this._hass = value;
    if (this.isConnected && !this._loaded && value) this.load();
  }
  get hass() { return this._hass; }

  connectedCallback() {
    this.render();
    if (this._hass && !this._loaded) this.load();
  }
  disconnectedCallback() {
    this._alive = false;
    this._modelRequest++;
    this.stopRecording(true);
    this.revokeAudio();
    this._loaded = false;
  }
  $(selector) { return this.shadowRoot.querySelector(selector); }
  async call(type, payload = {}) {
    if (!this._hass) throw new Error('Home Assistant ist nicht verbunden.');
    return this._hass.callWS({ type: `auralis/${type}`, ...payload });
  }
  status(message, error = false) {
    const node = this.$('#status');
    if (node) { node.textContent = message; node.classList.toggle('error', error); }
  }
  error() { return 'Bitte Verbindung und Einstellungen prüfen.'; }
  destination(value) {
    if (!value) return 'nicht eingerichtet';
    try { const url = new URL(value); return `${url.protocol}//${url.host}${url.pathname}`; }
    catch (_) { return 'konfiguriert'; }
  }
  async load() {
    this._loaded = true;
    this._alive = true;
    this.status('Einstellungen werden geladen …');
    try {
      const data = await this.call('get');
      if (!this._alive) return;
      this._settings = data.settings || {};
      this._entities = Array.isArray(data.entities) ? data.entities : [];
      this.fillSettings();
      this.renderActions();
      this.status('Einstellungen geladen.');
      void this.discoverModels();
      if (!this.$('#panel-history').hidden) await this.loadHistory();
    } catch (error) {
      this._loaded = false;
      this.status(`Laden fehlgeschlagen: ${this.error(error)}`, true);
    }
  }
  fillSettings() {
    const s = this._settings;
    for (const key of ['stt_url', 'stt_model', 'llm_url', 'llm_model', 'system_prompt']) this.$(`#${key}`).value = s[key] || '';
    this.$('#history_enabled').checked = !!s.history_enabled;
    for (const service of ['stt', 'llm']) {
      this.$(`#${service}_token`).value = '';
      this.$(`#${service}_token_mode`).value = 'keep';
      this.$(`#${service}_token_state`).textContent = s[`${service}_has_token`] ? 'Token gespeichert' : 'Kein Token gespeichert';
      this.tokenMode(service);
    }
    this.destinations();
  }
  destinations() {
    const s = this._settings || {};
    this.$('#destinations').textContent = `Gespeicherte Ziele: Sprache → ${this.destination(s.stt_url)} · Text und Kontext → ${this.destination(s.llm_url)}. Tests verwenden die gespeicherten Einstellungen.`;
  }
  tokenMode(service) {
    const input = this.$(`#${service}_token`);
    const changing = this.$(`#${service}_token_mode`).value === 'replace';
    input.disabled = !changing;
    input.required = changing;
    if (!changing) input.value = '';
  }
  async discoverModels() {
    const endpoint = this.$('#llm_url');
    const model = this.$('#llm_model');
    const list = this.$('#llm_models');
    const status = this.$('#model_status');
    const request = ++this._modelRequest;
    list.replaceChildren();
    status.textContent = 'Modell-ID eingeben oder Modelle laden.';
    const url = endpoint.value.trim();
    if (!this._loaded || !url || !endpoint.validity.valid) return;
    const mode = this.$('#llm_token_mode').value;
    const token = this.$('#llm_token').value;
    if (mode === 'replace' && !token) return;
    const payload = { url };
    if (mode !== 'keep') payload.token = mode === 'replace' ? token : '';
    status.textContent = 'Modelle werden geladen …';
    try {
      const data = await this.call('models', payload);
      if (request !== this._modelRequest || url !== endpoint.value.trim() || token !== this.$('#llm_token').value) return;
      if (data.error) {
        status.textContent = `Modellliste nicht verfügbar: ${data.error} Du kannst eine Modell-ID manuell eingeben.`;
        return;
      }
      const models = Array.isArray(data.models) ? data.models : [];
      for (const name of models) {
        const option = document.createElement('option');
        option.value = name;
        list.append(option);
      }
      if (!model.value && models.length === 1) model.value = models[0];
      status.textContent = models.length ? `${models.length} Modelle gefunden. Modell auswählen oder ID eingeben.` : 'Keine Modelle gefunden. Modell-ID manuell eingeben.';
    } catch (error) {
      if (request === this._modelRequest) status.textContent = `Modellliste konnte nicht geladen werden: ${this.error(error)}`;
    }
  }
  selectTab(tab) {
    for (const button of this.shadowRoot.querySelectorAll('[role="tab"]')) {
      const active = button === tab;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      this.$(`#${button.getAttribute('aria-controls')}`).hidden = !active;
    }
    if (tab.id === 'tab-history' && this._loaded) {
      this._offset = 0;
      void this.loadHistory();
    }
  }
  async save(event) {
    event.preventDefault();
    const button = this.$('#save');
    const settings = {};
    for (const key of ['stt_url', 'stt_model', 'llm_url', 'llm_model', 'system_prompt']) settings[key] = this.$(`#${key}`).value;
    settings.history_enabled = this.$('#history_enabled').checked;
    for (const service of ['stt', 'llm']) {
      const mode = this.$(`#${service}_token_mode`).value;
      if (mode === 'replace') settings[`${service}_token`] = this.$(`#${service}_token`).value;
      if (mode === 'clear') settings[`${service}_token`] = '';
    }
    button.disabled = true;
    this.status('Einstellungen werden gespeichert …');
    try {
      await this.call('save', { settings });
      const data = await this.call('get');
      this._settings = data.settings || {};
      this._entities = Array.isArray(data.entities) ? data.entities : [];
      this.fillSettings();
      this.renderActions();
      this.status('Einstellungen gespeichert.');
      void this.discoverModels();
    } catch (error) { this.status(`Speichern fehlgeschlagen: ${this.error(error)}`, true); }
    finally { button.disabled = false; }
  }
  renderActions() {
    const list = this.$('#actions');
    list.replaceChildren();
    const query = this.$('#search').value.trim().toLocaleLowerCase();
    let count = 0;
    for (const entity of this._entities || []) {
      const actions = Array.isArray(entity.actions) ? entity.actions : [];
      const matchesEntity = [entity.name, entity.id, entity.area, entity.state].some(v => String(v || '').toLocaleLowerCase().includes(query));
      const filtered = actions.filter(a => matchesEntity || [a.label, a.id].some(v => String(v || '').toLocaleLowerCase().includes(query)));
      if (!filtered.length) continue;
      count++;
      const group = document.createElement('section'); group.className = 'entity';
      const title = document.createElement('h3'); title.textContent = entity.name || entity.id || 'Entität';
      const meta = document.createElement('p'); meta.className = 'meta'; meta.textContent = [entity.id, entity.area, entity.state].filter(v => v != null && v !== '').join(' · ');
      group.append(title, meta);
      for (const action of filtered) {
        const label = document.createElement('label'); label.className = 'action';
        const text = document.createElement('span');
        const name = document.createElement('strong'); name.textContent = action.label || action.id || 'Aktion';
        text.append(name);
        if (action.sensitive) { const warning = document.createElement('small'); warning.textContent = 'Sensibel · nach Freigabe ohne weitere Bestätigung ausführbar'; text.append(warning); }
        const check = document.createElement('input'); check.type = 'checkbox'; check.checked = !!(action.configured ?? action.enabled);
        check.setAttribute('aria-label', `${action.label || action.id} für ${entity.name || entity.id} freigeben`);
        check.addEventListener('change', async () => {
          const previous = !check.checked; check.disabled = true;
          try { await this.call('action', { entity_id: entity.id, action: action.id, enabled: check.checked }); action.enabled = check.checked; this.status('Aktionsfreigabe gespeichert.'); }
          catch (error) { check.checked = previous; this.status(`Aktionsfreigabe fehlgeschlagen: ${this.error(error)}`, true); }
          finally { check.disabled = false; }
        });
        label.append(text, check); group.append(label);
      }
      list.append(group);
    }
    if (!count) { const empty = document.createElement('p'); empty.className = 'meta'; empty.textContent = 'Keine passenden Aktionen gefunden.'; list.append(empty); }
  }
  async testLLM(event) {
    event.preventDefault();
    const button = this.$('#llm_test'); button.disabled = true;
    const result = this.$('#llm_result'); result.textContent = 'Test läuft …';
    try {
      const data = await this.call('test_llm', { prompt: this.$('#test_prompt').value, language: this.$('#llm_language').value });
      result.textContent = data.error ? `Test fehlgeschlagen: ${data.error}` : `Antwort: ${data.answer || 'Keine Antwort'}\nWerkzeuge: ${this.format(data.tools)}`;
    } catch (error) { result.textContent = `Test fehlgeschlagen: ${this.error(error)}`; }
    finally {
      button.disabled = false;
      if (this._alive && !this.$('#panel-history').hidden) void this.loadHistory();
    }
  }
  format(value) {
    if (value == null || (Array.isArray(value) && !value.length)) return 'Keine';
    return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  }
  async record() {
    if (this._recorder && this._recorder.state !== 'inactive') { this.stopRecording(); return; }
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      this.$('#stt_result').textContent = 'Mikrofon nicht verfügbar. HTTPS oder localhost, Mikrofonberechtigung und ein unterstützter Browser sind erforderlich.'; return;
    }
    const button = this.$('#record'); button.disabled = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!this._alive) { stream.getTracks().forEach(t => t.stop()); return; }
      this._stream = stream;
      const recorder = this._recorder = new MediaRecorder(stream);
      const chunks = [];
      recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
      recorder.onerror = () => { this.$('#stt_result').textContent = 'Aufnahme fehlgeschlagen.'; this.stopRecording(true); };
      recorder.onstop = async () => {
        this.releaseStream();
        button.textContent = 'Aufnahme starten'; button.disabled = false;
        if (this._discardRecording || !this._alive) { this._discardRecording = false; return; }
        const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
        if (!blob.size) { this.$('#stt_result').textContent = 'Die Aufnahme ist leer.'; return; }
        await this.sendAudio(blob);
      };
      this._discardRecording = false;
      recorder.start();
      button.textContent = 'Aufnahme stoppen und testen';
      this.$('#stt_result').textContent = 'Aufnahme läuft · maximal 30 Sekunden';
      this._recordTimer = setTimeout(() => this.stopRecording(), 30000);
    } catch (error) { this.releaseStream(); this.$('#stt_result').textContent = `Mikrofon nicht verfügbar: ${this.error(error)}`; }
    finally { button.disabled = false; }
  }
  releaseStream() { if (this._stream) this._stream.getTracks().forEach(t => t.stop()); this._stream = null; }
  stopRecording(discard = false) {
    clearTimeout(this._recordTimer);
    if (discard) this._discardRecording = true;
    if (this._recorder && this._recorder.state !== 'inactive') this._recorder.stop();
    else this.releaseStream();
  }
  async wav(blob) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return blob;
    const context = new AudioContextClass({ sampleRate: 16000 });
    try {
      const decoded = await context.decodeAudioData(await blob.arrayBuffer());
      const length = decoded.length, channels = 1;
      const buffer = new ArrayBuffer(44 + length * channels * 2), view = new DataView(buffer);
      const str = (offset, s) => { for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i)); };
      str(0, 'RIFF'); view.setUint32(4, 36 + length * channels * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
      view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, channels, true);
      view.setUint32(24, decoded.sampleRate, true); view.setUint32(28, decoded.sampleRate * channels * 2, true);
      view.setUint16(32, channels * 2, true); view.setUint16(34, 16, true); str(36, 'data'); view.setUint32(40, length * channels * 2, true);
      const samples = Array.from({ length: decoded.numberOfChannels }, (_, i) => decoded.getChannelData(i));
      let offset = 44;
      for (let i = 0; i < length; i++) {
        const value = samples.reduce((sum, channel) => sum + channel[i], 0) / samples.length;
        const clamped = Math.max(-1, Math.min(1, value));
        view.setInt16(offset, clamped < 0 ? clamped * 32768 : clamped * 32767, true); offset += 2;
      }
      return new Blob([buffer], { type: 'audio/wav' });
    } finally { await context.close(); }
  }
  async sendAudio(blob) {
    const button = this.$('#record'); button.disabled = true;
    const result = this.$('#stt_result'); result.textContent = 'Sprachtest läuft …';
    try {
      try { blob = await this.wav(blob); } catch (_) { /* Use the browser recording format if decoding fails. */ }
      const audio = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('Aufnahme konnte nicht gelesen werden.')); reader.readAsDataURL(blob); });
      const data = await this.call('test_stt', { audio, mime: blob.type, language: this.$('#stt_language').value });
      result.textContent = data.error ? `Test fehlgeschlagen: ${data.error}` : `Transkript: ${data.transcript || 'Kein Text erkannt'}`;
    } catch (error) { result.textContent = `Test fehlgeschlagen: ${this.error(error)}`; }
    finally {
      button.disabled = false;
      if (this._alive && !this.$('#panel-history').hidden) void this.loadHistory();
    }
  }
  revokeAudio() { for (const url of this._urls) URL.revokeObjectURL(url); this._urls.clear(); }
  async loadHistory() {
    const list = this.$('#history'); list.textContent = 'Verlauf wird geladen …';
    try {
      const data = await this.call('history', { limit: 20, offset: this._offset });
      this.revokeAudio(); list.replaceChildren();
      const entries = Array.isArray(data.entries) ? data.entries : [];
      if (!entries.length) list.textContent = 'Keine Einträge vorhanden.';
      for (const entry of entries) {
        const article = document.createElement('article'); article.className = 'history-entry';
        const head = document.createElement('h3'); head.textContent = `${entry.test ? 'Test' : 'Live'} · ${entry.kind || 'Eintrag'} · ${entry.time || 'Zeit unbekannt'}`; article.append(head);
        for (const [key, label] of [['input', 'Eingabe'], ['transcript', 'Transkript'], ['answer', 'Antwort'], ['tools', 'Werkzeuge'], ['error', 'Fehler']]) {
          if (entry[key] == null || entry[key] === '') continue;
          const p = document.createElement('p'); const strong = document.createElement('strong'); strong.textContent = `${label}: `; p.className = key === 'error' || key === 'tools' ? 'result' : ''; p.append(strong, document.createTextNode(this.format(entry[key]))); article.append(p);
        }
        if (entry.has_audio) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Audio laden';
          button.addEventListener('click', async () => {
            button.disabled = true;
            try {
              const data = await this.call('audio', { id: entry.id });
              const bytes = Uint8Array.from(atob(data.audio), c => c.charCodeAt(0));
              const url = URL.createObjectURL(new Blob([bytes], { type: data.mime || 'audio/wav' })); this._urls.add(url);
              const audio = document.createElement('audio'); audio.controls = true; audio.src = url; audio.setAttribute('aria-label', 'Aufnahme abspielen'); button.replaceWith(audio);
            } catch (error) { this.status(`Audio konnte nicht geladen werden: ${this.error(error)}`, true); button.disabled = false; }
          }); article.append(button);
        }
        list.append(article);
      }
      const total = Number(data.total) || 0;
      this.$('#page').textContent = total ? `${this._offset + 1}–${this._offset + entries.length} von ${total}` : '0 Einträge';
      this.$('#prev').disabled = this._offset === 0; this.$('#next').disabled = this._offset + entries.length >= total || !entries.length;
    } catch (error) { list.textContent = `Verlauf konnte nicht geladen werden: ${this.error(error)}`; }
  }
  async clearHistory() {
    if (!window.confirm('Den gesamten Verlauf einschließlich gespeicherter Aufnahmen endgültig löschen?')) return;
    const button = this.$('#clear'); button.disabled = true;
    try { await this.call('clear'); this._offset = 0; await this.loadHistory(); this.status('Verlauf gelöscht.'); }
    catch (error) { this.status(`Löschen fehlgeschlagen: ${this.error(error)}`, true); }
    finally { button.disabled = false; }
  }
  render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color,#202124);background:var(--primary-background-color,#f5f5f5);font-family:var(--paper-font-body1_-_font-family,Roboto,Arial,sans-serif);min-height:100%;font-size:16px;line-height:1.5}*{box-sizing:border-box}header{height:64px;display:flex;align-items:center;gap:12px;padding:0 24px;background:var(--app-header-background-color,var(--primary-color,#03a9f4));color:var(--app-header-text-color,#fff)}header h1{font-size:20px;font-weight:500;margin:0}main{max-width:1100px;margin:auto;padding:30px 24px 64px}.intro{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:24px}.eyebrow{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--secondary-text-color,#555);margin:0 0 6px}h2{font-size:22px;line-height:1.25;margin:0 0 8px}h3{font-size:16px;margin:0}p{margin:8px 0}.muted,.meta,small{color:var(--secondary-text-color,#555)}.signal{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--divider-color,#ddd);border:1px solid var(--divider-color,#ddd);border-radius:12px;overflow:hidden;margin-bottom:24px}.stage{background:var(--card-background-color,#fff);padding:16px}.stage b{display:block}.stage span{font-size:13px;color:var(--secondary-text-color,#555)}.grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(280px,1fr);gap:20px;align-items:start}.stack{display:grid;gap:20px}.card{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#ddd);border-radius:12px;padding:22px;min-width:0}.card-head{margin-bottom:18px}.card-head p{font-size:14px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:14px}.wide{grid-column:1/-1}label.field{display:grid;gap:5px;font-size:14px;font-weight:600}input,textarea,select,button{font:inherit;color:inherit}input:not([type=checkbox]),textarea,select{width:100%;border:1px solid var(--divider-color,#aaa);border-radius:6px;background:var(--primary-background-color,#fafafa);padding:10px;min-height:44px}textarea{resize:vertical;min-height:100px}input[type=checkbox]{width:20px;height:20px;accent-color:var(--primary-color,#03a9f4);flex:none}button{min-height:44px;border:1px solid var(--divider-color,#aaa);border-radius:6px;background:var(--card-background-color,#fff);padding:9px 14px;cursor:pointer}button.primary{background:var(--primary-color,#0277bd);color:var(--text-primary-color,#fff);border-color:transparent}button:disabled{opacity:.55;cursor:not-allowed}:focus-visible{outline:3px solid var(--primary-color,#0277bd);outline-offset:2px}.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.checkrow{display:flex;align-items:center;gap:10px;font-weight:600}.notice{border-left:3px solid var(--primary-color,#03a9f4);padding:4px 0 4px 12px;font-size:14px;overflow-wrap:anywhere}.divider{border:0;border-top:1px solid var(--divider-color,#ddd);margin:20px 0}.result{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}.entity{padding:14px 0;border-top:1px solid var(--divider-color,#ddd)}.entity .meta{font-size:13px;margin:2px 0 8px;overflow-wrap:anywhere}.action{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;cursor:pointer}.action strong{font-size:14px;font-weight:500}.action small{display:block;font-size:12px}.history-entry{border-top:1px solid var(--divider-color,#ddd);padding:15px 0;overflow-wrap:anywhere}.history-entry h3{font-size:14px}.history-entry p{font-size:14px;white-space:pre-wrap}.history-entry audio{max-width:100%}.status{min-height:24px;font-size:14px}.error{color:var(--error-color,#b00020)}.menu{padding:0;width:44px;border:0;background:transparent;color:inherit;font-size:24px}.subheading{font-size:16px;margin:20px 0 12px}@media(max-width:760px){header{padding:0 12px}main{padding:22px 16px 48px}.intro{display:block}.grid{grid-template-columns:1fr}.signal{grid-template-columns:1fr}.stage{padding:12px 16px}.fields{grid-template-columns:1fr}.wide{grid-column:auto}.card{padding:18px}}
      [hidden]{display:none!important}.tabs{display:flex;flex-wrap:wrap;gap:6px;border-bottom:1px solid var(--divider-color,#ddd);margin-bottom:22px}.tabs button{background:none;border:0;border-bottom:3px solid transparent;border-radius:0}.tabs [aria-selected="true"]{border-bottom-color:var(--primary-color,#0277bd);font-weight:600}.model-controls{margin-top:10px}.model-controls .meta{overflow-wrap:anywhere}
    </style><header><button class="menu" id="menu" type="button" aria-label="Menü öffnen">☰</button><h1>Auralis</h1></header><main>
      <div class="intro"><div><p class="eyebrow">Home Assistant · Verwaltung</p><h2>Deine Sprachbrücke</h2><p class="muted">Dienste verbinden, Aktionen freigeben und Anfragen nachvollziehen.</p></div></div>
      <div class="tabs" role="tablist" aria-label="Auralis">
        <button id="tab-config" type="button" role="tab" aria-selected="true" aria-controls="panel-config">Konfiguration</button>
        <button id="tab-history" type="button" role="tab" aria-selected="false" aria-controls="panel-history" tabindex="-1">Verlauf</button>
        <button id="tab-actions" type="button" role="tab" aria-selected="false" aria-controls="panel-actions" tabindex="-1">Erlaubte Aktionen</button>
      </div>
      <p id="status" class="status" role="status" aria-live="polite"></p>
      <div id="panel-config" role="tabpanel" aria-labelledby="tab-config" tabindex="0">
      <div class="signal" aria-label="Signalweg"><div class="stage"><span>01 · Spracheingabe</span><b>STT</b><span>Sprache wird zu Text</span></div><div class="stage"><span>02 · Verarbeitung</span><b>LLM</b><span>Text und Aktionen</span></div><div class="stage"><span>03 · Sprachausgabe</span><b>Piper</b><span>Auswahl durch Home Assistant</span></div></div>
        <div class="grid">
      <section class="card"><div class="card-head"><p class="eyebrow">01 / Verbindung</p><h2>Dienste & Einstellungen</h2><p class="muted">Änderungen gelten nach dem Speichern.</p></div><p id="destinations" class="notice"></p><form id="settings"><h3 class="subheading">Spracheingabe · STT</h3><div class="fields"><label class="field">STT-URL<input id="stt_url" type="url" autocomplete="off"></label><label class="field">Modell<input id="stt_model" autocomplete="off"></label><label class="field">Token <span id="stt_token_state" class="meta"></span><select id="stt_token_mode"><option value="keep">Beibehalten</option><option value="replace">Ersetzen</option><option value="clear">Löschen</option></select></label><label class="field">Neues STT-Token<input id="stt_token" type="password" autocomplete="new-password" disabled></label></div><h3 class="subheading">Verarbeitung · LLM</h3><div class="fields"><label class="field">LLM-Basis-URL<input id="llm_url" type="url" placeholder="https://api.example.com/v1" autocomplete="off"></label><label class="field">Modell<input id="llm_model" list="llm_models" aria-describedby="model_status" autocomplete="off"><datalist id="llm_models"></datalist></label><label class="field">Token <span id="llm_token_state" class="meta"></span><select id="llm_token_mode"><option value="keep">Beibehalten</option><option value="replace">Ersetzen</option><option value="clear">Löschen</option></select></label><label class="field">Neues LLM-Token<input id="llm_token" type="password" autocomplete="new-password" disabled></label><div class="wide model-controls"><button id="load_models" type="button">Modelle laden</button><p id="model_status" class="meta" role="status"></p></div><label class="field wide">System-Prompt<textarea id="system_prompt"></textarea></label><label class="checkrow wide"><input id="history_enabled" type="checkbox"> Verlauf speichern</label></div><p class="meta">Der Verlauf kann Texte, Werkzeugaufrufe und Aufnahmen enthalten.</p><button id="save" class="primary" type="submit">Einstellungen speichern</button></form></section>
      <section class="card"><div class="card-head"><p class="eyebrow">03 / Test</p><h2>Sprachtest</h2><p class="muted">Eine Aufnahme an das gespeicherte STT-Ziel senden. Maximal 30 Sekunden.</p></div><label class="field">Sprache<input id="stt_language" value="de" autocomplete="off"></label><p class="meta">Mikrofonzugriff benötigt HTTPS oder localhost und deine Erlaubnis.</p><button id="record" type="button">Aufnahme starten</button><p id="stt_result" class="result" role="status" aria-live="polite"></p><hr class="divider"><h3>LLM-Probelauf</h3><p class="meta">Testet die Verarbeitung mit den gespeicherten Einstellungen.</p><form id="llm_form"><label class="field">Sprache<input id="llm_language" value="de" autocomplete="off"></label><label class="field">Testnachricht<textarea id="test_prompt" required></textarea></label><p><button id="llm_test" type="submit">LLM testen</button></p></form><p id="llm_result" class="result" role="status" aria-live="polite"></p></section>
        </div>
      </div>
      <div id="panel-history" role="tabpanel" aria-labelledby="tab-history" tabindex="0" hidden>
      <section class="card"><div class="card-head"><p class="eyebrow">04 / Protokoll</p><h2>Verlauf</h2><p class="muted">Test und Live sind für jeden Eintrag gekennzeichnet.</p></div><div id="history" aria-live="polite"></div><div class="row"><button id="prev" type="button" disabled>Zurück</button><span id="page" class="meta"></span><button id="next" type="button" disabled>Weiter</button></div><hr class="divider"><button id="clear" type="button">Gesamten Verlauf löschen</button></section>
      </div>
      <div id="panel-actions" role="tabpanel" aria-labelledby="tab-actions" tabindex="0" hidden>
      <section class="card"><div class="card-head"><p class="eyebrow">02 / Freigaben</p><h2>Aktionen</h2><p class="muted">Freigegebene Aktionen dürfen im Live-Betrieb ausgeführt werden. Sensible Aktionen laufen nach Freigabe ohne weitere Bestätigung.</p></div><label class="field">Entität oder Aktion suchen<input id="search" type="search" placeholder="Name, Raum oder Aktion"></label><div id="actions"></div></section>
      </div>
    </main>`;
    this.$('#menu').addEventListener('click', () => this.dispatchEvent(new Event('hass-toggle-menu', { bubbles: true, composed: true })));
    this.$('#settings').addEventListener('submit', e => this.save(e));
    const navigation = this.$('[role="tablist"]');
    const tabs = [...navigation.querySelectorAll('[role="tab"]')];
    for (const tab of tabs) tab.addEventListener('click', () => this.selectTab(tab));
    navigation.addEventListener('keydown', event => {
      const current = tabs.indexOf(event.target);
      const next = { ArrowRight: (current + 1) % tabs.length, ArrowLeft: (current + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 }[event.key];
      if (current < 0 || next === undefined) return;
      event.preventDefault();
      this.selectTab(tabs[next]);
      tabs[next].focus();
    });
    this.$('#load_models').addEventListener('click', () => this.discoverModels());
    for (const id of ['llm_url', 'llm_token_mode', 'llm_token']) {
      this.$(`#${id}`).addEventListener('change', () => this.discoverModels());
    }
    for (const service of ['stt', 'llm']) this.$(`#${service}_token_mode`).addEventListener('change', () => this.tokenMode(service));
    this.$('#search').addEventListener('input', () => this.renderActions());
    this.$('#llm_form').addEventListener('submit', e => this.testLLM(e));
    this.$('#record').addEventListener('click', () => this.record());
    this.$('#prev').addEventListener('click', () => { this._offset = Math.max(0, this._offset - 20); this.loadHistory(); });
    this.$('#next').addEventListener('click', () => { this._offset += 20; this.loadHistory(); });
    this.$('#clear').addEventListener('click', () => this.clearHistory());
  }
}
if (!customElements.get('auralis-panel')) customElements.define('auralis-panel', AuralisPanel);
