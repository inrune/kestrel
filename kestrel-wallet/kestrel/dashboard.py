"""
Kestrel node dashboard — a single, dependency-free HTML page served by the
node at ``GET /`` to web browsers. API clients (anything that does not send
``Accept: text/html``) still receive the JSON welcome, so the page is purely
additive: it is a *view* built on the very same open endpoints anyone can use.

Everything the page needs — layout, style, icons, logic — is inlined below so
a node works with no internet, no CDN and no build step. The page is static;
all live data is fetched client-side from this node's own JSON API.
"""

from . import params

# The page is served verbatim; two tiny placeholders are filled in at request
# time so the footer can show this node's version and network without any
# templating engine.
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Kestrel node</title>
<style>
  :root{
    --dusk:#1B212C; --dusk2:#222A38; --dusk3:#2A3444; --rail:#141924; --spot:#10141B;
    --buff:#EAE1CE; --muted:#A79F8D; --faint:#736C5E;
    --rufous:#C4552A; --rufous-hi:#DB6636;
    --green:#5FA46A; --red:#C15B4B; --slate:#8CA7C4; --amber:#D9A441;
    --grid:#202836; --line:#2C3646;
    --mono:"SFMono-Regular",ui-monospace,"Consolas","Cascadia Code","DejaVu Sans Mono",monospace;
    --sans:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    background:
      radial-gradient(1200px 600px at 80% -10%, #232c3b 0%, transparent 60%),
      radial-gradient(900px 500px at -10% 0%, #20303a 0%, transparent 55%),
      var(--dusk);
    color:var(--buff); font-family:var(--sans);
    font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased;
    min-height:100vh;
  }
  a{color:var(--slate); text-decoration:none}
  a:hover{color:var(--buff)}
  .wrap{max-width:1120px; margin:0 auto; padding:22px 20px 60px}

  /* ---------------------------------------------------------- header */
  header{display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:20px}
  .brand{display:flex; align-items:center; gap:11px}
  .mark{width:38px; height:38px; flex:0 0 auto; filter:drop-shadow(0 2px 6px rgba(0,0,0,.4))}
  .brand h1{font-size:21px; margin:0; font-weight:700; letter-spacing:.2px}
  .brand h1 span{color:var(--rufous-hi)}
  .brand .tag{font-size:11.5px; color:var(--faint); margin-top:1px; letter-spacing:.3px}
  .spacer{flex:1 1 auto}
  .pill{display:inline-flex; align-items:center; gap:7px; padding:6px 12px;
    background:var(--dusk2); border:1px solid var(--line); border-radius:999px;
    font-size:12px; color:var(--muted)}
  .pill b{color:var(--buff); font-weight:600}
  .dot{width:8px; height:8px; border-radius:50%; background:var(--faint)}
  .dot.live{background:var(--green); box-shadow:0 0 0 0 rgba(95,164,106,.6); animation:pulse 2.4s infinite}
  .dot.warn{background:var(--amber)}
  .dot.bad{background:var(--red)}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(95,164,106,.5)}70%{box-shadow:0 0 0 7px rgba(95,164,106,0)}100%{box-shadow:0 0 0 0 rgba(95,164,106,0)}}

  /* ---------------------------------------------------------- search */
  .search{position:relative; margin-bottom:20px}
  .search svg{position:absolute; left:14px; top:50%; transform:translateY(-50%); opacity:.5}
  .search input{
    width:100%; padding:13px 16px 13px 42px; font-size:14.5px; color:var(--buff);
    background:var(--spot); border:1px solid var(--line); border-radius:12px;
    font-family:var(--sans); outline:none; transition:border-color .15s, box-shadow .15s}
  .search input::placeholder{color:var(--faint)}
  .search input:focus{border-color:var(--rufous); box-shadow:0 0 0 3px rgba(196,85,42,.15)}

  /* ------------------------------------------------------- stat grid */
  .stats{display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px}
  .stat{background:var(--dusk2); border:1px solid var(--line); border-radius:14px; padding:14px 16px; position:relative; overflow:hidden}
  .stat .k{font-size:11.5px; color:var(--muted); letter-spacing:.4px; text-transform:uppercase; display:flex; align-items:center; gap:6px}
  .stat .v{font-size:23px; font-weight:700; margin-top:6px; font-variant-numeric:tabular-nums; letter-spacing:-.3px}
  .stat .v small{font-size:13px; color:var(--muted); font-weight:600}
  .stat .s{font-size:12px; color:var(--faint); margin-top:3px; font-variant-numeric:tabular-nums}
  .stat.accent::before{content:""; position:absolute; inset:0 auto 0 0; width:3px; background:linear-gradient(var(--rufous),var(--rufous-hi))}
  .bar{height:5px; border-radius:3px; background:var(--rail); margin-top:9px; overflow:hidden}
  .bar > i{display:block; height:100%; background:linear-gradient(90deg,var(--rufous),var(--rufous-hi)); border-radius:3px; width:0; transition:width .6s ease}

  /* ---------------------------------------------------------- panels */
  .cols{display:grid; grid-template-columns:1.55fr 1fr; gap:16px; align-items:start}
  .panel{background:var(--dusk2); border:1px solid var(--line); border-radius:14px; overflow:hidden}
  .panel h2{font-size:13px; margin:0; padding:13px 16px; border-bottom:1px solid var(--grid);
    color:var(--muted); font-weight:600; letter-spacing:.4px; text-transform:uppercase;
    display:flex; align-items:center; justify-content:space-between}
  .panel h2 .n{color:var(--faint); font-weight:500; text-transform:none; letter-spacing:0}

  table{width:100%; border-collapse:collapse; font-size:13px}
  th{ text-align:left; color:var(--faint); font-weight:600; font-size:11px; text-transform:uppercase;
    letter-spacing:.4px; padding:9px 16px; border-bottom:1px solid var(--grid)}
  td{padding:10px 16px; border-bottom:1px solid var(--grid); font-variant-numeric:tabular-nums}
  tr:last-child td{border-bottom:none}
  tbody tr{cursor:pointer; transition:background .1s}
  tbody tr:hover{background:var(--dusk3)}
  .mono{font-family:var(--mono); font-size:12.5px}
  .r{text-align:right}
  .hl{color:var(--rufous-hi); font-weight:600}
  .g{color:var(--green)} .muted{color:var(--muted)} .faint{color:var(--faint)}
  .badge{display:inline-block; padding:1px 7px; border-radius:6px; font-size:11px; font-weight:600;
    background:var(--rail); color:var(--slate)}
  .empty{padding:26px 16px; text-align:center; color:var(--faint); font-size:13px}

  .peer{display:flex; align-items:center; gap:9px; padding:9px 16px; border-bottom:1px solid var(--grid); font-size:12.5px}
  .peer:last-child{border-bottom:none}
  .peer .u{flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:var(--mono); font-size:12px}
  .rich{padding:6px 16px 10px}
  .rrow{padding:7px 0; border-bottom:1px solid var(--grid)}
  .rrow:last-child{border-bottom:none}
  .rich .top{display:flex; justify-content:space-between; gap:10px; font-size:12.5px}
  .rich .addr{font-family:var(--mono); font-size:11.5px; color:var(--slate); overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .minibar{height:4px; border-radius:2px; background:var(--rail); margin-top:5px; overflow:hidden}
  .minibar > i{display:block; height:100%; background:var(--slate); opacity:.7}

  /* ---------------------------------------------------------- drawer */
  .scrim{position:fixed; inset:0; background:rgba(9,12,17,.6); backdrop-filter:blur(2px);
    opacity:0; pointer-events:none; transition:opacity .2s; z-index:40}
  .scrim.on{opacity:1; pointer-events:auto}
  .drawer{position:fixed; top:0; right:0; height:100%; width:min(560px,94vw); background:var(--dusk);
    border-left:1px solid var(--line); box-shadow:-20px 0 60px rgba(0,0,0,.5);
    transform:translateX(100%); transition:transform .24s cubic-bezier(.4,0,.2,1); z-index:41;
    display:flex; flex-direction:column}
  .drawer.on{transform:none}
  .drawer .dh{display:flex; align-items:center; gap:10px; padding:16px 18px; border-bottom:1px solid var(--grid)}
  .drawer .dh .t{font-weight:700; font-size:15px}
  .drawer .dh .close{margin-left:auto; cursor:pointer; color:var(--muted); background:none; border:0; font-size:22px; line-height:1; padding:2px 6px; border-radius:8px}
  .drawer .dh .close:hover{background:var(--dusk3); color:var(--buff)}
  .drawer .db{overflow-y:auto; padding:16px 18px}
  .kv{display:grid; grid-template-columns:120px 1fr; gap:7px 14px; font-size:13px; margin-bottom:8px}
  .kv dt{color:var(--faint)}
  .kv dd{margin:0; word-break:break-all; font-variant-numeric:tabular-nums}
  .kv dd.mono{font-family:var(--mono); font-size:12px}
  .subh{font-size:11.5px; text-transform:uppercase; letter-spacing:.4px; color:var(--muted); margin:16px 0 8px; font-weight:600}
  .io{background:var(--dusk2); border:1px solid var(--line); border-radius:10px; padding:9px 12px; margin-bottom:7px; font-size:12px}
  .io .a{font-family:var(--mono); font-size:11.5px; color:var(--slate); word-break:break-all}

  /* ---------------------------------------------------------- footer */
  footer{margin-top:26px; padding-top:18px; border-top:1px solid var(--grid); color:var(--faint); font-size:12px;
    display:flex; flex-wrap:wrap; gap:8px 20px; align-items:center}
  footer code{font-family:var(--mono); color:var(--muted)}
  .params{display:flex; flex-wrap:wrap; gap:6px 8px; margin-top:14px}
  .params span{background:var(--dusk2); border:1px solid var(--line); border-radius:8px; padding:4px 9px; font-size:11.5px; color:var(--muted)}
  .params b{color:var(--buff); font-weight:600}
  .btn{cursor:pointer; background:var(--dusk3); border:1px solid var(--line); color:var(--buff);
    border-radius:9px; padding:6px 12px; font-size:12.5px; font-family:var(--sans)}
  .btn:hover{border-color:var(--rufous)}

  @media(max-width:880px){ .stats{grid-template-columns:repeat(2,1fr)} .cols{grid-template-columns:1fr} }
  @media(max-width:480px){ .hide-s{display:none} }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <svg class="mark" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <path d="M24 3l6 12 12 4-9 6 2 14-11-7-11 7 2-14-9-6 12-4z" fill="#C4552A"/>
        <path d="M24 3l6 12 12 4-9 6-9-22z" fill="#DB6636"/>
        <circle cx="24" cy="21" r="3.4" fill="#1B212C"/>
      </svg>
      <div>
        <h1>Kestrel<span>.</span> node</h1>
        <div class="tag">fast, light, decentralized money</div>
      </div>
    </div>
    <div class="spacer"></div>
    <span class="pill"><span id="live" class="dot"></span><span id="netname">kestrel</span></span>
    <span class="pill" id="syncpill"><span id="syncdot" class="dot"></span><span id="synctxt">connecting…</span></span>
    <button class="btn" id="refresh" title="Refresh now">Refresh</button>
  </header>

  <div class="search">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#A79F8D" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
    <input id="q" placeholder="Search a block height, block hash, transaction id or K-address…" autocomplete="off" spellcheck="false">
  </div>

  <div class="stats" id="stats"></div>

  <div class="cols">
    <div class="panel">
      <h2>Recent blocks <span class="n" id="tiph"></span></h2>
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Height</th><th>Age</th><th class="hide-s">Txs</th><th class="hide-s">Miner</th><th class="r">Reward</th></tr></thead>
        <tbody id="blocks"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
      </table>
      </div>
    </div>
    <div style="display:grid; gap:16px">
      <div class="panel">
        <h2>Peers <span class="n" id="peern"></span></h2>
        <div id="peers"><div class="empty">Loading…</div></div>
      </div>
      <div class="panel">
        <h2>Richest addresses</h2>
        <div class="rich" id="rich"><div class="empty">Loading…</div></div>
      </div>
    </div>
  </div>

  <div class="params" id="params"></div>

  <footer>
    <span>Kestrel <code id="ver">__VER__</code></span>
    <span>network <code id="magic">__MAGIC__</code></span>
    <span>node <code id="nodeid">—</code></span>
    <span class="spacer" style="flex:1"></span>
    <a href="/info">JSON API →</a>
  </footer>
</div>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" aria-hidden="true">
  <div class="dh"><span class="t" id="dtitle">Detail</span><button class="close" id="dclose">&times;</button></div>
  <div class="db" id="dbody"></div>
</aside>

<script>
const $ = s => document.querySelector(s);
const COIN = 100000000;
async function api(p){ const r = await fetch(p,{cache:"no-store"}); if(!r.ok) throw new Error(r.status+" "+p); return r.json(); }
function commas(n){ return String(n).replace(/\B(?=(\d{3})+(?!\d))/g,","); }
function ksl(f){ f=Number(f); const w=Math.floor(f/COIN); let out=commas(w);
  const fr=f-w*COIN; if(fr>0){ let s=(fr/COIN).toFixed(8).slice(2).replace(/0+$/,""); if(s) out+="."+s; } return out; }
function ago(ts){ let s=Math.max(0,Date.now()/1000-ts);
  if(s<45)return Math.floor(s)+"s"; if(s<3600)return Math.floor(s/60)+"m";
  if(s<86400)return Math.floor(s/3600)+"h"; return Math.floor(s/86400)+"d"; }
function shortMid(h,k){ k=k||8; return h && h.length>2*k+3 ? h.slice(0,k)+"…"+h.slice(-k) : h; }
function hashrate(diff,bt){ if(!bt||bt<=0)bt=120; const hs=diff*4096/bt;
  const u=["H/s","KH/s","MH/s","GH/s","TH/s","PH/s"]; let i=0,v=hs;
  while(v>=1000&&i<u.length-1){v/=1000;i++;} return v.toFixed(v<10?2:v<100?1:0)+" "+u[i]; }
function dur(sec){ sec=Math.round(sec); const d=Math.floor(sec/86400); if(d>=365){const y=(d/365).toFixed(1);return y+"y";}
  if(d>=1)return d+"d"; const h=Math.floor(sec/3600); if(h>=1)return h+"h";
  const m=Math.floor(sec/60); if(m>=1)return m+"m"; return sec+"s"; }
function esc(s){ return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

let firstOK=false, target=120;
function card(k,v,s,opts){ opts=opts||{};
  return `<div class="stat${opts.accent?" accent":""}"><div class="k">${k}</div>`+
    `<div class="v">${v}</div>`+(s?`<div class="s">${s}</div>`:"")+
    (opts.bar!=null?`<div class="bar"><i style="width:${Math.max(0,Math.min(100,opts.bar))}%"></i></div>`:"")+`</div>`;
}

async function tick(){
  try{
    const [info,sup] = await Promise.all([api("/info"),api("/supply")]);
    firstOK=true; target=sup.target_block_time||120;
    $("#live").className="dot live";
    $("#netname").textContent=info.network||"kestrel";
    $("#magic").textContent=info.magic||"—";
    $("#nodeid").textContent=(info.node_id||"—").slice(0,10);
    // sync state
    const best=Math.max(info.best_height||0, info.height||0);
    const behind=best-(info.height||0);
    const sd=$("#syncdot"), st=$("#synctxt");
    if(behind<=0){ sd.className="dot live"; st.textContent="synced · h "+commas(info.height); }
    else { sd.className="dot warn"; st.textContent="syncing · "+commas(behind)+" behind"; }

    const pct=sup.pct_mined||0;
    const netreach = info.reachable===true?"reachable":info.reachable===false?"behind NAT":"local";
    const stats=[
      card("Height", commas(info.height), "tip "+shortMid(info.tip,6), {accent:true}),
      card("Circulating", ksl(sup.circulating)+" <small>KSL</small>",
           pct.toFixed(pct<0.01?4:2)+"% of "+commas(sup.max_supply/COIN), {bar: pct}),
      card("Difficulty", (info.difficulty||0).toFixed(info.difficulty<100?3:0),
           "~"+hashrate(info.difficulty||1,target)+" network"),
      card("Block reward", sup.block_reward_ksl.replace(" KSL"," <small>KSL</small>"),
           "halving in "+commas(sup.blocks_to_halving)+" blk · "+dur((sup.blocks_to_halving||0)*target)),
      card("Peers", commas((info.peers||[]).length), (sup.peers_alive||0)+" alive · "+netreach),
      card("Mempool", commas(info.mempool||0), (info.mempool?"pending":"no pending")+" tx"),
      card("Avg block", sup.avg_block_time?dur(sup.avg_block_time):"—", "target "+target+"s"),
      card("Total work", commas(info.total_work||0), "cumulative"),
    ];
    $("#stats").innerHTML=stats.join("");

    // params chips (static-ish, refreshed cheaply)
    $("#params").innerHTML=[
      ["proof of work","scrypt"],["block time",target+"s"],
      ["halving","every "+commas(sup.halving_interval)+" blk"],
      ["supply cap",commas(sup.max_supply/COIN)+" KSL"],
      ["reward now",sup.block_reward_ksl],["mempool tx",commas(info.mempool||0)],
    ].map(p=>`<span><b>${p[1]}</b> ${p[0]}</span>`).join("");
  }catch(e){
    $("#live").className="dot bad";
    $("#syncdot").className="dot bad"; $("#synctxt").textContent="node unreachable";
    if(!firstOK) $("#stats").innerHTML=`<div class="stat" style="grid-column:1/-1"><div class="empty">Waiting for the node…</div></div>`;
  }
}

async function loadBlocks(){
  try{
    const d=await api("/latest?n=12");
    $("#tiph").textContent="height "+commas(d.height);
    const rows=(d.blocks||[]).map(b=>`<tr onclick="openDetail('block',${b.height})">`+
      `<td class="hl mono">${commas(b.height)}</td>`+
      `<td class="muted">${ago(b.timestamp)}</td>`+
      `<td class="hide-s"><span class="badge">${b.tx_count}</span></td>`+
      `<td class="hide-s mono muted">${shortMid(b.miner,7)}</td>`+
      `<td class="r g">${b.reward_ksl.replace(" KSL","")}</td></tr>`).join("");
    $("#blocks").innerHTML=rows||`<tr><td colspan="5" class="empty">No blocks yet</td></tr>`;
  }catch(e){ /* keep previous */ }
}
async function loadPeers(){
  try{
    const d=await api("/peers"); const alive=new Set(d.alive||[]);
    const list=(d.peers||[]);
    $("#peern").textContent=list.length?commas(list.length):"";
    $("#peers").innerHTML=list.length? list.map(u=>{
      const on=alive.has(u);
      return `<div class="peer"><span class="dot ${on?"live":"bad"}"></span><span class="u">${esc(u.replace(/^https?:\/\//,""))}</span>`+
             `<span class="faint">${on?"alive":"—"}</span></div>`;
    }).join("") : `<div class="empty">No peers yet — discovery in progress</div>`;
  }catch(e){ $("#peers").innerHTML=`<div class="empty">—</div>`; }
}
async function loadRich(){
  try{
    const d=await api("/richlist?n=8"); const rl=d.richlist||[];
    const max=rl.length?rl[0].pct:100;
    $("#rich").innerHTML=rl.length? rl.map(r=>`<div class="rrow">`+
      `<div class="top"><span class="addr" title="${esc(r.address)}" onclick="openDetail('address','${esc(r.address)}')" style="cursor:pointer">${shortMid(r.address,10)}</span>`+
      `<span class="mono">${ksl(r.amount)} <span class="faint">KSL</span></span></div>`+
      `<div class="minibar"><i style="width:${Math.max(2,r.pct/max*100)}%"></i></div></div>`).join("")
      : `<div class="empty">No balances yet</div>`;
  }catch(e){ $("#rich").innerHTML=`<div class="empty">—</div>`; }
}

/* ------------------------------------------------------------ drawer */
function openDrawer(){ $("#scrim").classList.add("on"); $("#drawer").classList.add("on"); $("#drawer").setAttribute("aria-hidden","false"); }
function closeDrawer(){ $("#scrim").classList.remove("on"); $("#drawer").classList.remove("on"); $("#drawer").setAttribute("aria-hidden","true"); }
$("#dclose").onclick=closeDrawer; $("#scrim").onclick=closeDrawer;
document.addEventListener("keydown",e=>{ if(e.key==="Escape")closeDrawer(); });

function row(k,v,mono){ return `<dt>${k}</dt><dd class="${mono?"mono":""}">${v}</dd>`; }
async function openDetail(kind,id){
  openDrawer(); $("#dbody").innerHTML=`<div class="empty">Loading…</div>`;
  try{
    if(kind==="block"){
      const b=await api("/block/"+id);
      $("#dtitle").textContent="Block "+commas(b.height);
      let h=`<dl class="kv">`+row("Height",commas(b.height))+row("Time",new Date(b.timestamp*1000).toLocaleString()+" · "+ago(b.timestamp)+" ago")+
        row("Block id",shortMid(b.block_id,12),1)+row("Prev",shortMid(b.prev_hash,12),1)+
        row("Merkle",shortMid(b.merkle_root,12),1)+row("PoW hash",shortMid(b.pow_hash,12),1)+
        row("Difficulty",(b.difficulty||0).toFixed(3))+row("Nonce",commas(b.nonce))+
        row("Size",commas(b.size)+" B")+row("Confirmations",commas(b.confirmations))+
        row("Reward",`<span class="g">${b.reward_ksl}</span>`)+row("Miner",shortMid(b.miner,12),1)+`</dl>`;
      h+=`<div class="subh">${b.transactions.length} transaction${b.transactions.length!=1?"s":""}</div>`;
      h+=b.transactions.map(t=>`<div class="io"><div class="a" style="cursor:pointer" onclick="openDetail('tx','${t.txid}')">${shortMid(t.txid,12)}</div>`+
         `<div class="muted" style="margin-top:4px">${t.is_coinbase?'<span class="badge">coinbase</span> ':""}${ksl(t.amount_out)} KSL · ${t.inputs.length} in / ${t.outputs.length} out</div></div>`).join("");
      $("#dbody").innerHTML=h;
    } else if(kind==="tx"){
      const t=await api("/tx/"+id);
      $("#dtitle").textContent="Transaction";
      let h=`<dl class="kv">`+row("Txid",shortMid(t.txid,14),1)+
        row("Status",`<span class="badge">${t.status||"—"}</span>`)+
        (t.block_height!=null?row("Block",commas(t.block_height)):"")+
        row("Confirmations",commas(t.confirmations||0))+row("Size",commas(t.size)+" B")+
        row("Total out",`<span class="g">${ksl(t.amount_out)} KSL</span>`)+`</dl>`;
      h+=`<div class="subh">Inputs (${t.inputs.length})</div>`+
        (t.is_coinbase?`<div class="io muted">Coinbase — newly minted coins</div>`:
         t.inputs.map(i=>`<div class="io"><div class="a">${shortMid((i.txid||"")+":"+ (i.vout!=null?i.vout:""),14)}</div>${i.address?`<div class="muted" style="margin-top:3px">${shortMid(i.address,14)}</div>`:""}</div>`).join(""));
      h+=`<div class="subh">Outputs (${t.outputs.length})</div>`+
        t.outputs.map(o=>`<div class="io"><div class="a" style="cursor:pointer" onclick="openDetail('address','${esc(o.address)}')">${shortMid(o.address,14)}</div>`+
        `<div class="g" style="margin-top:3px">${ksl(o.amount)} KSL</div></div>`).join("");
      $("#dbody").innerHTML=h;
    } else if(kind==="address"){
      const a=await api("/address/"+id);
      $("#dtitle").textContent="Address";
      let h=`<dl class="kv">`+row("Address",esc(a.address),1)+
        row("Confirmed",`<span class="g">${a.confirmed_ksl}</span>`)+
        row("Spendable",a.spendable_ksl)+row("Received",a.received_ksl)+
        row("Sent",a.sent_ksl)+row("Transactions",commas(a.tx_count))+
        row("UTXOs",commas((a.utxos||[]).length))+`</dl>`;
      if(a.history&&a.history.length){
        h+=`<div class="subh">History</div>`+a.history.slice(0,25).map(x=>`<div class="io"><div class="a" style="cursor:pointer" onclick="openDetail('tx','${x.txid}')">${shortMid(x.txid,12)}</div>`+
          `<div class="muted" style="margin-top:3px">${x.height!=null?"block "+commas(x.height):"pending"}${x.delta!=null?` · <span class="${x.delta>=0?"g":""}">${x.delta>=0?"+":""}${ksl(x.delta)} KSL</span>`:""}</div></div>`).join("");
      }
      $("#dbody").innerHTML=h;
    } else { $("#dbody").innerHTML=`<div class="empty">Nothing to show</div>`; }
  }catch(e){ $("#dbody").innerHTML=`<div class="empty">Not found</div>`; }
}
window.openDetail=openDetail;

/* ------------------------------------------------------------ search */
async function doSearch(){
  const q=$("#q").value.trim(); if(!q)return;
  try{
    const r=await api("/search/"+encodeURIComponent(q));
    if(r.type==="height"||r.type==="block") return openDetail("block", r.type==="height"?r.value:q);
    if(r.type==="tx") return openDetail("tx", r.value||q);
    if(r.type==="address") return openDetail("address", r.value||q);
    openDrawer(); $("#dtitle").textContent="Search"; $("#dbody").innerHTML=`<div class="empty">No match for “${esc(q)}”</div>`;
  }catch(e){ openDrawer(); $("#dtitle").textContent="Search"; $("#dbody").innerHTML=`<div class="empty">No match for “${esc(q)}”</div>`; }
}
$("#q").addEventListener("keydown",e=>{ if(e.key==="Enter")doSearch(); });
$("#refresh").onclick=()=>{ tick(); loadBlocks(); loadPeers(); loadRich(); };

/* ------------------------------------------------------- refresh loop */
let timer=null;
function loop(){ tick(); loadBlocks(); loadPeers(); loadRich(); }
function schedule(){ clearInterval(timer); timer=setInterval(()=>{ if(!document.hidden) loop(); }, 5000); }
document.addEventListener("visibilitychange",()=>{ if(!document.hidden) loop(); });
loop(); schedule();
</script>
</body>
</html>
"""


def page() -> bytes:
    """Return the dashboard HTML for this node, as UTF-8 bytes."""
    from . import __version__ as KVER  # lazy: avoids an import cycle at load
    html = (_HTML
            .replace("__VER__", KVER)
            .replace("__MAGIC__", params.NETWORK_MAGIC))
    return html.encode("utf-8")
