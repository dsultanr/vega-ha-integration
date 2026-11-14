// Browser-facing behavior without HA, a tracker, or additional packages.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() { this.nodes = {}; this.children = []; this.innerHTML = ''; this.value = ''; this.style = {}; this.listeners = {}; }
  querySelector(selector) { return this.nodes[selector] ||= new Element(); }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  setAttribute(key, value) { this[key] = value; }
  appendChild(child) { this.children.push(child); }
  dispatchEvent(event) { this.lastEvent = event; }
}

let Editor;
vm.runInNewContext(fs.readFileSync(path.join(__dirname,
  '../custom_components/vega/www/vega/vega-can-editor.js'), 'utf8'), {
  HTMLElement: Element,
  document: {createElement: () => new Element()},
  customElements: {get: () => Editor, define: (_, ctor) => { Editor = ctor; }},
  CustomEvent: class { constructor(type, options) { Object.assign(this, {type}, options); } },
  window: {}, console: {info() {}},
});

function editor(slot = 5) {
  const e = new Editor();
  e.setConfig({entity: 'sensor.test_can_scripts'});
  e._view = 'slots';
  const data = {slot, name: 'RemoteOn', repeat: 0, format_version: 2, frame_type: 1, chain_next: false, raw: 'aa'.repeat(82), editable: true,
    frames: [{delay_ms: 20, can_id: 146, bus: 1, count: 0, dlc: 8, data: '0000600000000000'}]};
  const calls = [];
  e.hass = {states: {sensor: {}, 'sensor.test_can_scripts': {attributes: {
    entry_id: 'tracker-one', slots: [data], command_bindings: {}, scenarios_read_at: 'today',
  }}}, callWS: async msg => { calls.push(msg); return {response: {verified: true}}; }};
  e._openModal(data);
  return {e, data, calls};
}

test('telemetry updates keep unsaved draft and original revision, including slot zero', () => {
  for (const slot of [0, 5]) {
    const {e, data} = editor(slot);
    const textarea = e.querySelector('#slot-json');
    assert.equal(JSON.parse(textarea.value).repeat, 0);
    textarea.value = 'unfinished draft';
    data.raw = 'bb'.repeat(82);
    data.frames[0].data = '0000000000000000'; // Same name and frame count.
    const previousSignature = e._sig;
    e._update();
    assert.notEqual(e._sig, previousSignature);
    assert.equal(textarea.value, 'unfinished draft');
    assert.equal(e._baselineRaw, 'aa'.repeat(82));
    assert.equal(e._activeSlot, slot);
  }
});

test('save selects original tracker, passes revision, and waits for verification', async () => {
  const {e, calls} = editor();
  e._attributes().entry_id = 'tracker-two';
  const text = e.querySelector('#slot-json').value;
  await e._save(5, text);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].service, 'write_can_script');
  assert.equal(calls[0].return_response, true);
  assert.equal(calls[0].service_data.entry_id, 'tracker-one');
  assert.equal(calls[0].service_data.expected_raw, 'aa'.repeat(82));
  assert.equal(calls[0].service_data.script.repeat, 0);
  assert.equal(e._activeSlot, null);
  assert.match(e.lastEvent.detail.message, /проверено чтением/);
});

test('service failure or unverified response keeps the draft and shows error', async () => {
  for (const callWS of [async () => { throw new Error('Слот изменился'); }, async () => ({response: {}})]) {
    const {e} = editor();
    e._hass.callWS = callWS;
    const text = e.querySelector('#slot-json').value;
    await e._save(5, text);
    assert.equal(e._activeSlot, 5);
    assert.equal(e.querySelector('#slot-json').value, text);
    assert.match(e.querySelector('#editor-error').textContent, /Ошибка записи/);
    assert.equal(e._saving, false);
    assert.equal(e.querySelector('#save-btn').disabled, false);
    assert.equal(e.lastEvent, undefined);
  }
});

test('pending write disables editing and duplicate saves', async () => {
  const {e} = editor();
  let resolve;
  let count = 0;
  e._hass.callWS = () => { count++; return new Promise(r => { resolve = r; }); };
  const text = e.querySelector('#slot-json').value;
  const first = e._save(5, text);
  assert.equal(e.querySelector('#save-btn').disabled, true);
  assert.equal(e.querySelector('#slot-json').disabled, true);
  await e._save(5, text);
  assert.equal(count, 1);
  resolve({response: {verified: true}});
  await first;
});

test('invalid JSON or truncated frame data is rejected before service call', async () => {
  const {e, calls} = editor();
  const script = JSON.parse(e.querySelector('#slot-json').value);
  script.frames[0].data = 'ff';
  for (const input of ['{', JSON.stringify(script)]) {
    await e._save(5, input);
    assert.ok(e.querySelector('#editor-error').textContent);
  }
  assert.equal(calls.length, 0);
});

test('cached old-format slot stays read-only until read from tracker', async () => {
  const {e, data, calls} = editor();
  delete data.editable;
  delete data.raw;
  e._openModal(data);
  assert.equal(e._editable, false);
  await e._save(5, e.querySelector('#slot-json').value);
  assert.equal(calls.length, 0);
});

test('slot names cannot inject markup into modal or list', () => {
  const {e, data} = editor();
  data.name = '</textarea><img src=x onerror=alert(1)>';
  e._openModal(data);
  e._update();
  assert.ok(!e._modalContainer.innerHTML.includes('<img'));
  assert.equal(JSON.parse(e.querySelector('#slot-json').value).name, data.name);
  const row = e._wrap.children.at(-1).children[0];
  assert.ok(!row.innerHTML.includes('<img'));
});

test('read distinguishes scripts from unavailable scenario bindings', async () => {
  const {e, calls} = editor();
  e._hass.callWS = async msg => { calls.push(msg); return {response: {slots: 8, scenarios_read: false}}; };
  await e._read();
  assert.equal(calls[0].service, 'read_commands');
  assert.match(e.lastEvent.detail.message, /сценарии команд недоступны/);
  assert.equal(e.querySelector('#read').disabled, false);
});

function commandEditor(action = 'preheat') {
  const {e, calls, data} = editor(4);
  const attrs = e._attributes();
  attrs.scenarios = Array.from({length: 25}, (_, index) => ({index,
    conditions: [], slot: null, editable: true, has_other_actions: false, raw: '00'.repeat(50),
    document: {conditions: [null, null], match: 'any', actions: {external_output_on: 0,
      external_output_on_seconds: 0, external_output_off: 0, external_output_off_seconds: 0, can_script: null}},
  }));
  const preheat = attrs.scenarios[3];
  preheat.conditions = [{sensor: 1347, threshold: 0, comparison: 1}];
  preheat.slot = 4;
  preheat.document.conditions = [{sensor: 1347, comparison: 'greater', value: 0}, null];
  Object.assign(preheat.document.actions, {external_output_on: 1, external_output_off: 7, can_script: 4});
  for (const index of [1, 10, 11]) {
    const s = attrs.scenarios[index];
    s.conditions = [{sensor: 1341, threshold: 1, comparison: 0}];
    s.document.conditions = [{sensor: 1341, comparison: 'equal', value: 1}, null];
  }
  attrs.command_bindings = {
    preheat: {setting_id: 1347, value: 1, scenario_routes: [{scenario: 3}]},
    remote_on: {setting_id: 1341, value: 1, scenario_routes: [1, 10, 11].map(scenario => ({scenario}))},
    remote_off: {setting_id: 1341, value: 0, scenario_routes: []},
  };
  e._view = 'commands'; e._update();
  e._hass.callWS = async msg => { calls.push(msg); return {response: {verified: true, written: ['4001/3']}}; };
  e._openCommandEditor(attrs.command_bindings[action].scenario_routes.map(r => r.scenario), action, action);
  return {e, calls, data, attrs};
}

test('command editor groups outputs, conditions and linked CAN; Remote On has no invented slot', () => {
  const {e} = commandEditor();
  const doc = JSON.parse(e.querySelector('#slot-json').value);
  assert.equal(doc.scenarios[0].actions.external_output_on, 1);
  assert.equal(doc.scenarios[0].actions.external_output_off, 7);
  assert.equal(doc.scripts[0].slot, 4);
  assert.match(e.querySelector('#command-preview').textContent, /1341=1; 1347=0; CAN-слот 4/);
  const remote = commandEditor('remote_on').e;
  const remoteDoc = JSON.parse(remote.querySelector('#slot-json').value);
  assert.deepEqual(remoteDoc.scenarios.map(s => s.index), [1, 10, 11]);
  assert.deepEqual(remoteDoc.scripts, []);
  assert.match(remote.querySelector('#command-preview').textContent, /1341=0 → пауза 1 с → 1341=1/);
});

test('command save pins tracker and all snapshots while telemetry keeps the draft', async () => {
  const {e, attrs, calls} = commandEditor();
  const text = e.querySelector('#slot-json').value;
  attrs.entry_id = 'tracker-two';
  attrs.scenarios[3].raw = 'ff'.repeat(50);
  attrs.scenarios[3].document.actions.external_output_on = 2;
  e.hass = e._hass;
  assert.equal(e.querySelector('#slot-json').value, text);
  await e._saveCommand(text);
  const request = calls[0];
  assert.equal(request.service, 'write_commands');
  assert.equal(request.service_data.entry_id, 'tracker-one');
  assert.equal(request.service_data.expected['4001/3'], '00'.repeat(50));
  assert.equal(Object.keys(request.service_data.expected).length, 26);
  assert.equal(request.service_data.document.scenarios[0].actions.external_output_on, 1);
  assert.equal(e._activeCommand, null);
});

test('partial command failure and unverified response keep JSON and never claim success', async () => {
  for (const callWS of [async () => { throw new Error('Подтверждены: 5502/4. Ошибка 4001/3'); }, async () => ({response: {}})]) {
    const {e} = commandEditor(); e._hass.callWS = callWS;
    const text = e.querySelector('#slot-json').value;
    await e._saveCommand(text);
    assert.equal(e.querySelector('#slot-json').value, text);
    assert.equal(e._activeCommand, 'preheat');
    assert.equal(e.querySelector('#slot-json').disabled, false);
    assert.match(e.querySelector('#editor-error').textContent, /Ошибка сохранения/);
    assert.equal(e.lastEvent, undefined);
  }
});

test('add scenario uses a free record and the original command trigger', () => {
  const {e, attrs} = commandEditor('remote_off');
  attrs.command_bindings.remote_off.setting_id = 9999;
  e.querySelector('#add-scenario').listeners.click();
  const doc = JSON.parse(e.querySelector('#slot-json').value);
  assert.equal(doc.scenarios[0].index, 0);
  assert.deepEqual(doc.scenarios[0].conditions, [{sensor: 1341, comparison: 'equal', value: 0}, null]);
  assert.equal(doc.scenarios[0].actions.can_script, null);
});

test('malformed command, duplicate scenarios and removal are rejected before write', async () => {
  const {e, calls} = commandEditor();
  const doc = JSON.parse(e.querySelector('#slot-json').value);
  for (const invalid of ['{', JSON.stringify({...doc, scenarios: []}),
    JSON.stringify({...doc, scenarios: [doc.scenarios[0], doc.scenarios[0]]}),
    JSON.stringify({...doc, scenarios: [{...doc.scenarios[0], actions: {...doc.scenarios[0].actions, external_output_on: 16}}]})]) {
    await e._saveCommand(invalid);
    assert.ok(e.querySelector('#editor-error').textContent);
  }
  assert.equal(calls.length, 0);
  doc.scenarios[0].conditions = [null, null];
  await e._saveCommand(JSON.stringify(doc));
  assert.equal(calls.length, 1);
});

test('command preview identifies shared CAN slots and preserved other actions', () => {
  const {e, attrs} = commandEditor();
  attrs.scenarios[5].slot = 4;
  attrs.scenarios[3].has_other_actions = true;
  e._openCommandEditor([3], 'Прогрев', 'preheat');
  assert.match(e.querySelector('#command-preview').textContent, /также используется сценариями: 5/);
  assert.match(e.querySelector('#command-preview').textContent, /дополнительные действия.*сохраняются без изменений/);
});

test('unsupported command format blocks writes; stale data cannot open an editor', async () => {
  const {e, attrs, calls} = commandEditor();
  attrs.scenarios[3].editable = false;
  e._openCommandEditor([3], 'Прогрев', 'preheat');
  await e._saveCommand(e.querySelector('#slot-json').value);
  assert.equal(calls.length, 0);
  assert.match(e.querySelector('#editor-error').textContent, /неподдерживаемый формат/);
  e.querySelector('#close-btn').listeners.click();
  attrs.scenarios_read_at = null;
  e._openCommandEditor([3], 'Прогрев', 'preheat');
  assert.equal(e._activeCommand, null);
});

test('pending command write blocks duplicate save, close and adding scenarios', async () => {
  const {e} = commandEditor();
  let resolve, count = 0;
  e._hass.callWS = () => { count++; return new Promise(r => { resolve = r; }); };
  const text = e.querySelector('#slot-json').value;
  const pending = e._saveCommand(text);
  assert.equal(e.querySelector('#slot-json').disabled, true);
  e.querySelector('#close-btn').listeners.click();
  e.querySelector('#add-scenario').listeners.click();
  await e._saveCommand(text);
  assert.equal(e._activeCommand, 'preheat');
  assert.equal(e.querySelector('#slot-json').value, text);
  assert.equal(count, 1);
  resolve({response: {verified: true, written: ['4001/3']}});
  await pending;
});

test('native fields survive slot and command drafts, including zero send count', async () => {
  const {e, calls, data} = editor();
  data.chain_next = true;
  e._openModal(data);
  const text = e.querySelector('#slot-json').value;
  const doc = JSON.parse(text);
  assert.equal(doc.format_version, 2);
  assert.equal(doc.chain_next, true);
  assert.equal(doc.frames[0].bus, 1);
  assert.equal(doc.frames[0].count, 0);
  assert.equal(doc.frames[0].dlc, 8);
  assert.match(e.querySelector('#frames-target').innerHTML, /Отправок: 0/);
  await e._save(5, text);
  assert.equal(calls[0].service_data.script.frames[0].count, 0);
  const bundle = JSON.parse(commandEditor().e.querySelector('#slot-json').value);
  assert.equal(bundle.scripts[0].format_version, 2);
  assert.equal(bundle.scripts[0].frames[0].count, 0);
});

test('legacy drafts, missing count and nonrepresentable delays cannot be saved', async () => {
  const {e, calls, data} = editor();
  const original = JSON.parse(e.querySelector('#slot-json').value);
  for (const mutation of [d => delete d.format_version, d => delete d.frames[0].count,
      d => delete d.frames[0].dlc, d => d.frames[0].delay_ms = 21,
      d => d.frames[0].bus = 0, d => d.frame_type = 3]) {
    const doc = JSON.parse(JSON.stringify(original)); mutation(doc);
    await e._save(5, JSON.stringify(doc));
    assert.ok(e.querySelector('#editor-error').textContent);
  }
  assert.equal(calls.length, 0);
  delete data.format_version;
  e._openModal(data);
  assert.equal(e._editable, false);
});

const preparedClimate = JSON.parse(fs.readFileSync(path.join(__dirname, '../tools/preheat_profile.json'), 'utf8'));
function climateEditor(bundle = false) {
  const state = bundle ? commandEditor() : editor(4);
  Object.assign(state.data, JSON.parse(JSON.stringify(preparedClimate.script)), {slot:4, editable:true, raw:preparedClimate.candidate_hex});
  if (bundle) state.e._openCommandEditor([3], 'Прогрев', 'preheat'); else state.e._openModal(state.data);
  return state;
}
function chooseClimate(e, values) {
  for (const [key, value] of Object.entries(values)) e.querySelector(`#climate-${key}`).value = value === null ? 'keep' : String(value);
  e.querySelector('#climate-apply').listeners.click();
}
const variedClimate = {temperature:23.5, fan:3, airflow:4, driver:1, passenger:2, rear_left:2, rear_right:1, wheel:2};

test('climate form decodes the current program and stays local until save', async () => {
  const {e, calls} = climateEditor();
  assert.equal(e.querySelector('#climate-temperature').value, '22');
  assert.equal(e.querySelector('#climate-airflow').value, '2');
  assert.equal(e.querySelector('#climate-driver').value, '3');
  assert.equal(e.querySelector('#climate-passenger').value, '0');
  assert.equal(e.querySelector('#climate-rear_left').value, 'keep');
  assert.equal(e.querySelector('#slot-advanced').open, false);
  chooseClimate(e, variedClimate);
  const doc = JSON.parse(e.querySelector('#slot-json').value);
  assert.deepEqual(doc.frames.map(f => f.data), ['00800a1a00000000','0000090000000000','fcff4fff0c1f6800','4400400000000000']);
  assert.deepEqual(doc.frames.map(f => f.count), [1,1,2,3]);
  assert.equal(e.querySelector('#climate-temperature').value, '23.5');
  assert.equal(calls.length, 0);
  assert.equal(e._baselineRaw, preparedClimate.candidate_hex);
  await e._save(4, e.querySelector('#slot-json').value);
  assert.equal(calls[0].service_data.expected_raw, preparedClimate.candidate_hex);
});

test('all seat and wheel levels encode OFF separately from leave unchanged', () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/climate_commands.json'), 'utf8'));
  for (const level of [null,0,1,2,3]) {
    const {e} = climateEditor();
    chooseClimate(e, {temperature:22,fan:null,airflow:null,driver:level,passenger:level,rear_left:level,rear_right:level,wheel:level});
    const doc = JSON.parse(e.querySelector('#slot-json').value);
    const front = Buffer.from(doc.frames[0].data, 'hex'), rear = Buffer.from(doc.frames[3].data, 'hex');
    assert.equal(front[3], level === null ? 0 : (level+1) * 9);
    const code = level === null ? 0 : level === 0 ? 4 : level;
    assert.equal(rear[0], code * 36);
    assert.equal(rear[2], code * 32);
    if (level !== null) {
      assert.equal(rear[0] & 0xe0, Buffer.from(fixture.heat_commands.rear_left[level][0], 'hex')[0]);
      assert.equal(rear[0] & 0x1c, Buffer.from(fixture.heat_commands.rear_right[level][0], 'hex')[0]);
      assert.equal(rear[2], Buffer.from(fixture.heat_commands.wheel[level][0], 'hex')[2]);
    }
    assert.equal(e.querySelector('#climate-driver').value, level === null ? 'keep' : String(level));
  }
});

test('climate controls preserve scenario conditions, outputs, snapshots and unrelated programs', async () => {
  const {e, calls, attrs} = climateEditor(true);
  const before = JSON.parse(e.querySelector('#slot-json').value);
  const originalSnapshots = {...e._bundleExpected};
  chooseClimate(e, variedClimate);
  const after = JSON.parse(e.querySelector('#slot-json').value);
  assert.deepEqual(after.scenarios, before.scenarios);
  assert.deepEqual(JSON.parse(JSON.stringify(e._bundleExpected)), originalSnapshots);
  attrs.entry_id = 'changed-tracker';
  await e._saveCommand(e.querySelector('#slot-json').value);
  assert.equal(calls[0].service_data.entry_id, 'tracker-one');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].service_data.document.scenarios)), before.scenarios);
});

test('invalid temperature cannot replace the draft; pending save disables climate controls', async () => {
  const {e} = climateEditor();
  const before = e.querySelector('#slot-json').value;
  chooseClimate(e, {...variedClimate, temperature:23.25});
  assert.equal(e.querySelector('#slot-json').value, before);
  assert.match(e.querySelector('#editor-error').textContent, /Температура/);
  chooseClimate(e, variedClimate);
  let resolve;
  e._hass.callWS = () => new Promise(r => {resolve = r;});
  const saved = e.querySelector('#slot-json').value;
  const pending = e._save(4, saved);
  assert.equal(e.querySelector('#climate-temperature').disabled, true);
  chooseClimate(e, {...variedClimate, temperature:20});
  assert.equal(e.querySelector('#slot-json').value, saved);
  resolve({response:{verified:true}}); await pending;
});

test('unknown CAN commands never become a climate form', () => {
  const {e, data} = climateEditor();
  data.frames[0].can_id = 0x701;
  e._openModal(data);
  assert.equal(e.querySelector('#climate-form').innerHTML, '');
  assert.equal(e.querySelector('#slot-advanced').open, true);
});

test('climate fan and airflow fields match independently captured OEM commands', () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/climate_commands.json'), 'utf8'));
  const {e} = climateEditor();
  for (const [i, commands] of Object.values(fixture.airflow_commands).entries()) {
    chooseClimate(e, {...variedClimate, airflow:i+1});
    assert.equal(JSON.parse(e.querySelector('#slot-json').value).frames[2].data.slice(4,6), commands[0].slice(4,6));
  }
  for (let fan=1; fan<=8; fan++) {
    chooseClimate(e, {...variedClimate, fan});
    const data = JSON.parse(e.querySelector('#slot-json').value).frames[2].data;
    const commands = fixture.fan_commands[`fan${fan}`];
    if (commands.length) assert.equal(data.slice(8,10), commands[0].slice(8,10));
    assert.equal(e.querySelector('#climate-fan').value, String(fan));
  }
  for (const [temperature, code] of [[17,'00'],[22,'50'],[22.5,'58'],[23.5,'68'],[31.5,'e8'],[32,'f0']]) {
    chooseClimate(e, {...variedClimate, temperature});
    assert.equal(JSON.parse(e.querySelector('#slot-json').value).frames[2].data.slice(12,14), code);
    assert.equal(e.querySelector('#climate-temperature').value, String(temperature));
  }
});

test('climate recognition rejects chains, inactive activation frames and unknown inactive fields', () => {
  for (const mutate of [d => {d.chain_next=true;}, d => {d.frames[0].count=0;},
      d => {d.frames[2].count=0;}, d => {d.frames[3].count=0; d.frames[3].data='0001000000000000';}]) {
    const {e,data} = climateEditor(); mutate(data); e._openModal(data);
    assert.equal(e.querySelector('#climate-form').innerHTML, '');
    assert.equal(e.querySelector('#slot-advanced').open, true);
  }
});

test('multiple climate programs retain other drafts and reject unapplied switching or saves', async () => {
  const {e,calls} = climateEditor(true);
  const doc = JSON.parse(e.querySelector('#slot-json').value);
  const second = {...JSON.parse(JSON.stringify(doc.scripts[0])),slot:5,name:'Другой профиль'};
  doc.scripts.push(second);
  e.querySelector('#slot-json').value = JSON.stringify(doc);
  e._renderClimateForm(doc,true);
  e.querySelector('#climate-temperature').value='20';
  e.querySelector('#climate-temperature').listeners.change();
  await e._saveCommand(e.querySelector('#slot-json').value);
  assert.equal(calls.length,0);
  const selector = e.querySelector('#climate-program');
  selector.value='5'; selector.listeners.change();
  assert.equal(e._climateSlot,4);
  chooseClimate(e,variedClimate);
  let after = JSON.parse(e.querySelector('#slot-json').value);
  assert.deepEqual(after.scripts[1],second);
  const first = after.scripts[0];
  e.querySelector('#climate-program').value='5';
  e.querySelector('#climate-program').listeners.change();
  chooseClimate(e,{...variedClimate,temperature:20});
  after = JSON.parse(e.querySelector('#slot-json').value);
  assert.deepEqual(after.scripts[0],first);
  assert.equal(after.scripts[1].slot,5);
});
