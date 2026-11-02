// Run after npm run build. PLAYPORT points to the patched source; install Playwright there with --no-save.
// CHROME selects a local browser; all device responses below are synthetic fixtures.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { createServer } from 'node:http';
const web = resolve(process.env.PLAYPORT || 'research/playport', 'web');
const { chromium } = createRequire(web + '/package.json')('playwright');
const server = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, 'http://localhost').pathname;
    const file = resolve(web, 'dist', '.' + (path === '/' ? '/index.html' : path));
    assert.ok(file.startsWith(web + '/dist/'));
    res.setHeader('Content-Type', ({'.js':'text/javascript','.css':'text/css','.html':'text/html'})[extname(file)] || 'application/octet-stream');
    res.end(await readFile(file));
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME});
try {
  for (const mobile of [false, true]) {
    const context = await browser.newContext({viewport:mobile ? {width:390,height:844} : {width:1280,height:800}, isMobile:mobile, hasTouch:mobile});
    const page = await context.newPage();
    const errors = [], saves = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/receiver-access.json', route => route.fulfill({json:{settingsPort:80}}));
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/display') {
        if (route.request().method() === 'POST') { saves.push(route.request().postDataJSON()); return route.fulfill({json:{ok:true}}); }
        return route.fulfill({json:{width:1280,height:720,fps:30,uiScale:100,hevc:false}});
      }
      return route.fulfill({json:{supported:false,phones:[]}});
    });
    await page.addInitScript(() => { localStorage.setItem('f50-auto-fit','false'); });
    const tap = async selector => mobile ? page.locator(selector).tap() : page.locator(selector).click();
    const option = (id, value) => `#${id} + .simple-options button[data-value="${value}"]`;
    await page.goto(`http://127.0.0.1:${server.address().port}/?token=synthetic-test-token`);
    await tap('#btn-settings');
    await tap('#simple-settings');
    assert.equal(await page.locator('#simple-settings').getAttribute('aria-pressed'), 'true');
    await tap('#picture-panel > summary');
    await tap(option('fullscreen-button','off'));
    assert.equal(await page.locator('#fullscreen-button').inputValue(),'off');
    await tap('#display-details > summary');
    await page.waitForFunction(() => document.querySelector('#width').value === '1280');
    await tap(option('fps','60'));
    await tap(option('codec','h264'));
    await tap(option('ui-scale','85'));
    await tap('#apply');
    await page.waitForFunction(() => !document.querySelector('#display-fields').disabled);
    assert.ok(saves.some(v => v.fps === 60 && v.uiScale === 85 && v.hevc === false), 'ordinary buttons use the real save handler');
    await page.evaluate(() => { document.querySelector('#display-fields').disabled = true; });
    await page.waitForFunction(() => document.querySelector('#fps + .simple-options button').disabled);
    await page.evaluate(() => { document.querySelector('#display-fields').disabled = false; });
    await page.waitForFunction(() => !document.querySelector('#fps + .simple-options button').disabled);
    assert.equal(await page.locator('#settings select:visible').count(), 0);
    assert.equal(await page.locator('#settings label .simple-options button').count(), 0, 'buttons must not activate a label picker');
    await tap(option('language','en'));
    assert.match(await page.locator('#simple-settings').innerText(), /Simple settings/);
    await page.reload();
    await tap('#btn-settings');
    assert.equal(await page.locator('#simple-settings').getAttribute('aria-pressed'),'true', 'choice survives refresh');
    await tap('#simple-settings');
    await tap('#picture-panel > summary');
    assert.ok(await page.locator('#fullscreen-button').isVisible());
    assert.equal(await page.locator('#fullscreen-button').inputValue(),'off', 'switching back preserves the choice');
    assert.equal(await page.evaluate(() => localStorage.getItem('f50-codec')), 'h264');
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`Simple settings: ${mobile ? 'mobile touch' : 'desktop'} passed (selection, submit, disabled state, language, persistence, switch back)`);
  }
} finally { await browser.close(); await new Promise(done => server.close(done)); }
