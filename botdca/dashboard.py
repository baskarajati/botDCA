from __future__ import annotations

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>botDCA</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    body { margin:0; background:#0b0d10; color:#f3f4f6; }
    main { max-width:1100px; margin:0 auto; padding:32px 20px 64px; }
    header { display:flex; justify-content:space-between; gap:16px; align-items:center; margin-bottom:24px; }
    h1 { margin:0; font-size:24px; }
    .status { display:inline-flex; align-items:center; gap:8px; padding:7px 11px; border:1px solid #2a2f38; border-radius:999px; background:#12161c; }
    .dot { width:8px; height:8px; border-radius:50%; background:#7c8797; }
    .dot.running { background:#22c55e; }
    .dot.paused { background:#f59e0b; }
    .grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }
    .card { background:#12161c; border:1px solid #242a33; border-radius:14px; padding:16px; }
    .label { color:#8d98a8; font-size:12px; margin-bottom:8px; }
    .value { font-size:22px; font-weight:650; letter-spacing:-.02em; }
    .sub { color:#8d98a8; font-size:12px; margin-top:5px; }
    section { margin-top:18px; }
    table { width:100%; border-collapse:collapse; }
    td { padding:10px 0; border-bottom:1px solid #20262e; }
    td:first-child { color:#8d98a8; width:48%; }
    .controls { display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; }
    button { background:#202630; color:#f8fafc; border:1px solid #343c48; border-radius:10px; padding:10px 14px; font-weight:600; cursor:pointer; }
    button.danger { background:#3a171a; border-color:#7f1d1d; }
    button:disabled { opacity:.45; cursor:not-allowed; }
    .warning { color:#fbbf24; }
    .error { margin-top:12px; color:#f87171; white-space:pre-wrap; }
    @media (max-width:800px) { .grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    @media (max-width:480px) { .grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>botDCA</h1><div class="sub">Bybit perpetual DCA strategy control</div></div>
    <div class="status"><span id="dot" class="dot"></span><span id="state">loading</span></div>
  </header>

  <div class="grid">
    <div class="card"><div class="label">Symbol</div><div class="value" id="symbol">—</div><div class="sub" id="leverage">—</div></div>
    <div class="card"><div class="label">Average entry</div><div class="value" id="avg">—</div><div class="sub" id="qty">—</div></div>
    <div class="card"><div class="label">DCA level</div><div class="value" id="dca">—</div><div class="sub" id="next-dca">—</div></div>
    <div class="card"><div class="label">Take profit</div><div class="value" id="tp">—</div><div class="sub" id="live-mode">—</div></div>
  </div>

  <section class="card">
    <div class="label">Risk</div>
    <table>
      <tr><td>Committed margin</td><td id="margin">—</td></tr>
      <tr><td>Maximum strategy margin</td><td id="margin-max">—</td></tr>
      <tr><td>Projected after next DCA</td><td id="margin-projected">—</td></tr>
      <tr><td>Next DCA allowed</td><td id="dca-allowed">—</td></tr>
      <tr><td>Risk message</td><td id="risk-message">—</td></tr>
    </table>
  </section>

  <div class="controls">
    <button onclick="action('/api/v1/bot/resume')">Resume</button>
    <button onclick="action('/api/v1/bot/pause')">Pause</button>
    <button class="danger" onclick="manualClose()">Close position & pause</button>
  </div>
  <div id="error" class="error"></div>
</main>
<script>
const f=(v,d=4)=>v===null||v===undefined?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:d});
async function load(){
  try{
    const r=await fetch('/api/v1/bot/status',{cache:'no-store'}); if(!r.ok) throw new Error(await r.text());
    const s=await r.json();
    document.querySelector('#state').textContent=s.state;
    const dot=document.querySelector('#dot'); dot.className='dot '+(s.state==='paused'?'paused':'running');
    document.querySelector('#symbol').textContent=s.symbol;
    document.querySelector('#leverage').textContent=`${s.leverage}× leverage`;
    document.querySelector('#avg').textContent=f(s.average_entry);
    document.querySelector('#qty').textContent=`${f(s.position_qty)} position qty`;
    document.querySelector('#dca').textContent=`${s.dca_level} / ${s.max_dca_level}`;
    document.querySelector('#next-dca').textContent=`Next: ${f(s.next_dca_price)}`;
    document.querySelector('#tp').textContent=f(s.tp_price);
    document.querySelector('#live-mode').textContent=s.live_trading?'LIVE TRADING':'Dry run';
    document.querySelector('#margin').textContent=`${f(s.committed_margin_usdt)} USDT`;
    document.querySelector('#margin-max').textContent=`${f(s.max_strategy_margin_usdt)} USDT`;
    document.querySelector('#margin-projected').textContent=`${f(s.projected_margin_after_next_dca_usdt)} USDT`;
    document.querySelector('#dca-allowed').textContent=s.next_dca_allowed?'Yes':'No';
    const msg=document.querySelector('#risk-message'); msg.textContent=s.dca_blocked_reason||'Within configured limits';
    msg.className=s.next_dca_allowed?'':'warning';
    document.querySelector('#error').textContent='';
  }catch(e){ document.querySelector('#error').textContent=String(e); }
}
async function action(path){
  try{ const r=await fetch(path,{method:'POST'}); if(!r.ok) throw new Error(await r.text()); await load(); }
  catch(e){ document.querySelector('#error').textContent=String(e); }
}
async function manualClose(){
  if(!confirm('Close the current position and pause the bot?')) return;
  await action('/api/v1/bot/manual-close');
}
load(); setInterval(load,2000);
</script>
</body>
</html>
"""
