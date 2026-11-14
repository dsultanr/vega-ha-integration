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
  const climate = climateSettings(script);
  if (climate) {
    const parts = [climate.temperature === 17 ? 'LO' : climate.temperature === 32 ? 'HI' : `${climate.temperature} °C`];
    if (climate.fan !== null) parts.push(`вентилятор ${climate.fan}`);
    if (climate.airflow !== null) parts.push(climateAirflows[climate.airflow].toLowerCase());
    return `Климат: ${parts.join(', ')}`;
  }
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

// F515 OEM command fields, captured with all heat levels, fan speeds and four
// airflow modes. This creates a draft; only the existing guarded save writes it.
const climateFields = [
  ['temperature', 'Температура'], ['fan', 'Вентилятор'], ['airflow', 'Обдув'],
  ['driver', 'Сиденье водителя'], ['passenger', 'Переднее пассажирское сиденье'],
  ['rear_left', 'Заднее левое сиденье'], ['rear_right', 'Заднее правое сиденье'],
  ['wheel', 'Обогрев руля'],
];
const climateAirflows = {1: 'В лицо', 2: 'В лицо и ноги', 3: 'В ноги', 4: 'В ноги и на лобовое стекло'};
function climateBytes(frame) { return frame.data.match(/../g).map(v => parseInt(v, 16)); }
function climateHex(bytes) { return bytes.map(v => v.toString(16).padStart(2, '0')).join(''); }
function climateHeat(code) { return code === 0 ? null : code === 4 ? 0 : code <= 3 ? code : undefined; }
function climateSettings(script) {
  try { const {slot, ...spec} = scriptDocument(script); validateScript(spec); } catch (_) { return null; }
  if (script.frame_type !== 1 || script.chain_next || !script.frames.some(f => f.bus === 1 && f.can_id === 0x394 && f.count > 0) ||
      !script.frames.some(f => f.bus === 2 && f.can_id === 0x11 && climateBytes(f)[2] === 0x0a && f.count > 0)) return null;
  if ([0x394, 0x92, 0x93].some(id => script.frames.filter(f => f.can_id === id).length > 1) ||
      script.frames.filter(f => f.can_id === 0x11 && climateBytes(f)[2] === 0x0a).length > 1) return null;
  const result = Object.fromEntries(climateFields.map(([key]) => [key, null]));
  for (const f of script.frames) {
    const b = climateBytes(f);
    // Restrict this form to known climate programs; other commands retain JSON editing.
    if (f.dlc !== 8 || !((f.bus === 1 && [0x394, 0x92, 0x93].includes(f.can_id)) ||
                        (f.bus === 2 && f.can_id === 0x11))) return null;
    if (f.can_id === 0x11 && (b[0] !== 0 || b.slice(4).some(Boolean) ||
        !((b[2] === 0x0a && [0x44, 0x58, 0x80].includes(b[1])) ||
          (f.data.toLowerCase() === '0000090000000000') || f.data.toLowerCase() === '0000005b00000000'))) return null;
    if (f.can_id === 0x394) {
      const cooling = b[0] === 0x41 && b[1] === 0;
      if (!(cooling || ([0xfc, 0x7c].includes(b[0]) && b[1] === 0xff)) ||
          (b[2] & 15) !== (cooling ? 0 : 15) || b[3] !== (cooling ? 0 : 0xff) ||
          b[5] !== (cooling ? 0 : 0x1f) || (b[4] & 0xc3) || (b[6] & 7) || b[7]) return null;
      if (f.count === 0) continue;
      const temp = b[6] >> 3, fan = (b[4] >> 2) & 15, airflow = b[2] >> 4;
      if (temp !== 31) result.temperature = 17 + temp / 2;
      if (fan) result.fan = fan;
      if (airflow) result.airflow = airflow;
    } else if (f.can_id === 0x92) {
      if ((b[0] & 3) || b[1] || (b[2] & 31) || b.slice(3).some(Boolean)) return null;
      for (const [key, code] of [['rear_left', b[0] >> 5], ['rear_right', (b[0] >> 2) & 7], ['wheel', b[2] >> 5]]) {
        const level = climateHeat(code); if (level === undefined) return null;
        if (f.count > 0 && level !== null) result[key] = level;
      }
    } else if (f.can_id === 0x93) {
      if (f.data.slice(2).toLowerCase() !== 'c3ccc0000c0000' || (b[0] & 0xc0)) return null;
      for (const [key, code] of [['driver', b[0] & 7], ['passenger', (b[0] >> 3) & 7]]) {
        const level = climateHeat(code); if (level === undefined) return null;
        if (f.count > 0 && level !== null) result[key] = level;
      }
    } else if (b[2] !== 9) {
      for (const [key, code] of [['driver', b[3] & 7], ['passenger', (b[3] >> 3) & 7]]) {
        if (code > 4) return null;
        if (f.count > 0 && code) result[key] = code - 1;
      }
      // The remote preset is used only when no explicit HVAC temperature exists.
      if (f.count > 0 && result.temperature === null && b[2] === 0x0a) result.temperature = {68:17, 88:22, 128:32}[b[1]];
    }
  }
  if (result.temperature === null || (result.fan !== null && result.fan > 8) ||
      (result.airflow !== null && !climateAirflows[result.airflow])) return null;
  return result;
}
function compileClimateProgram(script, settings) {
  if (!climateSettings(script)) throw new Error('CAN-программа не поддерживает климатическую форму');
  if (!settings || Object.keys(settings).length !== climateFields.length ||
      Object.keys(settings).some(k => !climateFields.some(([key]) => key === k))) throw new Error('Нужны все климатические настройки');
  const temp = settings.temperature;
  if (!Number.isFinite(temp) || temp < 17 || temp > 32 || !Number.isInteger(temp * 2)) throw new Error('Температура: LO, 17.5–31.5 °C с шагом 0.5 или HI');
  for (const [key] of climateFields.slice(1)) {
    const value = settings[key], max = key === 'fan' ? 8 : key === 'airflow' ? 4 : 3;
    const min = ['fan', 'airflow'].includes(key) ? 1 : 0;
    if (value !== null && (!Number.isInteger(value) || value < min || value > max)) throw new Error(`Недопустимое значение ${key}`);
  }
  const heat = value => value === null ? 0 : value === 0 ? 4 : value;
  const front = value => value === null ? 0 : value + 1;
  const remote = climateBytes(script.frames.find(f => f.can_id === 0x11 && climateBytes(f)[2] === 0x0a));
  const retained = script.frames.filter(f => f.can_id === 0x11).reduce((bits, f) => bits | (climateBytes(f)[3] & 0xc0), 0);
  remote[3] = retained | front(settings.driver) | (front(settings.passenger) << 3);
  const hvac = climateBytes(script.frames.filter(f => f.can_id === 0x394).at(-1));
  hvac[2] = (hvac[2] & 15) | ((settings.airflow || 0) << 4);
  hvac[4] = (hvac[4] & 0xc3) | ((settings.fan || 0) << 2);
  hvac[6] = (hvac[6] & 7) | (((temp - 17) * 2) << 3);
  const rear = [(heat(settings.rear_left) << 5) | (heat(settings.rear_right) << 2), 0,
    heat(settings.wheel) << 5, 0, 0, 0, 0, 0];
  const frame = (delay_ms, bus, can_id, count, data) => ({delay_ms, bus, can_id, count, dlc: 8, data: climateHex(data)});
  // Wake/preset first, final HVAC settings afterwards. Four records, one program.
  const result = {slot: script.slot, format_version: 2, name: script.name,
    repeat: script.repeat, frame_type: 1, chain_next: false, frames: [
      frame(5000, 2, 0x11, 1, remote), frame(50, 2, 0x11, 1, [0,0,9,0,0,0,0,0]),
      frame(1000, 1, 0x394, 2, hvac), frame(150, 1, 0x92, 3, rear)]};
  const {slot, ...spec} = result; validateScript(spec);
  return result;
}
function climateOptions(key) {
  if (key === 'temperature') return Array.from({length:31}, (_, i) => {
    const v = 17 + i / 2; return [v, i === 0 ? 'LO — минимум' : i === 30 ? 'HI — максимум' : `${v} °C`];
  });
  if (key === 'fan') return [['keep','Не менять'], ...Array.from({length:8}, (_, i) => [i+1, `${i+1}${i===7 ? ' — максимум' : ''}`])];
  if (key === 'airflow') return [['keep','Не менять'], ...Object.entries(climateAirflows)];
  return [['keep','Не менять'], [0,'Выключить'], [1,'1 — слабый'], [2,'2 — средний'], [3,'3 — максимум']];
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
          <div id="climate-form"></div>
          <div class="preview" id="command-preview"></div>
          <details id="command-advanced"><summary>Условия и CAN-кадры (JSON)</summary>
          <div class="hint">Сценарии задают условия и действия с выходами; scripts содержит связанные CAN-кадры. Порядок действий сценария не задаётся порядком полей JSON.</div>
          <details><summary>Поля редактора</summary><div class="hint">conditions: два условия или null. comparison: equal — стал равен, greater — стал больше, changed — любое изменение. match: any — ИЛИ, all — И. external_output_on/off: номер внешнего выхода 1–15 (ID = 1340 + номер), 0 — действие не выбрано. *_seconds: время 0–255 с, как в конфигураторе. can_script: слот 0–7 или null. Не удаляйте сценарий из списка: для отключения задайте оба условия null.</div></details>
          <button class="btn sec" id="add-scenario" ${action ? "" : "hidden"}>Добавить сценарий для этой команды</button>
          <textarea class="json-area" id="slot-json" aria-label="JSON команды"></textarea></details>
          <div class="error" id="editor-error" role="alert"></div>
          <div class="hint">Запись нескольких настроек проверяется по каждой. При ошибке оставшиеся записи останавливаются; результат показан в сообщении.</div>
        </div>
        <div class="modal-footer"><button class="btn sec" id="close-btn">Закрыть</button><button class="btn" id="save-btn" ${this._editable ? "" : "disabled"}>Сохранить команду и проверить</button></div>
      </div></div>`;
    const textarea = this.querySelector("#slot-json");
    textarea.value = JSON.stringify(doc, null, 2);
    this._climateSlot = null;
    this._renderClimateForm(doc, true);
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

  _renderClimateForm(doc, bundle) {
    this._climateDirty = false;
    const root = this.querySelector('#climate-form');
    if (!root) return;
    const scripts = (bundle ? doc.scripts : [{...doc, slot: this._activeSlot}]).filter(s => climateSettings(s));
    const advanced = this.querySelector(bundle ? '#command-advanced' : '#slot-advanced');
    if (advanced) advanced.open = scripts.length === 0;
    if (!scripts.length) { root.innerHTML = ''; this._climateSlot = null; return; }
    const script = scripts.find(s => s.slot === this._climateSlot) || scripts[0];
    this._climateSlot = script.slot;
    const settings = climateSettings(script);
    root.innerHTML = `
      <style>
        .climate-profile{border:1px solid var(--divider-color,#454545);border-radius:12px;padding:16px;margin-bottom:16px}
        .climate-profile h3{margin:0 0 8px;font-size:18px}.climate-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:16px 0}
        .climate-grid label{display:grid;gap:6px;min-width:0;font-size:14px}
        .climate-profile select{width:100%;min-height:44px;border:1px solid var(--divider-color,#555);border-radius:8px;padding:8px;background:var(--card-background-color,#252525);color:var(--primary-text-color,#eee);font:inherit}
        .climate-actions{display:flex;gap:8px;flex-wrap:wrap}.climate-status{margin-top:10px;font-size:13px;color:var(--secondary-text-color,#bbb)}
        @media(max-width:500px){.climate-grid{grid-template-columns:1fr}.climate-profile{padding:12px}}
      </style>
      <section class="climate-profile" aria-label="Климатический профиль">
        <h3>Климатический профиль</h3>
        <div class="hint">Настройки для следующего выполнения сценария. «Не менять» не задаёт отдельную команду для выбранного параметра; итоговое состояние зависит от штатного запуска климата.</div>
        ${scripts.length > 1 ? `<label>Программа<select id="climate-program">${scripts.map(s => `<option value="${s.slot}">${escapeHtml(s.name)}</option>`).join('')}</select></label>` : ''}
        <div class="climate-grid">${climateFields.map(([key, label]) => `<label for="climate-${key}">${label}<select id="climate-${key}">${climateOptions(key).map(([v, text]) => `<option value="${v}">${text}</option>`).join('')}</select></label>`).join('')}</div>
        <div class="climate-actions"><button class="btn" id="climate-apply" ${this._editable ? '' : 'disabled'}>Применить к черновику</button><button class="btn sec" id="climate-refresh">Загрузить из черновика</button></div>
        <div class="climate-status" id="climate-status" aria-live="polite"></div>
      </section>`;
    for (const [key] of climateFields) this.querySelector(`#climate-${key}`).value = settings[key] === null ? 'keep' : String(settings[key]);
    for (const [key] of climateFields) this.querySelector(`#climate-${key}`).addEventListener('change', () => {
      this._climateDirty = true;
      this.querySelector('#climate-status').textContent = 'Есть изменения: нажмите «Применить к черновику» перед сохранением.';
    });
    const readDraft = () => JSON.parse(this.querySelector('#slot-json').value);
    const select = this.querySelector('#climate-program');
    if (select && scripts.length > 1) {
      select.value = String(script.slot);
      select.addEventListener('change', () => {
        if (this._saving) return;
        if (this._climateDirty) {
          select.value = String(this._climateSlot);
          this.querySelector('#editor-error').textContent = 'Примените изменения к черновику перед выбором другой программы.';
          return;
        }
        this._climateSlot = Number(select.value);
        try { this._renderClimateForm(readDraft(), bundle); } catch (err) { this.querySelector('#editor-error').textContent = err.message; }
      });
    }
    this.querySelector('#climate-refresh').addEventListener('click', () => {
      if (this._saving) return;
      try { this._renderClimateForm(readDraft(), bundle); } catch (err) { this.querySelector('#editor-error').textContent = err.message; }
    });
    this.querySelector('#climate-apply').addEventListener('click', () => {
      if (this._saving || !this._editable) return;
      try {
        const current = readDraft();
        if (bundle) validateCommandDocument(current); else validateScript(current);
        const base = bundle ? current.scripts.find(s => s.slot === this._climateSlot) : {...current, slot: this._activeSlot};
        if (!base) throw new Error('Программа удалена из черновика');
        const values = Object.fromEntries(climateFields.map(([key]) => {
          const value = this.querySelector(`#climate-${key}`).value;
          return [key, value === 'keep' ? null : value === '' ? NaN : Number(value)];
        }));
        const updated = compileClimateProgram(base, values);
        let draft;
        if (bundle) {
          current.scripts = current.scripts.map(s => s.slot === updated.slot ? updated : s);
          draft = current;
          this._renderCommandPreview(draft);
        } else {
          const {slot, ...spec} = updated; draft = spec;
          this._renderDecodedSections(draft);
        }
        this.querySelector('#slot-json').value = JSON.stringify(draft, null, 2);
        this.querySelector('#editor-error').textContent = '';
        this._renderClimateForm(draft, bundle);
        this.querySelector('#climate-status').textContent = 'Профиль добавлен в черновик. Нажмите кнопку сохранения, чтобы записать программу.';
      } catch (err) { this.querySelector('#editor-error').textContent = err.message; }
    });
    this._climateBusy(this._saving);
  }

  _climateBusy(busy) {
    for (const id of ['climate-apply', 'climate-refresh', 'climate-program', ...climateFields.map(([key]) => `climate-${key}`)]) {
      const element = this.querySelector(`#${id}`);
      if (element) element.disabled = busy || !this._editable;
    }
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
      lines.push(explainCanScript(s));
      const others = this._bundleContext.filter(c => c.slot === s.slot && !doc.scenarios.some(d => d.index === c.index));
      if (others.length) lines.push(`Этот CAN-слот также используется сценариями: ${others.map(s => s.index).join(", ")}. Изменение кадров повлияет и на них.`);
    }
    this.querySelector("#command-preview").textContent = lines.join("\n") || "Связанных сценариев нет. Можно добавить новый сценарий для этой команды.";
    this.querySelector("#command-preview").style.whiteSpace = "pre-wrap";
  }

  async _saveCommand(text) {
    if (this._climateDirty) { this.querySelector('#editor-error').textContent = 'Сначала примените климатические настройки к черновику.'; return; }
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
    this._climateBusy(true);
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
      this._climateBusy(false);
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

    summaryEl.textContent = `🎯 ${climateSettings(s) ? explainCanScript(s) : (s.summary || explainCanScript(s))}`;

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
      <div id="climate-form"></div>
      <details id="slot-advanced"><summary>CAN-кадры (JSON)</summary>
      <div class="section-title">Расшифровка кадров</div>
      <div class="summary-box" id="summary-target"></div>

      <div class="section-title" style="margin-top:6px;">Последовательность кадров</div>
      <div class="frames-list" id="frames-target"></div>

      <div class="section-title" style="margin-top:8px;">Редактор (JSON)</div>
      <div class="hint">До 4 кадров. bus — CAN1–CAN3; delay_ms — 0–10230 мс, шаг 10; count — число отправок; dlc — 0–8. data хранит 8 байт (16 HEX-символов), передаётся dlc байт. repeat — повтор всей программы; chain_next — переход к следующему слоту. Запись проверяется чтением и не запускает скрипт.</div>
      <div class="hint">${this._editable ? "" : "Перечитайте слоты. Если запись остаётся недоступной, формат этого слота пока не поддерживается редактором."}</div>
      <textarea class="json-area" id="slot-json"></textarea></details>
      <div class="error" id="editor-error" role="alert"></div>
    `;

    this.querySelector("#slot-json").value = JSON.stringify(jsonSpec, null, 2);
    this._renderDecodedSections(s);
    this._climateSlot = null;
    this._renderClimateForm({...jsonSpec, slot: s.slot}, false);
  }

  async _save(slot, text) {
    if (this._climateDirty) { this.querySelector('#editor-error').textContent = 'Сначала примените климатические настройки к черновику.'; return; }
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
    this._climateBusy(true);
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
      this._climateBusy(false);
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
