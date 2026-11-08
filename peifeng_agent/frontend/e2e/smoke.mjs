import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import assert from 'node:assert/strict';

const capabilitiesOnly = process.argv.includes('--capabilities-only');
const base = process.argv[2] ?? 'http://127.0.0.1:8765';
mkdirSync('e2e/shots', { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.locator('.composer-input').waitFor();
  assert.equal(await page.title(), '培风');
  assert.equal(await page.locator('.sidebar-intro,.sidebar-footer,.composer-label').count(), 0);
  assert.equal(await page.locator('.topbar').count(),1);
  await page.waitForFunction(()=>document.querySelector('.brand img').naturalWidth>0);
  assert.equal(await page.locator('.header-verse').innerText(),'子非鱼，安知鱼之乐？');
  if (!capabilitiesOnly) {
    await page.locator('.poem-title').waitFor();
    await page.locator('.shortcut-grid button').first().waitFor();
    assert.ok(await page.locator('.shortcut-grid button').count()===3);
    assert.deepEqual(await page.locator('.shortcut-copy strong').allTextContents(),['PPT生成','Word生成','科研绘图']);
    await page.getByText('就绪',{exact:true}).waitFor();
    assert.equal(await page.locator('.header-actions button').innerText(),'能力与个性化');
    assert.equal(await page.locator('.settings-mini').count(),0);
    await page.locator('.shortcut-grid button').first().click();
    await page.getByText('PPT生成',{exact:true}).last().waitFor();
    await page.locator('.ant-modal-close').click();
    assert.match(await page.locator('.poem-title').innerText(), /故九万里/);
    assert.equal(await page.locator('.plan-todo').count(), 0);
    const usage = await page.locator('.context-numbers').innerText();
    assert.match(usage, /已用/);
    assert.doesNotMatch(usage, /已用 0\b/);
    await page.screenshot({ path: 'e2e/shots/minimal-live-home.png', fullPage: true });
    await page.getByRole('button', { name: /新建项目/ }).click();
    await page.getByText('选择文件夹', { exact: true }).waitFor();
    await page.locator('.ant-modal-close').click();
  }
  await page.getByRole('button', { name: '能力与个性化', exact: true }).click();
  await page.locator('.ant-drawer').waitFor();
  await page.locator('.cap-tool').first().waitFor();
  await page.waitForFunction(()=>{const el=document.querySelector('.ant-drawer-content-wrapper');if(!el)return false;const box=el.getBoundingClientRect();return box.width>400 && box.right<=innerWidth+1 && box.left>=0;});
  const toolCount = await page.locator('.cap-tool').count();
  assert.ok(toolCount > 0);
  assert.equal(await page.getByRole('button',{name:'查看描述',exact:true}).count(),toolCount);
  assert.equal(await page.locator('.cap-tool-description,.cap-tool-details').count(),0);
  await page.getByRole('button',{name:'查看描述',exact:true}).first().click();
  await page.locator('.tool-description-content').waitFor();
  assert.ok((await page.locator('.tool-description-content').innerText()).trim().length>0);
  await page.screenshot({path:'e2e/shots/tool-description-modal.png',fullPage:true});
  await page.locator('.ant-modal-close').click();
  const sharedNames = await page.locator('.cap-shared-tools .cap-tool-info > .ant-typography').allTextContents();
  assert.equal(new Set(sharedNames).size, sharedNames.length);
  await page.screenshot({path:'e2e/shots/tool-descriptions.png',fullPage:true});
  const searchRole = page.locator('.cap-role').filter({hasText:'web_search'});
  if (await searchRole.count()) {
    await searchRole.scrollIntoViewIfNeeded();
    assert.equal(await searchRole.getByRole('button',{name:'查看描述',exact:true}).count(),2);
    await page.screenshot({path:'e2e/shots/search-tool-descriptions.png',fullPage:true});
  }
  assert.deepEqual(errors, []);
  console.log('Live minimal UI verification passed (no task submitted, no preferences changed).');
} finally {
  await browser.close();
}

