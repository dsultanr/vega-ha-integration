/* Explicit, device-scoped setup of passive HV battery telemetry. */
class VegaHvSetup extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._generation = 0;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; margin-bottom:20px; }
        section { padding:20px; border:1px solid var(--divider-color); border-radius:12px; background:var(--card-background-color); }
        h2 { margin:0 0 12px; font-size:20px; font-weight:500; }
        p { line-height:1.5; }
        .hint { color:var(--secondary-text-color); font-size:14px; }
        .values { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:18px 0; }
        .value { font-size:22px; display:block; margin-bottom:4px; }
        .label { color:var(--secondary-text-color); font-size:13px; }
        label { display:block; margin:16px 0 6px; }
        select { box-sizing:border-box; width:100%; max-width:360px; padding:10px; border:1px solid var(--divider-color); border-radius:8px; color:var(--primary-text-color); background:var(--card-background-color); font:inherit; }
        .actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
        button { padding:11px 16px; border:0; border-radius:8px; cursor:pointer; color:var(--text-primary-color,white); background:var(--primary-color); font:inherit; }
        button:disabled { opacity:.45; cursor:default; }
        #hv-status { white-space:pre-wrap; overflow-wrap:anywhere; }
        .error { color:var(--error-color,#db4437); }
        [hidden] { display:none !important; }
        @media(max-width:480px) { section {padding:16px 12px;} .values {gap:8px;} .value {font-size:19px;} }
      </style>
      <section aria-labelledby="hv-title">
        <h2 id="hv-title">Мощность ВВБ</h2>
        <p class="hint">Напряжение, ток и мгновенная мощность тяговой батареи с трекера.
          Плюс — разряд, минус — зарядка или рекуперация.</p>
        <div class="values">
          <div><span id="hv-power" class="value">—</span><span class="label">Мощность</span></div>
          <div><span id="hv-voltage" class="value">—</span><span class="label">Напряжение</span></div>
          <div><span id="hv-current" class="value">—</span><span class="label">Ток</span></div>
        </div>
        <label for="hv-profile">Профиль автомобиля</label>
        <select id="hv-profile">
          <option value="">Выберите профиль</option>
          <option value="f515_ispace_4wd">F515 / i-Space 4WD</option>
          <option value="ispace_2025">i-Space 2025 (не проверено на автомобиле)</option>
        </select>
        <p class="hint">Сначала проверим совместимость и свободное место в настройках трекера.
          Для проверки и настройки нужно соединение Vega Binary.</p>
        <div class="actions">
          <button id="hv-check" disabled>Проверить настройки</button>
          <button id="hv-apply" hidden>Настроить измерение мощности ВВБ</button>
        </div>
        <p id="hv-plan" class="hint" hidden></p>
        <p id="hv-control" class="hint" hidden>Для записи включите Direct Control на странице устройства.</p>
        <p id="hv-status" role="status" aria-live="polite">Настройки ещё не проверены.</p>
      </section>`;
    this.shadowRoot.querySelector("#hv-profile").addEventListener("change", () => {
      this._plan = null;
      this._notice = "";
      this._error = false;
      this._render();
    });
    this.shadowRoot.querySelector("#hv-check").addEventListener("click", () => this._run(false));
    this.shadowRoot.querySelector("#hv-apply").addEventListener("click", () => this._run(true));
  }

  setConfig(config) {
    this._config = {...config};
    this._generation++;
    this._plan = null;
    this._busy = false;
    this._notice = "";
    this._error = false;
    this.shadowRoot.querySelector("#hv-profile").value = "";
    this._render();
  }

  set hass(hass) { this._hass = hass; this._render(); }

  _render() {
    const $ = id => this.shadowRoot.querySelector(id);
    const attrs = this._hass?.states[this._config?.entity]?.attributes || {};
    const validEntry = attrs.entry_id === this._config?.entry_id;
    const ready = !!this._plan && !this._plan.configured;
    $("#hv-profile").disabled = !!this._busy;
    $("#hv-check").disabled = !!this._busy || !validEntry || !$("#hv-profile").value;
    $("#hv-apply").hidden = !ready;
    $("#hv-apply").disabled = !!this._busy || !validEntry || !attrs.direct_control;
    $("#hv-control").hidden = !ready || !!attrs.direct_control;
    $("#hv-plan").hidden = !ready;
    $("#hv-plan").textContent = ready
      ? "Будут настроены датчики напряжения и тока с периодом записи 5 секунд. Исходные настройки сохранятся в резервной копии." : "";
    const progress = attrs.hv_setup;
    const active = this._busy && validEntry && ["reading", "writing"].includes(progress?.phase);
    $("#hv-status").textContent = active
      ? `${progress.message}${progress.total ? ` (${progress.progress}/${progress.total})` : ""}`
      : this._notice || (validEntry && progress?.message) || "Настройки ещё не проверены.";
    $("#hv-status").className = this._error ? "error" : "";
    for (const [key, unit] of [["power", "кВт"], ["voltage", "В"], ["current", "А"]]) {
      const state = this._hass?.states[this._config?.[`${key}_entity`]];
      const value = state?.state;
      $("#hv-" + key).textContent = validEntry && value != null && value !== "" && Number.isFinite(Number(value))
        ? `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:3})} ${unit}` : "—";
    }
  }

  async _run(apply) {
    if (this._busy || !this._hass || !this._config) return;
    const attrs = this._hass.states[this._config.entity]?.attributes;
    if (attrs?.entry_id !== this._config.entry_id) return;
    if (apply && (!this._plan || this._plan.configured || !attrs.direct_control)) return;
    const profile = this.shadowRoot.querySelector("#hv-profile").value;
    if (!profile) return;
    const generation = this._generation;
    const data = apply ? {plan_id: this._plan.plan_id} : {profile};
    this._busy = true;
    this._error = false;
    this._notice = apply ? "Повторно проверяю настройки перед записью…" : "Читаю настройки трекера…";
    this._render();
    try {
      const result = await this._hass.callWS({type:"call_service", domain:"vega",
        service:apply ? "configure_hv_battery" : "prepare_hv_battery",
        service_data:{entry_id:this._config.entry_id, ...data}, return_response:true});
      if (generation !== this._generation) return;
      const response = result?.response;
      if (!response || (apply ? response.verified !== true : !response.plan_id)) {
        throw new Error("Не получено подтверждение. Проверьте настройки заново.");
      }
      this._plan = apply ? null : response;
      this._notice = apply ? "Настройки сохранены и проверены. Показания появятся после получения свежих данных."
        : response.configured ? "Измерение мощности ВВБ уже настроено. Запись не требуется."
        : "Трекер совместим. Можно настроить измерение мощности ВВБ.";
    } catch (error) {
      if (generation !== this._generation) return;
      this._plan = null;
      this._error = true;
      this._notice = error.message || String(error);
    } finally {
      if (generation === this._generation) { this._busy = false; this._render(); }
    }
  }
}

if (!customElements.get("vega-hv-setup")) customElements.define("vega-hv-setup", VegaHvSetup);
