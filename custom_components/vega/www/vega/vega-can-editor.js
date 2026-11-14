/*
 * vega-can-editor
 *
 * Reads and edits command scenarios (4001) and their CAN scripts (5502).
 * Bundles retain the original snapshots for conflict checks and preserve
 * actions outside the supported editor fields on the server.
 *
 * Config:
 *   type: custom:vega-can-editor
 *   entity: sensor.vega_<imei>_can_scripts
 */

function explainCanFrame(bus, canId, dataHex) {
  const d = (dataHex || "").toLowerCase().replace(/\s+/g, "");
  const cid = typeof canId === "string" ? (canId.startsWith("0x") ? parseInt(canId, 16) : parseInt(canId, 10)) : (canId || 0);
  const pfx = "0x" + cid.toString(16).toUpperCase();
  const busLabel = `CAN${bus}`;

  if (cid === 0x701) {
    if (d.startsWith("03220203aa21")) return `UDS (${pfx}, ${busLabel}): Постановка на охрану (закрыть ЦЗ)`;
    if (d.startsWith("032202030000")) return `UDS (${pfx}, ${busLabel}): Снятие с охраны (открыть ЦЗ)`;
    if (d.startsWith("03220203")) return `UDS (${pfx}, ${busLabel}): Запрос состояния/управления ЦЗ (DID 0x0203)`;
    if (d.startsWith("062f010303080055")) return `UDS (${pfx}, ${busLabel}): Включение звукового сигнала (Horn ON)`;
    if (d.startsWith("062f010303000055")) return `UDS (${pfx}, ${busLabel}): Выключение звукового сигнала (Horn OFF)`;
    if (d.startsWith("062f0103")) return `UDS (${pfx}, ${busLabel}): Управление клаксоном (IOCBI DID 0x0103)`;
    return `UDS Diag (${pfx}, ${busLabel}): Диагностический запрос`;
  }
  if (cid === 0x011) {
    const parts = [];
    if (d.startsWith("04")) parts.push("сигнал клаксона");
    if (d.length >= 6) {
      const b12 = d.slice(2, 6);
      if (b12 === "440a") parts.push("включение охлаждения (A/C ON)");
      else if (b12 === "580a") parts.push("команда климата 22°C");
      else if (b12 === "800a") {
        parts.push("максимальный прогрев (Heating ON)");
      } else if (b12 === "0009") parts.push("сброс триггера команды (снятие импульса / idle)");
    }
    if (d.length >= 8 && d.slice(6, 8) === "5b") parts.push("предустановка обдува климата");
    if (d.length >= 12) {
      const b45 = d.slice(8, 12);
      if (b45 === "aa21") parts.push("закрытие центрального замка (Lock)");
      else if (b45 === "0002") parts.push("открытие центрального замка (Unlock)");
    }
    if (d.length >= 12 && d.slice(10, 12) === "05") parts.push("открытие замка багажника");
    if (parts.length) return `BCM (${pfx}, ${busLabel}): ${parts.join(", ")}`;
    return `BCM (${pfx}, ${busLabel}): Команда кузовной электроники`;
  }
  if (cid === 0x092) {
    if (d.length >= 6 && d.slice(4, 6) === "60") return `CAN (${pfx}, ${busLabel}): Кадр из климатических скриптов; самостоятельный Remote On не подтверждён`;
    return `CAN (${pfx}, ${busLabel}): Назначение кадра не подтверждено`;
  }
  if (cid === 0x093) {
    return `HVAC (${pfx}, ${busLabel}): Конфигурация климат-контроля`;
  }
  if (cid === 0x09a) {
    if (d.startsWith("10")) return `Tailgate (${pfx}, ${busLabel}): Открытие электропривода багажника`;
    if (d.startsWith("00")) return `Tailgate (${pfx}, ${busLabel}): Закрытие электропривода багажника`;
    return `Tailgate (${pfx}, ${busLabel}): Модуль двери багажника`;
  }
  if (cid === 0x394) {
    if (d === "fcff0fff001ff000") return `HVAC (${pfx}, ${busLabel}): Запрос максимального прогрева (HI, максимальный нагрев)`;
    if (["7cff0fff001f5000", "fcff0fff001f5000"].includes(d)) return `HVAC (${pfx}, ${busLabel}): Запрос поддержания 22°C`;
    if (d.startsWith("4100")) return `HVAC (${pfx}, ${busLabel}): Запрос кондиционирования (Cooling LO)`;
    return `HVAC (${pfx}, ${busLabel}): Управление целевым режимом климата`;
  }
  return `CAN (${pfx}, ${busLabel}): ${d}`;
}

function explainCanScript(script) {
  const frames = script.frames || [];
  if (!frames.length) return "Пустой скрипт";
  if (frames.some(f => Number(f.can_id) === 0x394 && (f.data || "").toLowerCase() === "fcff2fff201f5000")) return "Запрос климата 22°C, обдув в лицо и ноги, вентилятор максимум; команды обогрева";
  const allHex = frames.map(f => (f.data || "").toLowerCase().replace(/\s+/g, ""));
  const canIds = new Set(frames.map(f => typeof f.can_id === "string" ? (f.can_id.startsWith("0x") ? parseInt(f.can_id, 16) : parseInt(f.can_id, 10)) : (f.can_id || 0)));

  const hasPreheat = allHex.some(d => d === "fcff0fff001ff000" || (d.length >= 6 && d.slice(2, 6) === "800a"));
  const hasCooling = allHex.some(d => d.startsWith("4100") || (d.length >= 6 && d.slice(2, 6) === "440a"));
  const hasAuto22 = allHex.some(d => d.startsWith("7cff0fff") || (d.length >= 6 && d.slice(2, 6) === "580a"));
  const hasLock = allHex.some(d => d.startsWith("03220203aa21") || (d.length >= 12 && d.slice(8, 12) === "aa21"));
  const hasUnlock = allHex.some(d => d.startsWith("032202030000") || (d.length >= 12 && d.slice(8, 12) === "0002"));
  const hasHorn = allHex.some(d => d.startsWith("062f0103") || d.startsWith("04"));
  const hasTrunkOpen = allHex.some(d => d.startsWith("1000ffff") || (d.length >= 12 && d.slice(10, 12) === "05"));
  const hasTrunkClose = allHex.some(d => d.startsWith("0000ffff"));
  const hasReset = allHex.some((d, i) => Number(frames[i].can_id) === 0x011 && d.length >= 6 && d.slice(2, 6) === "0009");

  if (hasCooling) return "Включение охлаждения салона (HVAC + BCM)";
  if (hasAuto22) return "Команды климат-контроля 22°C (HVAC + BCM)";
  if (hasPreheat) return "Включение максимального прогрева салона (HVAC + BCM)";
  if (hasLock) return "Закрытие центрального замка / постановка на охрану (UDS + BCM)";
  if (hasUnlock) return "Открытие центрального замка / снятие с охраны (UDS + BCM)";
  if (hasHorn) return "Звуковой сигнал клаксона (UDS + BCM)";
  if (hasTrunkOpen) return "Открытие багажника (BCM + Tailgate)";
  if (hasTrunkClose) return "Закрытие багажника (Tailgate)";
  if (hasReset) return "Сброс триггера BCM; самостоятельный Remote Off не подтверждён";

  const names = [];
  if (canIds.has(0x701)) names.push("UDS Diag");
  if (canIds.has(0x011)) names.push("BCM");
  if (canIds.has(0x092)) names.push("VCU");
  if (canIds.has(0x093) || canIds.has(0x394)) names.push("HVAC");
  if (canIds.has(0x09a)) names.push("Tailgate");
  if (names.length) return "Команды в блоки: " + names.join(", ");
  return `Пользовательский скрипт (${frames.length} кадр.)`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
}

const commandNames = {
  lock: "Закрыть ЦЗ", unlock: "Открыть ЦЗ", cooling: "Охлаждение", preheat: "Прогрев",
  remote_on: "Remote On", remote_off: "Remote Off", horn: "Гудок",
  preheat_off: "Отключить прогрев", cooling_off: "Отключить охлаждение",
};

function validateScript(script) {
  if (!script || typeof script !== "object" || Array.isArray(script)) throw new Error("Нужен JSON-объект скрипта");
  if (script.format_version !== 2) throw new Error("Нужен format_version=2: перечитайте CAN-скрипт, старый формат небезопасен");
  const allowed = ["format_version", "name", "repeat", "frame_type", "chain_next", "frames"];
  if (Object.keys(script).some(k => !allowed.includes(k))) throw new Error("Неизвестное поле скрипта");
  if (typeof script.name !== "string" || script.name.length > 16 || /[\x00-\x1f]/.test(script.name)) throw new Error("name: строка до 16 символов Windows-1251");
  if (!Number.isInteger(script.repeat) || script.repeat < 0 || script.repeat > 255) throw new Error("repeat: целое число 0…255");
  if (![1, 2].includes(script.frame_type)) throw new Error("frame_type: 1 (11 бит) или 2 (29 бит)");
  if (typeof script.chain_next !== "boolean") throw new Error("chain_next: true или false");
  if (!Array.isArray(script.frames) || script.frames.length > 4) throw new Error("frames: список из 0–4 кадров");
  script.frames.forEach((f, i) => {
    if (!f || typeof f !== "object" || Array.isArray(f)) throw new Error(`Кадр ${i + 1}: нужен объект`);
    if (Object.keys(f).some(k => !["delay_ms", "can_id", "bus", "count", "dlc", "data"].includes(k))) throw new Error(`Кадр ${i + 1}: неизвестное поле`);
    for (const [key, min, max] of [["delay_ms", 0, 10230], ["can_id", 0, script.frame_type === 1 ? 0x7ff : 0x1fffffff], ["bus", 1, 3], ["count", 0, 255], ["dlc", 0, 8]]) {
      if (!Number.isInteger(f[key]) || f[key] < min || f[key] > max) throw new Error(`Кадр ${i + 1}, ${key}: целое число ${min}…${max}`);
    }
    if (f.delay_ms % 10 !== 0) throw new Error(`Кадр ${i + 1}: delay_ms с шагом 10 мс`);
    if (typeof f.data !== "string" || !/^[0-9a-fA-F]{16}$/.test(f.data)) throw new Error(`Кадр ${i + 1}: data должен содержать 16 HEX-символов`);
  });
}

function scriptDocument(s) {
  return {slot: s.slot, format_version: s.format_version, name: s.name, repeat: s.repeat,
    frame_type: s.frame_type, chain_next: s.chain_next,
    frames: (s.frames || []).map(f => ({delay_ms: f.delay_ms, can_id: f.can_id, bus: f.bus,
      count: f.count, dlc: f.dlc, data: f.data}))};
}

function validateCommandDocument(doc) {
  if (!doc || typeof doc !== "object" || Array.isArray(doc) ||
      Object.keys(doc).some(k => !["scenarios", "scripts"].includes(k)) ||
      !Array.isArray(doc.scenarios) || !Array.isArray(doc.scripts)) throw new Error("Нужны списки scenarios и scripts");
  const seen = new Set();
  const integer = (v, max, label) => {
    if (!Number.isInteger(v) || v < 0 || v > max) throw new Error(`${label}: целое число 0…${max}`);
  };
  for (const s of doc.scenarios) {
    if (!s || Object.keys(s).some(k => !["index", "conditions", "match", "actions"].includes(k))) throw new Error("Неизвестное поле сценария");
    integer(s.index, 24, "index");
    if (seen.has(`s${s.index}`)) throw new Error("Сценарий указан дважды");
    seen.add(`s${s.index}`);
    if (!Array.isArray(s.conditions) || s.conditions.length !== 2 || !["any", "all"].includes(s.match)) throw new Error("Нужны два conditions (или null) и match: any/all");
    for (const c of s.conditions) {
      if (c === null) continue;
      if (!c || Object.keys(c).some(k => !["sensor", "comparison", "value"].includes(k))) throw new Error("Неизвестное поле условия");
      integer(c.sensor, 65535, "sensor");
      if (!c.sensor || !["equal", "greater", "changed"].includes(c.comparison) ||
          typeof c.value !== "number" || !Number.isFinite(c.value) || Math.abs(c.value) > 3.4028234663852886e38) throw new Error("Проверьте sensor, comparison и числовой value");
    }
    const a = s.actions;
    const fields = ["external_output_on", "external_output_on_seconds", "external_output_off", "external_output_off_seconds", "can_script"];
    if (!a || Object.keys(a).some(k => !fields.includes(k)) || fields.some(k => !(k in a))) throw new Error("Нужны все поля actions");
    for (const key of fields.slice(0, 4)) integer(a[key], key.endsWith("seconds") ? 255 : 15, key);
    if (a.can_script !== null) integer(a.can_script, 7, "can_script");
  }
  for (const s of doc.scripts) {
    integer(s.slot, 7, "slot");
    if (seen.has(`c${s.slot}`)) throw new Error("CAN-слот указан дважды");
    seen.add(`c${s.slot}`);
    const {slot, ...spec} = s;
    validateScript(spec);
  }
}

function scenarioSummary(s) {
  const doc = s.document || s;
  const conditions = (doc.conditions || []).filter(Boolean).map(c =>
    c.comparison === "changed" ? `изменение ${c.sensor}` : `${c.sensor} ${c.comparison === "equal" ? "стал =" : "стал >"} ${c.value}`);
  const a = doc.actions || {};
  const actions = [];
  if (a.external_output_on) actions.push(`${1340 + a.external_output_on}=1${a.external_output_on_seconds ? ` на ${a.external_output_on_seconds} с` : ""}`);
  if (a.external_output_off) actions.push(`${1340 + a.external_output_off}=0${a.external_output_off_seconds ? ` на ${a.external_output_off_seconds} с` : ""}`);
  if (Number.isInteger(a.can_script)) actions.push(`CAN-слот ${a.can_script}`);
  if (s.has_other_actions) actions.push("другие действия сохранены");
  return `${conditions.join(doc.match === "all" ? " И " : " ИЛИ ") || "условия отключены"} → ${actions.join("; ") || "нет действий выходов/CAN"}`;
}

class VegaCanEditor extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("vega-can-editor: 'entity' is required");
    }
    this._config = config;
    this._rendered = false;
    this._activeSlot = null;
    this._saving = false;
    this._reading = false;
    this._sig = null;
    this._view = "commands";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) this._build();
    this._update();
  }

  getCardSize() {
    return 6;
  }

  _slots() {
    const st = this._hass && this._hass.states[this._config.entity];
    return (st && st.attributes && st.attributes.slots) || [];
  }

  _attributes() {
    return this._hass.states[this._config.entity]?.attributes || {};
  }

  _scenarios() {
    return this._attributes().scenarios || [];
  }

  _bindingText(slot) {
    const attrs = this._attributes();
    if (!attrs.scenarios_read_at) return "Привязки к командам ещё не прочитаны";
    const routes = Object.entries(attrs.command_bindings || {})
      .filter(([, b]) => (b.slots || []).includes(slot))
      .map(([action, b]) => `${commandNames[action] || action}${b.status === "conditional" ? " (с дополнительными условиями)" : b.status === "multiple" ? " (несколько слотов)" : ""}`);
    return routes.length ? `По сценариям: ${routes.join(", ")}` : "Связь с кнопками не найдена в прочитанных сценариях";
  }

  async _call(service, data = {}) {
    const entryId = data.entry_id || this._attributes().entry_id;
    if (!entryId) throw new Error("Обновите интеграцию Vega и перечитайте слоты");
    const result = await this._hass.callWS({type: "call_service", domain: "vega", service,
      service_data: {...data, entry_id: entryId}, return_response: true});
    return result.response;
  }

  async _read() {
    if (this._reading || this._saving) return;
    this._reading = true;
    const button = this.querySelector("#read");
    button.disabled = true;
    button.textContent = "Читаю команды и сценарии…";
    try {
      const response = await this._call("read_commands");
      this._toast(response.scenarios_read ? "Команды, сценарии и CAN-слоты прочитаны" : "CAN-слоты прочитаны; сценарии команд недоступны");
    } catch (e) {
      this._toast(`Ошибка чтения: ${e.message || e}`);
    } finally {
      this._reading = false;
      button.disabled = false;
      button.textContent = "Прочитать из трекера";
    }
  }

  _build() {
    this.innerHTML = `
      <ha-card header="Команды Vega">
        <style>
          .wrap { padding: 8px 16px 16px; }
          .bar { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 16px; flex-wrap:wrap; }
          .slot-table { width:100%; border-collapse:collapse; margin-top:6px; }
          .slot-row { border-top:1px solid var(--divider-color, rgba(255,255,255,0.1)); cursor:pointer; transition:background 0.15s; }
          .slot-row:hover { background:var(--table-row-alternative-background-color, rgba(255,255,255,0.04)); }
          .slot-cell { padding:10px 8px; vertical-align:middle; }
          .slot-badge { display:inline-block; font-size:11px; font-weight:600; padding:2px 8px; border-radius:12px;
                        background:var(--secondary-background-color, rgba(255,255,255,0.1));
                        color:var(--primary-text-color, #fff); }
          .slot-title { font-weight:600; font-size:14px; margin-bottom:2px; }
          .slot-summary { font-size:12px; color:var(--secondary-text-color, #888); }
          .btn { cursor:pointer; border:none; border-radius:8px; padding:7px 12px; font-weight:600;
                 background:var(--primary-color, #03a9f4); color:#fff; font-size:13px; }
          .btn.sec { background:var(--secondary-background-color, rgba(255,255,255,0.1));
                     color:var(--primary-text-color, #fff); }
          .btn.sec:hover { background:var(--divider-color, rgba(255,255,255,0.2)); }
          .btn:disabled { opacity:0.5; cursor:default; }
          .error { color:var(--error-color, #db4437); white-space:pre-wrap; }
          .hint { font-size:12px; color:var(--secondary-text-color, #888); }
          .command-row { padding:12px 0; border-top:1px solid var(--divider-color); display:flex; gap:12px; align-items:center; }
          .command-row > div { flex:1; min-width:0; }
          .command-row .hint { margin-top:4px; overflow-wrap:anywhere; }
          .tabs { display:flex; gap:6px; padding:4px 16px 12px; flex-wrap:wrap; }
          .tabs [aria-selected="true"] { background:var(--primary-color); color:white; }
          .preview { padding:10px; background:var(--secondary-background-color); border-radius:8px; font-size:13px; }

          /* Modal Window Styles */
          .modal-backdrop {
            position: fixed; inset: 0; background: rgba(0, 0, 0, 0.65);
            backdrop-filter: blur(3px); z-index: 99999; display: flex;
            align-items: center; justify-content: center; padding: 16px;
            box-sizing: border-box;
          }
          .modal-box {
            background: var(--ha-card-background, var(--card-background-color, #1c1c1e));
            color: var(--primary-text-color, #fff); border-radius: 14px;
            width: 100%; max-width: 660px; max-height: 88vh; display: flex;
            flex-direction: column; box-shadow: 0 16px 36px rgba(0,0,0,0.5);
            border: 1px solid var(--divider-color, rgba(255,255,255,0.12));
            overflow: hidden; animation: popIn 0.2s ease-out;
          }
          @keyframes popIn {
            from { transform: scale(0.95); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
          }
          .modal-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 14px 18px; border-bottom: 1px solid var(--divider-color, rgba(255,255,255,0.1));
          }
          .modal-title { font-size: 17px; font-weight: 600; }
          .modal-close {
            background: transparent; border: none; font-size: 22px; cursor: pointer;
            color: var(--secondary-text-color, #aaa); line-height: 1; padding: 0 6px;
          }
          .modal-close:hover { color: var(--primary-text-color, #fff); }
          .modal-body {
            padding: 16px 18px; overflow-y: auto; flex: 1; display: flex;
            flex-direction: column; gap: 14px;
          }
          .section-title { font-size: 13px; font-weight: 600; text-transform: uppercase;
                           letter-spacing: 0.5px; color: var(--secondary-text-color, #888); }
          .summary-box {
            background: var(--secondary-background-color, rgba(255,255,255,0.05));
            border-left: 4px solid var(--primary-color, #03a9f4);
            padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 13px; font-weight: 500;
          }
          .frames-list { display: flex; flex-direction: column; gap: 8px; }
          .frame-card {
            background: var(--secondary-background-color, rgba(255,255,255,0.04));
            border: 1px solid var(--divider-color, rgba(255,255,255,0.08));
            border-radius: 8px; padding: 10px 12px;
          }
          .frame-header {
            display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 6px;
          }
          .tag {
            font-size: 11px; padding: 2px 6px; border-radius: 4px; font-family: monospace;
            background: var(--divider-color, rgba(255,255,255,0.12));
          }
          .frame-action {
            font-size: 13px; font-weight: 600; color: var(--primary-color, #03a9f4);
            margin-bottom: 3px;
          }
          .frame-hex { font-family: monospace; font-size: 11px; color: var(--secondary-text-color, #888); }
          .json-area {
            width: 100%; box-sizing: border-box; min-height: 280px; flex-shrink: 0; font-family: monospace;
            font-size: 12px; background: var(--card-background-color, #111);
            color: var(--primary-text-color, #eee); border: 1px solid var(--divider-color, rgba(255,255,255,0.15));
            border-radius: 8px; padding: 8px; resize: vertical;
          }
          .modal-footer {
            display: flex; justify-content: flex-end; gap: 10px; padding: 12px 18px;
            border-top: 1px solid var(--divider-color, rgba(255,255,255,0.1));
            background: var(--secondary-background-color, rgba(255,255,255,0.02));
          }
        </style>
        <div class="bar">
          <button class="btn sec" id="read">Прочитать из трекера</button>
          <span class="hint">Запись требует включённого Direct Control.</span>
        </div>
        <div class="tabs" role="tablist">
          <button class="btn sec" id="tab-commands" role="tab">Команды</button>
          <button class="btn sec" id="tab-scenarios" role="tab">Все сценарии</button>
          <button class="btn sec" id="tab-slots" role="tab">CAN-слоты</button>
        </div>
        <div class="wrap" id="wrap"></div>
        <div id="modal-container"></div>
      </ha-card>
    `;
    this.querySelector("#read").addEventListener("click", () => this._read());
    for (const view of ["commands", "scenarios", "slots"]) {
      this.querySelector(`#tab-${view}`).addEventListener("click", () => { this._view = view; this._update(); });
    }
    this._wrap = this.querySelector("#wrap");
    this._modalContainer = this.querySelector("#modal-container");
    this._rendered = true;
  }

  _update() {
    if (!this._rendered) return;
    const slots = this._slots();
    const sig = JSON.stringify([this._view, slots, this._scenarios(), this._attributes().command_bindings, this._attributes().scenarios_read_at]);
    if (sig === this._sig) return;
    this._sig = sig;
    for (const view of ["commands", "scenarios", "slots"]) this.querySelector(`#tab-${view}`).setAttribute("aria-selected", String(this._view === view));

    this._wrap.innerHTML = "";
    if (this._view !== "slots") {
      this._renderCommands();
      return;
    }
    if (!slots.length) {
      this._wrap.innerHTML = `<div class="hint" style="padding:12px 0;">Нет данных. Нажмите «Прочитать из трекера».</div>`;
      return;
    }

    const table = document.createElement("table");
    table.className = "slot-table";
    for (const s of slots) {
      const tr = document.createElement("tr");
      tr.className = "slot-row";
      const frameCount = (s.frames || []).length;
      const summaryText = explainCanScript(s);
      tr.innerHTML = `
        <td class="slot-cell" style="width:70px;">
          <span class="slot-badge">Слот ${s.slot}</span>
        </td>
        <td class="slot-cell">
          <div class="slot-title">${escapeHtml(s.name || "(без имени)")}</div>
          <div class="slot-summary">${escapeHtml(summaryText)}</div>
          <div class="hint">${escapeHtml(this._bindingText(s.slot))}</div>
        </td>
        <td class="slot-cell" style="text-align:right; width:130px;">
          <button class="btn sec open-btn">Просмотр</button>
        </td>
      `;
      tr.querySelector(".open-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        this._openModal(s);
      });
      tr.addEventListener("click", () => this._openModal(s));
      table.appendChild(tr);
    }
    this._wrap.appendChild(table);

    // The open editor owns its draft and original raw bytes. Telemetry and reads
    // must not replace either; the server detects changes before saving.
  }

  _renderCommands() {
    const fresh = !!this._attributes().scenarios_read_at;
    const scenarios = this._scenarios();
    if (!fresh) this._wrap.innerHTML = '<p class="hint">Прочитайте настройки из трекера для просмотра и редактирования команд.</p>';
    const append = (title, detail, indices, action = null) => {
      const row = document.createElement("div");
      row.className = "command-row";
      row.innerHTML = `<div><div class="slot-title">${escapeHtml(title)}</div><div class="hint">${escapeHtml(detail)}</div></div><button class="btn sec" ${fresh ? "" : "disabled"}>Редактировать</button>`;
      row.querySelector("button").addEventListener("click", () => this._openCommandEditor(indices, title, action));
      this._wrap.appendChild(row);
    };
    if (this._view === "scenarios") {
      for (const s of scenarios) append(`Сценарий ${s.index}`, scenarioSummary(s), [s.index]);
      return;
    }
    const bindings = this._attributes().command_bindings || {};
    for (const [action, title] of Object.entries(commandNames)) {
      const b = bindings[action];
      if (!b) continue;
      const indices = (b.scenario_routes || []).map(r => r.scenario);
      const details = indices.map(index => {
        const s = scenarios.find(s => s.index === index);
        return s ? `Сценарий ${index}: ${scenarioSummary(s)}` : "";
      });
      append(title, `Команда HA: ${b.setting_id}=${b.value}. ${details.join(" · ") || (fresh ? "Связанных сценариев нет" : "Сценарии ещё не прочитаны")}`, indices, action);
    }
  }

  _openCommandEditor(indices, title, action = null) {
    if (this._saving || !this._attributes().scenarios_read_at) return;
    const scenarios = indices.map(i => this._scenarios().find(s => s.index === i)).filter(Boolean);
    const slots = [...new Set(scenarios.map(s => s.slot).filter(s => s !== null))];
    const scripts = slots.map(i => this._slots().find(s => s.slot === i)).filter(Boolean);
    this._bundleExpected = {};
    for (const s of this._scenarios()) this._bundleExpected[`4001/${s.index}`] = s.raw;
    for (const s of this._slots()) this._bundleExpected[`5502/${s.slot}`] = s.raw;
    this._editorEntryId = this._attributes().entry_id;
    this._bundleOriginalIndices = indices.slice();
    this._editorBinding = action ? {...this._attributes().command_bindings[action]} : null;
    this._editable = scenarios.every(s => s.editable) && scripts.every(s => s.editable && s.format_version === 2) && slots.length === scripts.length;
    this._activeSlot = null;
    this._activeCommand = action || `scenario:${indices.join(",")}`;
    this._bundleContext = JSON.parse(JSON.stringify(this._scenarios()));
    const doc = {scenarios: scenarios.map(s => ({index: s.index, ...s.document})), scripts: scripts.map(scriptDocument)};
    this._modalContainer.innerHTML = `
      <div class="modal-backdrop" id="backdrop"><div class="modal-box" role="dialog" aria-label="Редактор команды">
        <div class="modal-header"><span class="modal-title">${escapeHtml(title)}</span><button class="modal-close" id="close-x">&times;</button></div>
        <div class="modal-body">
          <div class="preview" id="command-preview"></div>
          <div class="hint">Сценарии задают условия и действия с выходами; scripts содержит связанные CAN-кадры. Порядок действий сценария не задаётся порядком полей JSON.</div>
          <details><summary>Поля редактора</summary><div class="hint">conditions: два условия или null. comparison: equal — стал равен, greater — стал больше, changed — любое изменение. match: any — ИЛИ, all — И. external_output_on/off: номер внешнего выхода 1–15 (ID = 1340 + номер), 0 — действие не выбрано. *_seconds: время 0–255 с, как в конфигураторе. can_script: слот 0–7 или null. Не удаляйте сценарий из списка: для отключения задайте оба условия null.</div></details>
          <button class="btn sec" id="add-scenario" ${action ? "" : "hidden"}>Добавить сценарий для этой команды</button>
          <textarea class="json-area" id="slot-json" aria-label="JSON команды"></textarea>
          <div class="error" id="editor-error" role="alert"></div>
          <div class="hint">Запись нескольких настроек проверяется по каждой. При ошибке оставшиеся записи останавливаются; результат показан в сообщении.</div>
        </div>
        <div class="modal-footer"><button class="btn sec" id="close-btn">Закрыть</button><button class="btn" id="save-btn" ${this._editable ? "" : "disabled"}>Сохранить команду и проверить</button></div>
      </div></div>`;
    const textarea = this.querySelector("#slot-json");
    textarea.value = JSON.stringify(doc, null, 2);
    const preview = () => {
      try {
        const current = JSON.parse(textarea.value);
        validateCommandDocument(current);
        this._renderCommandPreview(current);
        this.querySelector("#editor-error").textContent = this._editable ? "" : "Есть неподдерживаемый формат. Запись недоступна.";
      } catch (e) { this.querySelector("#editor-error").textContent = e.message; }
    };
    textarea.addEventListener("input", preview);
    preview();
    const close = () => { if (!this._saving) { this._activeCommand = null; this._modalContainer.innerHTML = ""; } };
    this.querySelector("#close-btn").addEventListener("click", close);
    this.querySelector("#close-x").addEventListener("click", close);
    this.querySelector("#backdrop").addEventListener("click", e => { if (e.target.id === "backdrop") close(); });
    this.querySelector("#save-btn").addEventListener("click", () => this._saveCommand(textarea.value));
    this.querySelector("#add-scenario").addEventListener("click", () => {
      if (this._saving || !action) return;
      try {
        const current = JSON.parse(textarea.value);
        validateCommandDocument(current);
        const free = this._bundleContext.find(s => !s.conditions.length && !s.has_other_actions &&
          !s.document.actions.external_output_on && !s.document.actions.external_output_off && s.slot === null &&
          !current.scenarios.some(c => c.index === s.index));
        if (!free) throw new Error("Нет свободного сценария");
        const b = this._editorBinding;
        current.scenarios.push({index: free.index, ...JSON.parse(JSON.stringify(free.document)),
          conditions: [{sensor: b.setting_id, comparison: "equal", value: b.value}, null]});
        textarea.value = JSON.stringify(current, null, 2);
        preview();
      } catch (e) { this.querySelector("#editor-error").textContent = e.message; }
    });
  }

  _renderCommandPreview(doc) {
    const lines = [];
    if (this._editorBinding) {
      const b = this._editorBinding;
      lines.push(this._activeCommand === "remote_on"
        ? "Кнопка HA: 1341=0 → пауза 1 с → 1341=1 (задано в интеграции)."
        : `Кнопка HA: ${b.setting_id}=${b.value}.`);
    }
    if (!doc.scenarios.length) lines.push("Связанных сценариев нет. Можно добавить новый сценарий для этой команды.");
    lines.push(...doc.scenarios.map(s => `Сценарий ${s.index}: ${scenarioSummary(s)}`));
    for (const s of doc.scenarios) {
      if (this._bundleContext.find(old => old.index === s.index)?.has_other_actions)
        lines.push(`Сценарий ${s.index}: дополнительные действия (SMS, физические выходы и др.) сохраняются без изменений.`);
    }
    for (const s of doc.scripts) {
      lines.push(`CAN-слот ${s.slot} «${s.name}»: ${s.frames.length} кадров, повтор ${s.repeat}, следующий слот: ${s.chain_next ? "да" : "нет"}.`);
      const others = this._bundleContext.filter(c => c.slot === s.slot && !doc.scenarios.some(d => d.index === c.index));
      if (others.length) lines.push(`Этот CAN-слот также используется сценариями: ${others.map(s => s.index).join(", ")}. Изменение кадров повлияет и на них.`);
    }
    this.querySelector("#command-preview").textContent = lines.join("\n") || "Связанных сценариев нет. Можно добавить новый сценарий для этой команды.";
    this.querySelector("#command-preview").style.whiteSpace = "pre-wrap";
  }

  async _saveCommand(text) {
    if (this._saving || !this._editable) return;
    let doc;
    try {
      doc = JSON.parse(text);
      validateCommandDocument(doc);
      if (this._bundleOriginalIndices.some(i => !doc.scenarios.some(s => s.index === i))) throw new Error("Для отключения сценария задайте conditions: [null, null], не удаляя его из документа");
      if (!doc.scenarios.length && !doc.scripts.length) throw new Error("Добавьте сценарий для редактирования");
    } catch (e) { this.querySelector("#editor-error").textContent = e.message; return; }
    const textarea = this.querySelector("#slot-json"), button = this.querySelector("#save-btn");
    this._saving = true;
    textarea.disabled = button.disabled = true;
    button.textContent = "Сохраняю и проверяю…";
    this.querySelector("#editor-error").textContent = "";
    try {
      const response = await this._call("write_commands", {document: doc, expected: this._bundleExpected, entry_id: this._editorEntryId});
      if (response?.verified !== true) throw new Error("Подтверждение сохранения не получено. Перечитайте настройки");
      this._toast(response.unchanged ? "Изменений нет, настройки проверены" : `Команда сохранена. Проверено записей: ${response.written.length}`);
      this._activeCommand = null;
      this._modalContainer.innerHTML = "";
    } catch (e) { this.querySelector("#editor-error").textContent = `Ошибка сохранения: ${e.message || e}`; }
    finally {
      this._saving = false;
      textarea.disabled = false;
      button.disabled = !this._editable;
      button.textContent = "Сохранить команду и проверить";
    }
  }

  _openModal(slotData) {
    if (this._saving) return;
    this._activeSlot = slotData.slot;
    this._baselineRaw = slotData.raw;
    this._editorEntryId = this._attributes().entry_id;
    this._editable = slotData.editable === true && slotData.format_version === 2 && /^[0-9a-f]{164}$/i.test(slotData.raw || "");
    this._modalContainer.innerHTML = `
      <div class="modal-backdrop" id="backdrop">
        <div class="modal-box" id="box">
          <div class="modal-header">
            <span class="modal-title">Слот ${slotData.slot}: ${escapeHtml(slotData.name || "(без имени)")}</span>
            <button class="modal-close" id="close-x">&times;</button>
          </div>
          <div class="modal-body" id="modal-body"></div>
          <div class="modal-footer">
            <button class="btn sec" id="close-btn">Закрыть</button>
            <button class="btn" id="save-btn" ${this._editable ? "" : "disabled"}>Записать и проверить</button>
          </div>
        </div>
      </div>
    `;

    const close = () => {
      if (this._saving) return;
      this._activeSlot = null;
      this._modalContainer.innerHTML = "";
    };

    this.querySelector("#close-x").addEventListener("click", close);
    this.querySelector("#close-btn").addEventListener("click", close);
    this.querySelector("#backdrop").addEventListener("click", (e) => {
      if (e.target.id === "backdrop") close();
    });

    this._renderModalContent(slotData);

    const ta = this.querySelector("#slot-json");
    if (ta) {
      ta.addEventListener("input", () => {
        try {
          const parsed = JSON.parse(ta.value);
          validateScript(parsed);
          this._renderDecodedSections(parsed);
          this.querySelector("#editor-error").textContent = "";
        } catch (e) {
          this.querySelector("#editor-error").textContent = e.message;
        }
      });
    }

    this.querySelector("#save-btn").addEventListener("click", () => {
      const taCurrent = this.querySelector("#slot-json");
      if (taCurrent) this._save(slotData.slot, taCurrent.value);
    });
  }

  _renderDecodedSections(s) {
    const summaryEl = this.querySelector("#summary-target");
    const framesEl = this.querySelector("#frames-target");
    if (!summaryEl || !framesEl) return;

    summaryEl.textContent = `🎯 ${s.summary || explainCanScript(s)}`;

    const frames = s.frames || [];
    if (!frames.length) {
      framesEl.innerHTML = `<div class="hint">В этом слоте нет CAN-кадров.</div>`;
      return;
    }
    framesEl.innerHTML = frames
      .map((f, i) => {
        const busNum = f.bus || 0;
        const busName = `CAN${busNum}`;
        const canIdNum = typeof f.can_id === "string" ? (f.can_id.startsWith("0x") ? parseInt(f.can_id, 16) : parseInt(f.can_id, 10)) : (f.can_id || 0);
        const canIdHex = `0x${canIdNum.toString(16).toUpperCase()}`;
        const actionDesc = f.description || explainCanFrame(busNum, canIdNum, f.data);
        return `
          <div class="frame-card">
            <div class="frame-header">
              <span class="tag">Шаг ${i + 1}</span>
              <span class="tag">${canIdHex}</span>
              <span class="tag">${busName}</span>
              <span class="tag">${f.delay_ms || 0} мс</span>
              <span class="tag">Отправок: ${f.count ?? "?"}</span>
              <span class="tag">DLC: ${f.dlc ?? "?"}</span>
            </div>
            <div class="frame-action">📌 ${escapeHtml(actionDesc)}</div>
            <div class="frame-hex">DATA: ${escapeHtml(f.data || "0000000000000000")}</div>
          </div>
        `;
      })
      .join("");
  }

  _renderModalContent(s) {
    const body = this.querySelector("#modal-body");
    if (!body) return;

    const {slot, ...jsonSpec} = scriptDocument(s);

    body.innerHTML = `
      <div class="hint">${escapeHtml(this._bindingText(s.slot))}</div>
      <div class="section-title">Расшифровка кадров</div>
      <div class="summary-box" id="summary-target"></div>

      <div class="section-title" style="margin-top:6px;">Последовательность кадров</div>
      <div class="frames-list" id="frames-target"></div>

      <div class="section-title" style="margin-top:8px;">Редактор (JSON)</div>
      <div class="hint">До 4 кадров. bus — CAN1–CAN3; delay_ms — 0–10230 мс, шаг 10; count — число отправок; dlc — 0–8. data хранит 8 байт (16 HEX-символов), передаётся dlc байт. repeat — повтор всей программы; chain_next — переход к следующему слоту. Запись проверяется чтением и не запускает скрипт.</div>
      <div class="hint">${this._editable ? "" : "Перечитайте слоты. Если запись остаётся недоступной, формат этого слота пока не поддерживается редактором."}</div>
      <textarea class="json-area" id="slot-json"></textarea>
      <div class="error" id="editor-error" role="alert"></div>
    `;

    this.querySelector("#slot-json").value = JSON.stringify(jsonSpec, null, 2);
    this._renderDecodedSections(s);
  }

  async _save(slot, text) {
    if (this._saving || !this._editable) return;
    let script;
    try {
      script = JSON.parse(text);
      validateScript(script);
    } catch (e) {
      this.querySelector("#editor-error").textContent = e.message;
      return;
    }
    this._saving = true;
    const button = this.querySelector("#save-btn");
    const textarea = this.querySelector("#slot-json");
    textarea.disabled = true;
    button.disabled = true;
    button.textContent = "Записываю и проверяю…";
    this.querySelector("#editor-error").textContent = "";
    try {
      const response = await this._call("write_can_script", {slot, script,
        expected_raw: this._baselineRaw, entry_id: this._editorEntryId});
      if (response?.verified !== true) throw new Error("Подтверждение записи не получено. Перечитайте слот");
      this._toast(`Слот ${slot}: ${response.unchanged ? "изменений нет, данные проверены" : "записано, проверено чтением"}`);
      this._activeSlot = null;
      this._modalContainer.innerHTML = "";
    } catch (e) {
      this.querySelector("#editor-error").textContent = `Ошибка записи: ${e.message || e}`;
    } finally {
      this._saving = false;
      textarea.disabled = false;
      button.disabled = !this._editable;
      button.textContent = "Записать и проверить";
    }
  }

  _toast(message) {
    this.dispatchEvent(
      new CustomEvent("hass-notification", { bubbles: true, composed: true, detail: { message } })
    );
  }
}

if (!customElements.get("vega-can-editor")) customElements.define("vega-can-editor", VegaCanEditor);
window.customCards = window.customCards || [];
if (!window.customCards.some(card => card.type === "vega-can-editor")) window.customCards.push({
  type: "vega-can-editor",
  name: "Vega Commands Editor",
  description: "Условия сценариев, действия внешних выходов и связанные CAN-скрипты Vega",
});
console.info("%c VEGA-CAN-EDITOR v0.3.4 %c loaded ", "background:#00c0fc;color:#fff", "");
