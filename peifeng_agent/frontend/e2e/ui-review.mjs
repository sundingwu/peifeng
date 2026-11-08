import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFileSync, mkdirSync } from 'node:fs';
import { resolve, extname } from 'node:path';
import assert from 'node:assert/strict';
const root = resolve('dist'), shots = resolve('../../docs/ui-review-2026-11-03');
mkdirSync(shots, {
    recursive: true
});
const ev = (seq, kind, data) => ({
    seq, kind, data, session: '研究记录'
});
const samplePresets = ["汇报文档", "数据图", "统计表格", "汇报 PPT", "目录整理"].map((name,index)=>({id:`preset-${index}`,name,description:"",fields:[]}));
const state = {
    context_usage: {model: "deepseek-flash", characters: 60000, estimated_tokens: 32000, limit: 1048576},
    projects: [{slug: 'research', name: '研究项目', path: 'D:/Research', folder: 'D:/Research', current: true, missing: false, sessions: [], archived: []}], project: '研究项目', session: '研究记录', workspace: 'D:/Research', default_parent: 'D:/', running: true, pending: [], files: [], seq: 5, tasks: [
        {
            seq: 1, requirement: '整理实验记录，并归纳本周进展', status: 'completed', summary: '整理实验记录', content: '### 本周研究进展\n已整理三组实验数据，并生成阶段记录。\n\n- 完成数据清点与缺失值检查\n- 对照结果已整理到报告\n\n下一步可以继续补充对照实验分析。', files: [
                'reports/本周进展.docx'
            ]
        },
        {
            seq: 2, requirement: '请把报告中的结论改得更清楚', status: 'completed', summary: '完善结论', content: '已将结论调整为现象、证据和后续行动三部分，保留原始数据及其不确定性。', files: []
        }
    ], trace: [
        ev(1, 'task_received', {
            requirement: '增加对照实验分析，生成最终报告，并核对全部图表与统计结论'
        }), ev(2, 'plan_ready', {
            summary: '核对数据，补充对照分析，整理最终报告', steps: [
                {
                    goal: '核对实验数据', assignee: 'office_docs'
                },
                {
                    goal: '补充对照分析', assignee: 'chart'
                },
                {
                    goal: '生成最终报告', assignee: 'office_docs'
                }
            ]
        }), ev(3, 'step_start', {
            index: 0, total: 3, goal: '核对实验数据', assignee: 'office_docs'
        }), ev(4, 'step_review', {
            index: 0, approved: true, summary: '数据完整'
        }), ev(5, 'step_start', {
            index: 1, total: 3, goal: '补充对照分析', assignee: 'chart'
        })
    ]
};
const server = createServer((req, res) => { if (req.url.startsWith('/api/events')) {
    res.writeHead(200, {
        'Content-Type': 'text/event-stream'
    });
    res.write(':ready\n\n');
    return;
} if (req.url.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(req.url.startsWith('/api/state') ? state : {
        presets: samplePresets
    }));
    return;
} const path = req.url === '/' ? resolve(root, 'index.html') : resolve(root, '.' + req.url.split('?')[0]); try {
    res.setHeader('Content-Type', ({
        '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg'
    })[extname(path)] ?? 'application/octet-stream');
    res.end(readFileSync(path));
}
catch {
    res.writeHead(404);
    res.end();
} });
await new Promise(done => server.listen(0, '127.0.0.1', done));
const browser = await chromium.launch();
try {
    const page = await browser.newPage({
        viewport: {
            width: 1440, height: 1000
        }
    });
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    for (const theme of [
        'light', 'dark'
    ]) {
        await page.goto(`http://127.0.0.1:${server.address().port}`, {
            waitUntil: 'domcontentloaded'
        });
        await page.evaluate(t => localStorage.setItem('peifeng-theme', t), theme);
        await page.reload({
            waitUntil: 'domcontentloaded'
        });
        await page.evaluate(t => { document.documentElement.dataset.theme = t; if (t === 'dark') {
            const values = {
                '--bg': '#101713', '--bg-elev': '#18221c', '--bg-sunken': '#131d17', '--border': '#2b3930', '--border-strong': '#425748', '--text': '#e6eee7', '--text-dim': '#a8b9ab', '--text-faint': '#809385', '--accent': '#8ac7a5', '--accent-soft': 'rgba(138,199,165,.1)', '--accent-border': 'rgba(138,199,165,.3)'
            };
            for (const [key, value] of Object.entries(values))
                document.documentElement.style.setProperty(key, value);
        } }, theme);
        await page.locator('.plan-todo li').first().waitFor();
        assert.equal(await page.locator('.plan-todo li').count(), 3);
        assert.equal(await page.locator('.conversation-turn').count(), 2);
        assert.equal(await page.locator('.todo-count').innerText(), '1/3');
        assert.equal(await page.locator('.conversation-nav button').count(), 3);
        const labels = await page.locator('.conversation-nav button').evaluateAll(elements => elements.map(el=>el.getAttribute('aria-label')));
        assert.ok(labels.every(label => Array.from(label).length <= 20));
        assert.equal(Array.from(labels.at(-1)).length, 20);
        const boxes = await page.evaluate(() => ({
            plan: document.querySelector('.plan-todo').getBoundingClientRect().bottom, chat: document.querySelector('.stream').getBoundingClientRect().top
        }));
        assert.ok(boxes.plan <= boxes.chat, 'plan overlaps conversation');
        assert.equal(await page.locator('.stream .plan-todo').count(), 0);
        assert.equal(await page.locator('.topbar .context-meter').count(), 1);
        assert.match(await page.locator('.context-numbers').innerText(), /32,000/);
        assert.equal(await page.locator('.context-track').getAttribute('role'), 'progressbar');
        assert.equal(await page.locator('.topbar').count(),1);
        assert.equal(await page.locator('.right-rail').count(),0);
        assert.equal(await page.locator('.header-verse').innerText(),'子非鱼，安知鱼之乐？');
        assert.equal(await page.locator('.brand img').getAttribute('src'),await page.locator('link[rel=icon]').getAttribute('href'));
        await page.waitForFunction(()=>document.querySelector('.brand img').naturalWidth>0);
        assert.equal(await page.title(),'培风');
        assert.equal(await page.locator('.project-total').count(), 0);
        assert.match(await page.locator('.sidebar-title').innerText(), /项目列表/);
        await page.locator('.stream-scroll').evaluate(el => el.scrollTop = 0);
        await page.screenshot({
            path: `${shots}/${theme}-desktop.png`, fullPage: true
        });
        console.log(theme, await page.locator('.plan-todo').evaluate(el => ({
            background: getComputedStyle(el).backgroundColor, color: getComputedStyle(el).color
        })));
    }
    await page.setViewportSize({
        width: 900, height: 900
    });
    await page.reload({
        waitUntil: 'domcontentloaded'
    });
    await page.locator('.plan-todo li').first().waitFor();
    await page.screenshot({
        path: `${shots}/light-narrow.png`, fullPage: true
    });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'horizontal overflow');
    await page.locator('.conversation-nav button').first().click();
    await page.waitForTimeout(400);
    const anchorTop = await page.locator('.stream-scroll').evaluate(el=>el.querySelector('[data-conversation-anchor]').getBoundingClientRect().top-el.getBoundingClientRect().top);
    assert.ok(Math.abs(anchorTop - 16) < 30, 'navigation did not reach target');
    await page.locator('.conversation-nav button').first().hover();
    await page.screenshot({path: `${shots}/quick-navigation.png`, fullPage: true});
    state.running = false;
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.locator('.poem-title').waitFor();
    await page.locator('.shortcut-grid button').first().waitFor();
    assert.ok(await page.locator('.shortcut-grid button').first().evaluate(el=>el.getBoundingClientRect().height)>=90);
    assert.equal(await page.locator('.shortcut-grid button').count(),3);
    const input = page.locator('.composer-input');
    assert.equal(await input.getAttribute('placeholder'),'不知周之梦为胡蝶与，胡蝶之梦为周与？');
    await input.focus();
    assert.equal(await input.evaluate(el=>getComputedStyle(el).outlineStyle),'none');
    assert.equal(await page.locator('.plan-todo').count(),0);
    await page.screenshot({path: `${shots}/minimal-home.png`,fullPage:true});
    const menu = page.locator('.project-row .row-menu').first();
    assert.equal(await menu.evaluate(el=>getComputedStyle(el).opacity),'1');
    await menu.click();
    await page.getByRole('menuitem', {name: '新建会话'}).waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('button', {name: /新建项目/}).click();
    await page.getByText('选择文件夹', {exact: true}).waitFor();
    await page.screenshot({path: `${shots}/new-project-folder.png`, fullPage: true});
    state.running=true;
    state.trace=[ev(10,'task_received',{requirement:'直接回答一个问题'})];
    await page.reload({waitUntil:'domcontentloaded'});
    await page.locator('.composer-input').waitFor();
    await page.waitForTimeout(150);
    assert.equal(await page.locator('.plan-todo').count(),0);
    state.running=false;
    await page.setViewportSize({width:390,height:844});
    await page.reload({waitUntil:'domcontentloaded'});
    await page.locator('.shortcut-grid button').first().waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    const headerBoxes=await page.evaluate(()=>({brand:document.querySelector('.brand').getBoundingClientRect().right,actions:document.querySelector('.capability-trigger').getBoundingClientRect().left}));
    assert.ok(headerBoxes.brand<headerBoxes.actions,'mobile header controls overlap');
    await page.screenshot({path:`${shots}/classical-mobile.png`,fullPage:true});
    const submissions=[];
    await page.route('**/api/task',async route=>{submissions.push(route.request().postDataJSON());await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true})});});
    const entries=[['PPT生成','演示主题','具体要求'],['Word生成','文档主题','具体要求'],['科研绘图','绘图主题','绘图要求']];
    for(const [index, [name,topic,requirements]] of entries.entries()){
      await page.reload({waitUntil:'domcontentloaded'});
      await page.locator('.shortcut-grid button').filter({hasText:name}).click();
      await page.getByLabel(topic,{exact:true}).fill('验证主题');
      await page.getByLabel(requirements,{exact:true}).fill('验证要求');
      const sent=page.waitForResponse(response=>response.url().endsWith('/api/task'));
      await page.locator('.ant-modal-footer .ant-btn-primary').click();
      await sent;
      assert.equal(submissions.length,index+1);
      assert.ok(submissions[index].text.includes(name));
      assert.ok(submissions[index].text.includes('验证要求'));
      assert.equal('preset' in submissions[index],false);
      assert.equal('values' in submissions[index],false);
    }
    assert.deepEqual(errors, []);
    console.log('UI verification passed', shots);
}
finally {
    await browser.close();
    server.closeAllConnections();
    server.close();
}
