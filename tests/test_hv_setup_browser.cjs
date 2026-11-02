// HV onboarding UI with real Chromium, mocked HA, and no tracker traffic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, args:['--no-sandbox'],
    ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath:process.env.CHROMIUM_EXECUTABLE} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1000,height:850}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setContent('<style>body{font-family:sans-serif;--primary-color:#03a9f4;--primary-text-color:#222;--card-background-color:#fff;--divider-color:#ccc;--secondary-text-color:#666}</style>');
    await page.addScriptTag({content:fs.readFileSync(path.resolve(__dirname,
      '../custom_components/vega/www/vega/vega-hv-setup.js'),'utf8')});
    await page.evaluate(() => {
      window.calls = [];
      window.reply = {plan_id:'plan-one',configured:false};
      window.hass = {states:{
        'sensor.renamed_config':{state:'0',attributes:{entry_id:'one',direct_control:false}},
        'sensor.renamed_watts':{state:'0'},
        'sensor.renamed_volts':{state:'361.4'},
        'sensor.renamed_amps':{state:'0'},
      },callWS:async request => {calls.push(request); return {response:reply};}};
      window.card = document.createElement('vega-hv-setup');
      card.setConfig({entry_id:'one',entity:'sensor.renamed_config',power_entity:'sensor.renamed_watts',
        voltage_entity:'sensor.renamed_volts',current_entity:'sensor.renamed_amps'});
      card.hass = hass;
      document.body.appendChild(card);
    });
    assert.equal(await page.evaluate(() => calls.length),0);
    assert.equal(await page.locator('#hv-check').isDisabled(),true);
    assert.equal(await page.locator('#hv-power').innerText(),'0 кВт');
    await page.locator('#hv-profile').selectOption('f515_ispace_4wd');
    assert.equal(await page.evaluate(() => calls.length),0);
    await page.locator('#hv-check').click();
    await page.locator('#hv-apply').waitFor();
    assert.deepEqual(await page.evaluate(() => calls.at(-1)), {type:'call_service',domain:'vega',
      service:'prepare_hv_battery',service_data:{entry_id:'one',profile:'f515_ispace_4wd'},return_response:true});
    assert.equal(await page.locator('#hv-apply').isDisabled(),true);
    assert.equal(await page.locator('#hv-control').isVisible(),true);
    await page.evaluate(() => {
      hass.states['sensor.renamed_config'].attributes.direct_control=true;
      hass.states['sensor.renamed_watts'].state='-2.676'; card.hass={...hass};
    });
    assert.equal(await page.locator('#hv-profile').inputValue(),'f515_ispace_4wd');
    assert.match(await page.locator('#hv-power').innerText(), /-2,676/);
    // One request even if the user double-clicks; progress remains visible.
    await page.evaluate(() => {
      hass.callWS = request => {calls.push(request); return new Promise(resolve => {window.finish=resolve;});};
      card.hass=hass;
    });
    await page.locator('#hv-apply').click();
    await page.locator('#hv-apply').evaluate(e=>e.click());
    assert.equal(await page.evaluate(() => calls.filter(c=>c.service==='configure_hv_battery').length),1);
    assert.deepEqual(await page.evaluate(() => calls.at(-1).service_data),{entry_id:'one',plan_id:'plan-one'});
    await page.evaluate(() => {
      hass.states['sensor.renamed_config'].attributes.hv_setup={phase:'reading',message:'Читаю настройки…',progress:64,total:136};
      card.hass=hass;
    });
    assert.match(await page.locator('#hv-status').innerText(), /64\/136/);
    await page.evaluate(() => finish({response:{verified:true,configured:true}}));
    await page.waitForFunction(() => !card._busy);
    assert.equal(await page.locator('#hv-apply').isVisible(),false);
    assert.match(await page.locator('#hv-status').innerText(), /сохранены и проверены/);
    // Already configured: a check never offers an unnecessary write.
    await page.evaluate(() => {hass.callWS=async request=>{calls.push(request);return {response:{plan_id:'existing',configured:true}};};});
    await page.locator('#hv-check').click();
    assert.equal(await page.locator('#hv-apply').isVisible(),false);
    assert.match(await page.locator('#hv-status').innerText(), /уже настроено/);
    // Error text is inert, and a failed check cannot reuse an earlier plan.
    await page.evaluate(() => {hass.callWS=async()=>{throw new Error('<img src=x onerror=alert(1)> Датчик занят');};});
    await page.locator('#hv-check').click();
    assert.match(await page.locator('#hv-status').innerText(), /Датчик занят/);
    assert.equal(await page.locator('img').count(),0);
    assert.equal(await page.locator('#hv-apply').isVisible(),false);
    // Late responses for the previous device cannot install its plan on another.
    await page.evaluate(() => {hass.callWS=()=>new Promise(resolve=>{window.oldReply=resolve;});});
    await page.locator('#hv-check').click();
    await page.evaluate(() => {
      hass.states['sensor.config_two']={state:'0',attributes:{entry_id:'two',direct_control:true}};
      card.setConfig({entity:'sensor.config_two',entry_id:'two'});
      card.hass=hass;
      oldReply({response:{plan_id:'wrong-device',configured:false}});
    });
    assert.equal(await page.locator('#hv-apply').isVisible(),false);
    assert.equal(await page.locator('#hv-profile').inputValue(),'');
    await page.locator('#hv-profile').selectOption('f515_ispace_4wd');
    await page.evaluate(() => {hass.callWS=async request=>{calls.push(request);return {response:{plan_id:'two',configured:false}};};});
    await page.locator('#hv-check').click();
    assert.equal(await page.evaluate(() => calls.at(-1).service_data.entry_id),'two');
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=390),true);
    for (const id of ['#hv-profile','#hv-check','#hv-apply']) {
      const box=await page.locator(id).boundingBox();
      assert.ok(box.x>=0 && box.x+box.width<=390);
    }
    if (process.env.SCREENSHOT) await page.screenshot({path:process.env.SCREENSHOT,fullPage:true});
    // Missing response after apply is not presented as success and needs rescan.
    await page.evaluate(() => {hass.callWS=async()=>({});});
    await page.locator('#hv-apply').click();
    assert.match(await page.locator('#hv-status').innerText(), /Не получено подтверждение/);
    assert.equal(await page.locator('#hv-apply').isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log('PASS HV setup Chromium: explicit profile, no automatic writes, scoped plan, Direct Control, progress, duplicate click, existing profile, errors, stale responses, renamed entities, signed/zero power, mobile');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
