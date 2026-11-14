// Optional real-browser smoke test. Requires Playwright with Chromium and Python 3.11+.
// Uses archived fixtures and a mocked HA service; never connects to a tracker.
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const attrs = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-B', '-c', `
import importlib.util, json
from pathlib import Path
spec = importlib.util.spec_from_file_location('protocol', 'custom_components/vega/protocol.py')
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
scenarios = [p.decode_scenario(bytes.fromhex(raw), int(i)) for i, raw in json.loads(Path('tests/fixtures/scenarios.json').read_text()).items()]
slots = [dict(p.decode_can_script(bytes.fromhex(raw)), slot=int(i), editable=True) for i, raw in json.loads(Path('tests/fixtures/can_slots.json').read_text()).items()]
print(json.dumps(dict(entry_id='fixture-tracker', scenarios=scenarios, slots=slots, scenarios_read_at='today', command_bindings=p.command_script_bindings(scenarios))))
`], {cwd: root, encoding: 'utf8'}));

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 900}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setContent('<style>body{font-family:sans-serif;background:#151515;color:#eee}ha-card{display:block;max-width:800px;margin:auto}</style>');
    await page.addScriptTag({path: path.join(root, 'custom_components/vega/www/vega/vega-can-editor.js')});
    await page.evaluate(attrs => {
      window.calls = [];
      window.toasts = [];
      const editor = document.createElement('vega-can-editor');
      editor.setConfig({entity: 'sensor.fixture'});
      editor.hass = {states: {'sensor.fixture': {attributes: attrs}}, callWS: async msg => {
        window.calls.push(msg);
        if (window.failSave) throw new Error('Подтверждены: 5502/4. Ошибка 4001/3');
        return {response: {verified: true, written: ['4001/3']}};
      }};
      editor.addEventListener('hass-notification', e => window.toasts.push(e.detail.message));
      document.body.appendChild(editor);
      window.editor = editor;
    }, attrs);
    assert.equal(await page.locator('.command-row').count(), 9);
    await page.locator('.command-row').filter({has: page.locator('.slot-title', {hasText: /^Прогрев$/})}).getByRole('button').click();
    assert.match(await page.locator('#command-preview').innerText(), /1341=1; 1347=0; CAN-слот 4/);
    const doc = JSON.parse(await page.locator('#slot-json').inputValue());
    assert.equal(doc.scripts[0].slot, 4);
    assert.equal(doc.scripts[0].format_version, 2);
    assert.equal(doc.scripts[0].frames[0].delay_ms, 5000);
    assert.equal(doc.scripts[0].frames[0].bus, 1);
    assert.equal(doc.scripts[0].frames[0].count, 2);
    assert.equal(doc.scripts[0].frames[1].count, 0);
    assert.equal(doc.scripts[0].frames[2].bus, 2);
    doc.scenarios[0].actions.external_output_on_seconds = 2;
    const draft = JSON.stringify(doc, null, 2);
    await page.locator('#slot-json').fill(draft);
    assert.match(await page.locator('#command-preview').innerText(), /1341=1 на 2 с/);
    await page.evaluate(() => {
      editor._hass.states['sensor.fixture'].attributes.scenarios[3].raw = 'ff'.repeat(50);
      editor.hass = editor._hass;
      window.failSave = true;
    });
    assert.equal(await page.locator('#slot-json').inputValue(), draft);
    await page.locator('#save-btn').click();
    await page.waitForFunction(() => document.querySelector('#editor-error').textContent.includes('Ошибка сохранения'));
    assert.equal(await page.locator('#slot-json').inputValue(), draft);
    assert.equal((await page.evaluate(() => calls[0])).service_data.expected['4001/3'], attrs.scenarios[3].raw);
    assert.equal(await page.evaluate(() => toasts.length), 0);
    await page.evaluate(() => { window.failSave = false; });
    await page.locator('#save-btn').click();
    await page.locator('#slot-json').waitFor({state: 'detached'});
    assert.equal(await page.evaluate(() => calls[1].service), 'write_commands');
    assert.equal(await page.evaluate(() => calls[1].service_data.entry_id), 'fixture-tracker');
    assert.equal(await page.evaluate(() => Object.keys(calls[1].service_data.expected).length), 33);
    await page.locator('#tab-scenarios').click();
    assert.equal(await page.locator('.command-row').count(), 25);
    await page.locator('#tab-slots').click();
    assert.equal(await page.locator('.slot-row').count(), 8);
    await page.locator('#tab-commands').click();
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('.command-row').filter({has: page.locator('.slot-title', {hasText: /^Remote On$/})}).getByRole('button').click();
    const remote = JSON.parse(await page.locator('#slot-json').inputValue());
    assert.deepEqual(remote.scenarios.map(s => s.index), [1, 10, 11]);
    assert.deepEqual(remote.scripts, []);
    const box = await page.locator('.modal-box').boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390 && box.height <= 844);
    await page.locator('#save-btn').scrollIntoViewIfNeeded();
    if (process.env.SCREENSHOT) await page.screenshot({path: process.env.SCREENSHOT});
    await page.locator('#close-btn').click();
    assert.deepEqual(errors, []);
    console.log('PASS Chromium: command/scenario/slot views, draft, service payload, failure, success, mobile modal');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
