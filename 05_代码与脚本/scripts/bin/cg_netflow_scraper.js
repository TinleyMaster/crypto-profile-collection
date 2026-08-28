/**
 * CoinGlass 网页端 netflow 抓取器（绕过 capi 加密响应）
 * 思路：用真实 Chrome 渲染 https://www.coinglass.com/InflowAndOutflow，
 *      前端 JS 自动解密并渲染到 DOM，直接读 DOM 精准数值（aria-label）。
 *
 * 输出：结构化 JSON（主表逐笔 + 链上告警 + 按交易所/币种聚合净流）
 *   同时打印到 stdout（可被管道消费）并写入 cg_netflow_latest.json
 *
 * 用法：node cg_netflow_scraper.js [--no-write]
 *
 * 环境变量：
 *   CG_URL        - 覆盖默认 CoinGlass 页面 URL（离线测试用）
 *   CHROME_PATH   - 覆盖 Chrome 可执行文件路径
 *   PLAYWRIGHT_PATH - 覆盖 playwright-core 模块路径
 */
const fs = require('fs');
const path = require('path');

// 可配置路径（优先环境变量，否则使用默认值）
const PLAYWRIGHT_PATH = process.env.PLAYWRIGHT_PATH || 'C:/Users/SuperTing/.workbuddy/binaries/node/workspace/node_modules/playwright-core';
const { chromium } = require(PLAYWRIGHT_PATH);

const CHROME_PATH = process.env.CHROME_PATH || 'C:/Users/SuperTing/.agent-browser/browsers/chrome-152.0.7977.64/chrome.exe';
const URL = process.env.CG_URL || 'https://www.coinglass.com/InflowAndOutflow';

// 解析 "1,499,837" / "126,386.621" -> 126386.621
const num = (s) => {
  if (s == null) return null;
  const cleaned = String(s).replace(/[^0-9.\-]/g, '');
  if (cleaned === '' || cleaned === '-' || cleaned === '.') return null;
  const v = parseFloat(cleaned);
  return isNaN(v) ? null : v;
};

(async () => {
  const noWrite = process.argv.includes('--no-write');
  const browser = await chromium.launch({
    executablePath: CHROME_PATH,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled', '--no-proxy-server']
  });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
    extraHTTPHeaders: { 'accept-language': 'zh-CN,zh;q=0.9', 'referer': 'https://www.coinglass.com/' }
  });
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') console.error('[console.error]', m.text().slice(0, 160)); });

  // 等待数据行"稳定"（ant-table 分批异步渲染，不能在首个 >0 就 break，否则只抓到初始批）
  async function waitStable() {
    let last = -1, stable = 0, waited = 0;
    while (waited < 45000) {
      const rc = await page.evaluate(() => document.querySelectorAll('tr[data-row-key]').length);
      if (rc > 0 && rc === last) stable++; else stable = 0;
      last = rc;
      if (rc > 0 && stable >= 3) return rc; // 连续 3 次(~6s)不变 => 渲染完成
      await page.waitForTimeout(2000);
      waited += 2000;
    }
    return last; // 超时也返回当前值
  }

  const resp = await page.goto(URL, { waitUntil: 'load', timeout: 60000 });
  const status = resp && resp.status();
  let rowCount = await waitStable();

  // 偶发空表兜底：reload + 冷却退避（capi.coinglass.com 对高频请求会 ERR_CONNECTION_CLOSED 节流，
  // 立即连 reload 会越打越死，必须冷却后再试）
  const backoff = [0, 30000, 60000]; // 首次不睡，之后 30s / 60s 冷却
  let tries = 0;
  while (rowCount === 0 && tries < backoff.length) {
    if (tries > 0) {
      console.error(`[retry ${tries}] 数据行 0，冷却 ${backoff[tries] / 1000}s 后 reload…`);
      await page.waitForTimeout(backoff[tries]);
    }
    await page.reload({ waitUntil: 'load', timeout: 60000 });
    rowCount = await waitStable();
    tries++;
  }

  if (rowCount === 0) {
    const diag = await page.evaluate(() => ({
      title: document.title,
      bodyLen: document.body.innerText.length,
      hasAntTable: !!document.querySelector('.ant-table'),
      bodyHead: document.body.innerText.slice(0, 400)
    }));
    console.error('FATAL: 数据行未渲染。HTTP=' + status, JSON.stringify(diag));
    await browser.close();
    process.exit(2);
  }

  // 解析：找到主表（含 Side 列）与告警表（含 From 列）
  const parsed = await page.evaluate(() => {
    const numAttr = (el) => {
      const n = el && el.querySelector('.Number');
      if (!n) return null;
      const c = String(n.getAttribute('aria-label')).replace(/[^0-9.\-]/g, '');
      return c === '' ? null : parseFloat(c);
    };
    const tables = Array.from(document.querySelectorAll('table'));
    let main = [], alert = [];
    for (const t of tables) {
      const head = Array.from(t.querySelectorAll('thead th')).map(th => th.innerText.trim());
      const isMain = head.includes('Side') && head.includes('Exchanges');
      const isAlert = head.includes('From') && head.includes('To');
      const rows = Array.from(t.querySelectorAll('tr[data-row-key]'));
      for (const r of rows) {
        const tds = r.querySelectorAll('td');
        const symEl = r.querySelector('.symbol-name');
        const symbol = symEl ? symEl.innerText.trim() : (tds[0] ? tds[0].innerText.replace(/\s+/g, ' ').trim() : '');
        if (isMain) {
          main.push({
            symbol,
            exchange: tds[1] ? tds[1].innerText.trim() : '',
            side: tds[2] ? tds[2].innerText.trim() : '',
            qty: numAttr(tds[3]),
            qty_display: tds[3] ? tds[3].innerText.trim() : '',
            value: numAttr(tds[4]),
            value_display: tds[4] ? tds[4].innerText.trim() : '',
            time: tds[5] ? tds[5].innerText.trim() : ''
          });
        } else if (isAlert) {
          alert.push({
            symbol,
            from: tds[1] ? tds[1].innerText.trim() : '',
            to: tds[2] ? tds[2].innerText.trim() : '',
            qty: numAttr(tds[3]),
            qty_display: tds[3] ? tds[3].innerText.trim() : '',
            time: tds[4] ? tds[4].innerText.trim() : ''
          });
        }
      }
    }
    return { main, alert };
  });

  await browser.close();

  // 聚合净流：按 (exchange, symbol)
  const agg = {};
  for (const r of parsed.main) {
    const s = (r.side || '').toLowerCase();
    if (s !== 'inflow' && s !== 'outflow') continue;
    const key = `${r.exchange}||${r.symbol}`;
    if (!agg[key]) agg[key] = { exchange: r.exchange, symbol: r.symbol, inflow_qty: 0, outflow_qty: 0, inflow_usd: 0, outflow_usd: 0, tx: 0 };
    agg[key].tx += 1;
    if (s === 'inflow') {
      agg[key].inflow_qty += (r.qty != null ? r.qty : 0);
      agg[key].inflow_usd += (r.value != null ? r.value : 0);
    } else {
      agg[key].outflow_qty += (r.qty != null ? r.qty : 0);
      agg[key].outflow_usd += (r.value != null ? r.value : 0);
    }
  }
  const netflow = Object.values(agg).map(a => ({
    exchange: a.exchange, symbol: a.symbol, tx: a.tx,
    inflow_qty: +a.inflow_qty.toFixed(4), outflow_qty: +a.outflow_qty.toFixed(4),
    net_qty: +(a.inflow_qty - a.outflow_qty).toFixed(4),
    inflow_usd: +a.inflow_usd.toFixed(2), outflow_usd: +a.outflow_usd.toFixed(2),
    net_usd: +(a.inflow_usd - a.outflow_usd).toFixed(2)
  })).sort((x, y) => y.net_usd - x.net_usd);

  const totalIn = parsed.main.filter(r => (r.side || '').toLowerCase() === 'inflow').reduce((s, r) => s + (r.value || 0), 0);
  const totalOut = parsed.main.filter(r => (r.side || '').toLowerCase() === 'outflow').reduce((s, r) => s + (r.value || 0), 0);

  const out = {
    fetched_at: new Date().toISOString(),
    source: URL,
    http_status: status,
    main_rows: parsed.main.length,
    alert_rows: parsed.alert.length,
    main_table: parsed.main,
    alert_history: parsed.alert,
    netflow_by_exchange_coin: netflow,
    summary: {
      total_inflow_usd: +totalIn.toFixed(2),
      total_outflow_usd: +totalOut.toFixed(2),
      net_usd: +(totalIn - totalOut).toFixed(2),
      distinct_coins: netflow.length
    }
  };

  const json = JSON.stringify(out, null, 2);
  if (!noWrite) {
    const fp = path.join(__dirname, 'cg_netflow_latest.json');
    fs.writeFileSync(fp, json, 'utf8');
    console.error('[written] ' + fp);
  }
  // stdout 仅输出 JSON，便于管道消费
  console.log(json);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
