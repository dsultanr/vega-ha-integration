/* Portable entry point: /vega-config; device links: /vega-config/<config_entry_id>.
 * Resolve the editor through HA's entity registry, never by a guessed entity ID
 * or by picking the first available tracker. Reads/writes remain user actions.
 */
const editorUrl = new URL("./vega-can-editor.js", import.meta.url);
editorUrl.search = new URL(import.meta.url).search;
await import(editorUrl.href);
const hvUrl = new URL("./vega-hv-setup.js", import.meta.url);
hvUrl.search = new URL(import.meta.url).search;
await import(hvUrl.href);

class VegaConfigPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._request = 0;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; height:100%; overflow:auto; color:var(--primary-text-color); background:var(--primary-background-color); }
        header { padding:16px 24px; border-bottom:1px solid var(--divider-color); background:var(--app-header-background-color, var(--card-background-color)); color:var(--app-header-text-color, var(--primary-text-color)); }
        a { color:inherit; text-decoration:none; display:inline-block; padding:6px 0; }
        h1 { font-size:22px; font-weight:500; margin:8px 0 0; }
        main { max-width:1000px; margin:0 auto; padding:20px; }
        #status { white-space:pre-wrap; color:var(--secondary-text-color); }
        #status.error { color:var(--error-color); }
        #trackers { margin-left:24px; }
        #choices { display:grid; gap:12px; margin:16px 0; }
        .tracker { display:block; padding:16px; border:1px solid var(--divider-color); border-radius:12px; background:var(--card-background-color); overflow-wrap:anywhere; }
        .tracker:hover, .tracker:focus-visible { border-color:var(--primary-color); }
        .tracker strong, .tracker span { display:block; }
        .tracker span { margin-top:6px; font-size:14px; color:var(--secondary-text-color); }
        button { margin:8px 0 16px; padding:10px 16px; border:0; border-radius:8px; cursor:pointer; color:var(--text-primary-color, white); background:var(--primary-color); }
        [hidden] { display:none !important; }
        @media(max-width:600px) { header { padding:12px 16px; } main { padding:12px 8px; } h1 { font-size:20px; } }
      </style>
      <header><a id="back" href="/config/devices/dashboard">← Устройства</a><a id="trackers" href="/vega-config" hidden>Все трекеры</a><h1>Настройки Vega</h1></header>
      <main><p id="status" role="status">Загружаю список трекеров…</p>
        <nav id="choices" aria-label="Трекеры Vega" hidden></nav>
        <button id="retry" hidden>Обновить</button><div id="editor" hidden></div></main>`;
    this.shadowRoot.querySelector("#retry").addEventListener("click", () => {
      this._attempted = false;
      this._resolve();
    });
    // HA can create this element and assign its properties before the module
    // finishes loading. Replay those own properties through the new setters.
    for (const key of ["route", "hass"]) {
      if (Object.prototype.hasOwnProperty.call(this, key)) {
        const value = this[key];
        delete this[key];
        this[key] = value;
      }
    }
  }

  set route(route) {
    const path = typeof route?.path === "string" ? route.path : null;
    if (this._routeSet && path === this._routePath) return;
    this._routeSet = true;
    this._routePath = path;
    // null means the portable root; undefined means an invalid URL.
    this._routeEntry = path === "" || path === "/" ? null : path?.match(/^\/([A-Za-z0-9_-]+)\/?$/)?.[1];
    this._entryId = this._routeEntry;
    this._request++;
    this._attempted = false;
    this._row = null;
    this._editor = null;
    this._hv = null;
    this.shadowRoot.querySelector("#editor").replaceChildren();
    this.shadowRoot.querySelector("#editor").hidden = true;
    this.shadowRoot.querySelector("#choices").hidden = true;
    this.shadowRoot.querySelector("#trackers").hidden = !this._routeEntry;
    const back = this.shadowRoot.querySelector("#back");
    back.href = "/config/devices/dashboard";
    back.textContent = "← Устройства";
    this._resolve();
  }

  set hass(hass) {
    this._hass = hass;
    this._resolve();
    this._updateEditor();
  }

  _message(text, retry = false, error = false) {
    const status = this.shadowRoot.querySelector("#status");
    status.textContent = text;
    status.hidden = !text;
    status.className = error ? "error" : "";
    this.shadowRoot.querySelector("#retry").hidden = !retry;
  }

  async _resolve() {
    if (!this._hass || !this._routeSet) return;
    if (this._routeEntry === undefined) {
      this._message("Неверный адрес редактора. Откройте список трекеров.", false, true);
      this.shadowRoot.querySelector("#trackers").hidden = false;
      return;
    }
    if (this._attempted) return;
    this._attempted = true;
    this._row = null;
    this.shadowRoot.querySelector("#editor").hidden = true;
    this.shadowRoot.querySelector("#choices").hidden = true;
    const request = ++this._request;
    let entry = this._routeEntry;
    this._entryId = entry;
    if (entry === null) {
      const back = this.shadowRoot.querySelector("#back");
      back.href = "/config/devices/dashboard";
      back.textContent = "← Устройства";
    }
    this._message("Загружаю настройки устройства…");
    try {
      const [entities, entries] = await Promise.all([
        this._hass.callWS({type: "config/entity_registry/list"}),
        entry === null ? this._hass.callWS({type: "config_entries/get", domain: "vega"}) : null,
      ]);
      if (request !== this._request) return;
      const editors = entities.filter(e => e.platform === "vega" &&
        e.entity_id.startsWith("sensor.") && e.unique_id?.endsWith("_can_scripts"));
      if (entry === null) {
        const trackers = entries.filter(e => e.domain === "vega" && e.source !== "ignore");
        if (!trackers.length) throw new Error("Трекеры Vega не настроены. Добавьте интеграцию в настройках HA.");
        if (trackers.length > 1) {
          this._showChoices(trackers, editors);
          return;
        }
        entry = trackers[0].entry_id;
        this._entryId = entry;
        if (trackers[0].disabled_by) throw new Error("Интеграция Vega отключена. Включите её в настройках HA.");
      }
      const matches = editors.filter(e => e.config_entry_id === entry);
      if (matches.length !== 1) throw new Error(matches.length
        ? "Для этой интеграции найдено несколько редакторов. Проверьте устройства Vega."
        : "Сенсор команд этого устройства не найден. Проверьте, что интеграция Vega загружена.");
      const row = matches[0];
      if (row.disabled_by) throw new Error("Сенсор CAN Scripts отключён. Включите его на странице устройства.");
      this._row = row;
      this._hvEntities = Object.fromEntries(["voltage", "current", "power"].map(key => {
        const matches = entities.filter(e => e.platform === "vega" && e.config_entry_id === entry &&
          e.unique_id?.endsWith(`_hv_battery_${key}`));
        return [`${key}_entity`, matches.length === 1 ? matches[0].entity_id : null];
      }));
      if (row.device_id) {
        const back = this.shadowRoot.querySelector("#back");
        back.href = `/config/devices/device/${encodeURIComponent(row.device_id)}`;
        back.textContent = "← К устройству";
      }
      this._updateEditor();
    } catch (error) {
      if (request !== this._request) return;
      this.shadowRoot.querySelector("#editor").hidden = true;
      this._message(error.message || String(error), true, true);
    }
  }

  _showChoices(entries, editors) {
    const choices = this.shadowRoot.querySelector("#choices");
    choices.replaceChildren();
    for (const entry of [...entries].sort((a, b) => (a.title || "Vega").localeCompare(b.title || "Vega"))) {
      const rows = editors.filter(row => row.config_entry_id === entry.entry_id);
      const row = rows.length === 1 ? rows[0] : null;
      const device = row && this._hass.devices?.[row.device_id];
      const name = device?.name_by_user || entry.title || device?.name || "Vega";
      const imei = row?.unique_id?.match(/^vega_(\d{15})_can_scripts$/)?.[1];
      const link = document.createElement("a");
      link.className = "tracker";
      link.href = `/vega-config/${encodeURIComponent(entry.entry_id)}`;
      const title = document.createElement("strong");
      title.textContent = name;
      link.appendChild(title);
      const details = [];
      if (imei && !name.includes(imei)) details.push(`IMEI: ${imei}`);
      if (entry.disabled_by) details.push("Интеграция отключена");
      else if (row?.disabled_by) details.push("Сенсор команд отключён");
      else if (!row || !this._hass.states[row.entity_id] || this._hass.states[row.entity_id].state === "unavailable") details.push("Данные пока недоступны");
      if (details.length) {
        const detail = document.createElement("span");
        detail.textContent = details.join(" · ");
        link.appendChild(detail);
      }
      choices.appendChild(link);
    }
    choices.hidden = false;
    this._message("Выберите трекер, команды и сценарии которого хотите открыть.", true);
  }

  _updateEditor() {
    if (!this._row || !this._hass) return;
    const container = this.shadowRoot.querySelector("#editor");
    const state = this._hass.states[this._row.entity_id];
    if (!state || state.attributes.entry_id !== this._entryId || state.state === "unavailable") {
      container.hidden = true;
      this._message("Данные устройства пока недоступны. Проверьте интеграцию Vega или обновите список сущностей.", true);
      return;
    }
    if (!this._editor || this._editorEntity !== this._row.entity_id) {
      this._editor = document.createElement("vega-can-editor");
      this._editor.setConfig({entity: this._row.entity_id});
      this._editorEntity = this._row.entity_id;
      this._hv = document.createElement("vega-hv-setup");
      this._hv.setConfig({entity: this._row.entity_id, entry_id: this._entryId, ...this._hvEntities});
      container.replaceChildren(this._hv, this._editor);
    }
    container.hidden = false;
    this._message("");
    this._editor.hass = this._hass;
    this._hv.hass = this._hass;
  }
}

if (!customElements.get("vega-config-panel")) customElements.define("vega-config-panel", VegaConfigPanel);
