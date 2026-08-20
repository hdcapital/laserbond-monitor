"""Static HTML dashboard -> /docs (GitHub Pages).

Self-contained: no CDNs, no external assets. Charts are rendered
client-side by a small inline SVG renderer from JSON embedded in the page.
Missing series render a NO DATA card - never sample data.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pandas as pd

from ..config import REPO_ROOT
from ..store import read_events, read_observations

log = logging.getLogger("lbl_tracker.dashboard")

# Series charted individually; high-cardinality families are summarised.
FAMILY_PREFIXES = ("qld_coal.mine.", "jsa.ivi.", "cat.resource_industries_yoy_pct.",
                   "abs.exploration.metres_drilled_")
FAMILY_KEEP = {"abs.exploration.metres_drilled_total", "abs.exploration.metres_drilled_wa"}

SERIES_META = {
    "qld_coal.saleable_tonnes_total": ("QLD coal saleable production, total", "tonnes/quarter"),
    "pilbara.total_throughput_mt": ("Pilbara Ports total throughput", "Mt/month"),
    "pilbara.iron_ore_throughput_mt": ("Pilbara Ports iron ore (Port Hedland exports)", "Mt/month"),
    "abs.capex.mining_actual": ("ABS private new capex - mining, actual", "A$m/quarter"),
    "abs.capex.mining_expected": ("ABS private new capex - mining, expectations", "A$m/quarter"),
    "abs.exploration.metres_drilled_total": ("ABS mineral exploration metres drilled, Australia", "'000 m/quarter"),
    "abs.exploration.metres_drilled_wa": ("ABS mineral exploration metres drilled, WA", "'000 m/quarter"),
    "rba.commodity_index_aud": ("RBA index of commodity prices (AUD)", "index"),
    "rba.audusd": ("AUD/USD daily", "USD"),
    "rba.audusd_monthly": ("AUD/USD monthly average", "USD"),
    "aisi.capacity_utilisation_pct": ("AISI raw-steel capability utilisation", "%"),
    "aisi.raw_steel_production_kt": ("AISI weekly raw-steel production", "kt"),
    "fred.steel_new_orders": ("US new orders, iron & steel mills (Census M3)", "US$m/month"),
    "bh.rigcount_na_total": ("Baker Hughes rig count, North America", "rigs"),
    "bh.rigcount_us_total": ("Baker Hughes rig count, US", "rigs"),
    "bh.rigcount_canada_total": ("Baker Hughes rig count, Canada", "rigs"),
    "bh.rigcount_intl_total": ("Baker Hughes rig count, international", "rigs"),
    "cat.resource_industries_yoy_pct": ("CAT dealer retail sales, Resource Industries, world", "% YoY"),
    "jsa.ivi_trades_tightness": ("IVI vacancies: metal fitters/machinists + welders (AUST)", "vacancies"),
    "tungsten.flag_count": ("Tungsten policy news flags (proxy)", "flags/month"),
    "emeco.utilisation_pct": ("Emeco operating utilisation (from announcements)", "%"),
    "mitchell.avg_operating_rigs": ("Mitchell Services average operating rigs", "rigs"),
}

# Every series a pulse depends on must appear in the freshness table even
# before it has data.
EXPECTED_SERIES = list(SERIES_META)


def _series_payload(obs: pd.DataFrame) -> list[dict]:
    payload = []
    present = set(obs["series_id"].unique()) if len(obs) else set()
    ordered = [s for s in EXPECTED_SERIES] + sorted(
        s for s in present if s not in EXPECTED_SERIES
        and not any(s.startswith(p) for p in FAMILY_PREFIXES) or s in FAMILY_KEEP)
    seen = set()
    for sid in ordered:
        if sid in seen or (any(sid.startswith(p) for p in FAMILY_PREFIXES)
                           and sid not in FAMILY_KEEP):
            continue
        seen.add(sid)
        title, units = SERIES_META.get(sid, (sid, ""))
        sel = obs[obs["series_id"] == sid].sort_values("date") if sid in present else \
            pd.DataFrame(columns=obs.columns)
        points = [
            {"d": pd.Timestamp(r["date"]).date().isoformat(),
             "v": None if pd.isna(r["value"]) else float(r["value"])}
            for _, r in sel.iterrows()
        ]
        # cap payload for very long daily series: keep last ~15 years
        if len(points) > 6000:
            points = points[-6000:]
        src = sel["source_url"].iloc[-1] if len(sel) else None
        last_val = sel["value"].dropna()
        payload.append({
            "id": sid, "title": title, "units": units, "points": points,
            "source_url": src,
            "last_date": points[-1]["d"] if points else None,
            "last_value": float(last_val.iloc[-1]) if len(last_val) else None,
            "n": len(points),
            "retrieved_at": (pd.Timestamp(sel["retrieved_at"].max()).isoformat()
                             if len(sel) else None),
        })
    return payload


def _freshness(obs: pd.DataFrame, now: pd.Timestamp) -> list[dict]:
    rows = []
    present = obs.groupby("series_id") if len(obs) else []
    by_series = {sid: g for sid, g in present}
    for sid in EXPECTED_SERIES:
        g = by_series.get(sid)
        if g is None or g["value"].dropna().empty:
            rows.append({"series": sid, "label": SERIES_META.get(sid, (sid,))[0],
                         "last_date": None, "last_value": None, "rows": 0,
                         "status": "NO DATA", "source_url": None})
            continue
        gv = g.dropna(subset=["value"]).sort_values("date")
        last = gv.iloc[-1]
        dates = pd.to_datetime(gv["date"]).sort_values()
        gap_days = float(dates.diff().dt.days.dropna().median()) if len(dates) > 3 else 35.0
        age = (now - pd.Timestamp(last["date"])).days
        status = "OK" if age <= max(2.5 * gap_days, 45) else "STALE"
        rows.append({"series": sid, "label": SERIES_META.get(sid, (sid,))[0],
                     "last_date": pd.Timestamp(last["date"]).date().isoformat(),
                     "last_value": float(last["value"]), "rows": int(len(g)),
                     "status": status, "source_url": str(last["source_url"])})
    return rows


def build() -> str:
    obs = read_observations()
    now = pd.Timestamp(datetime.now(timezone.utc).date())

    pulses_path = REPO_ROOT / "docs" / "data" / "pulses.json"
    pulses = json.loads(pulses_path.read_text()) if pulses_path.exists() else \
        {"pulses": {}, "attribution": {}, "technology": {"available": False}}
    backtest_path = REPO_ROOT / "docs" / "data" / "backtest.json"
    backtest = json.loads(backtest_path.read_text()) if backtest_path.exists() else None

    flags = read_events("tungsten_flags")
    recent_flags = []
    if len(flags):
        flags = flags.sort_values("published", ascending=False).head(12)
        recent_flags = [{"published": pd.Timestamp(r["published"]).date().isoformat(),
                         "title": r["title"], "url": r["url"], "keyword": r["keyword"]}
                        for _, r in flags.iterrows()]

    announcements = read_events("announcements")
    recent_ann = []
    if len(announcements):
        announcements = announcements.sort_values("date", ascending=False).head(15)
        recent_ann = [{"ticker": r["ticker"], "date": str(r["date"])[:10],
                       "headline": r["headline"], "url": r["url"]}
                      for _, r in announcements.iterrows()]

    payload = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "series": _series_payload(obs),
        "freshness": _freshness(obs, now),
        "pulses": pulses,
        "backtest": backtest,
        "tungsten_flags": recent_flags,
        "announcements": recent_ann,
    }

    docs = REPO_ROOT / "docs"
    (docs / "data").mkdir(parents=True, exist_ok=True)
    (docs / "data" / "dashboard.json").write_text(json.dumps(payload, default=str))
    html = TEMPLATE.replace("/*__PAYLOAD__*/null",
                            json.dumps(payload, default=str))
    (docs / "index.html").write_text(html)
    (docs / ".nojekyll").write_text("")
    log.info("dashboard built: %d series, %d freshness rows",
             len(payload["series"]), len(payload["freshness"]))
    return str(docs / "index.html")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LBL Tracker</title>
<style>
:root{
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --div-pos:#2a78d6; --div-neg:#e34948; --div-mid:#f0efec;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
  --good-text:#006300;
}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])){
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70;
    --div-pos:#3987e5; --div-neg:#e66767; --div-mid:#383835;
    --good-text:#0ca30c;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --surface-1:#1a1a19; --page:#0d0d0d;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --div-pos:#3987e5; --div-neg:#e66767; --div-mid:#383835;
  --good-text:#0ca30c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 64px}
header h1{font-size:22px;margin:0 0 2px}
header p{margin:0;color:var(--ink-2)}
h2{font-size:17px;margin:36px 0 12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.card{background:var(--surface-1);border:1px solid var(--ring);border-radius:10px;
  padding:14px 16px}
.card h3{margin:0 0 4px;font-size:14px;font-weight:600}
.card .sub{color:var(--muted);font-size:12px}
.hero{font-size:44px;font-weight:600;line-height:1.1;margin:6px 0 2px}
.nodata{color:var(--muted);font-weight:600;font-size:20px;padding:14px 0}
.meter{position:relative;height:10px;border-radius:5px;background:var(--div-mid);
  margin:10px 0 4px;overflow:hidden}
.meter .fill{position:absolute;top:0;bottom:0}
.meter .mid{position:absolute;top:-2px;bottom:-2px;left:50%;width:1px;background:var(--axis)}
.axislbl{display:flex;justify-content:space-between;color:var(--muted);font-size:11px}
table{border-collapse:collapse;width:100%;font-size:13px;background:var(--surface-1);
  border:1px solid var(--ring);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-top:1px solid var(--grid);
  font-variant-numeric:tabular-nums}
thead th{border-top:none;color:var(--ink-2);font-weight:600;font-size:12px}
td.num,th.num{text-align:right}
.badge{display:inline-block;font-size:11px;font-weight:600;padding:1px 8px;
  border-radius:9px;border:1px solid var(--ring)}
.badge.ok{color:var(--good-text)} .badge.ok::before{content:"✓ "}
.badge.stale{color:var(--serious)} .badge.stale::before{content:"⚠ "}
.badge.nodata{color:var(--muted)} .badge.nodata::before{content:"∅ "}
.chartgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:14px}
.chart-card svg{display:block;width:100%;height:auto}
.tt{position:fixed;pointer-events:none;background:var(--surface-1);color:var(--ink);
  border:1px solid var(--ring);border-radius:6px;padding:5px 9px;font-size:12px;
  box-shadow:0 2px 8px rgba(0,0,0,.18);display:none;z-index:9;white-space:nowrap}
.attr{margin:8px 0 0;padding:0;list-style:none;font-size:12px}
.attr li{display:flex;justify-content:space-between;gap:8px;padding:2px 0;
  border-top:1px solid var(--grid);color:var(--ink-2)}
.attr li span:last-child{font-variant-numeric:tabular-nums;color:var(--ink)}
.small{font-size:12px;color:var(--ink-2)}
a{color:var(--s1);text-decoration:none} a:hover{text-decoration:underline}
.pill{font-size:11px;color:var(--muted)}
.scroll{overflow-x:auto}
footer{margin-top:48px;color:var(--muted);font-size:12px;border-top:1px solid var(--grid);
  padding-top:12px}
.stage-row{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.stage{flex:1;min-width:72px;text-align:center;background:var(--page);
  border:1px solid var(--ring);border-radius:8px;padding:6px 4px}
.stage b{font-size:18px;display:block}
.stage .d{font-size:11px;color:var(--muted)}
</style></head>
<body>
<div class="wrap">
<header>
  <h1>LBL Tracker</h1>
  <p>External-data nowcast for LaserBond (ASX:LBL) &middot; built <span id="built"></span></p>
</header>
<h2>Pulses <span class="pill">(&minus;100 &hellip; +100, 5-yr rolling z-score composites &mdash; weights in config.yaml)</span></h2>
<div class="cards" id="pulseCards"></div>
<h2>Technology pipeline <span class="pill">(facts from classified LBL announcements &mdash; never scored)</span></h2>
<div class="cards" id="techCard"></div>
<h2>Data freshness</h2>
<div class="scroll" id="freshness"></div>
<h2>Series</h2>
<div class="chartgrid" id="charts"></div>
<h2>Latest tungsten policy flags <span class="pill">(keyword proxy &mdash; no free spot price exists)</span></h2>
<div class="scroll" id="flags"></div>
<h2>Latest announcements</h2>
<div class="scroll" id="announcements"></div>
<div id="backtestWrap"></div>
<footer>
Data integrity: every point is stored with its source URL and retrieval time.
Nothing is estimated, interpolated or backfilled; missing series show NO DATA.
Composite method &amp; weights: see config.yaml and SOURCES.md in the repository.
</footer>
</div>
<div class="tt" id="tt"></div>
<script>
const DATA = /*__PAYLOAD__*/null;
const $ = (s)=>document.querySelector(s);
const esc = (s)=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = (v)=>{
  if(v===null||v===undefined||Number.isNaN(v)) return "—";
  const a=Math.abs(v);
  if(a>=1e9) return (v/1e9).toFixed(1)+"B";
  if(a>=1e6) return (v/1e6).toFixed(1)+"M";
  if(a>=1e4) return Math.round(v).toLocaleString();
  if(a>=100) return v.toFixed(1).replace(/\.0$/,"");
  return (Math.round(v*100)/100).toString();
};
$("#built").textContent = DATA.built_at;

/* ---- pulse cards ---- */
(function(){
  const host=$("#pulseCards");
  const names=Object.keys(DATA.pulses.pulses||{});
  if(!names.length){host.innerHTML='<div class="card"><div class="nodata">NO DATA</div></div>';return;}
  for(const name of names){
    const p=DATA.pulses.pulses[name];
    const attr=(DATA.pulses.attribution||{})[name]||[];
    const card=document.createElement("div");card.className="card";
    let body;
    if(p.latest_value===null||p.latest_value===undefined){
      body='<div class="nodata">NO DATA</div>';
    }else{
      const v=p.latest_value, pct=Math.min(Math.abs(v),100)/2;
      const side=v>=0?'left:50%':'right:50%';
      const col=v>=0?'var(--div-pos)':'var(--div-neg)';
      body=`<div class="hero">${v>0?"+":""}${v.toFixed(1)}</div>
        <div class="meter"><div class="fill" style="${side};width:${pct}%;background:${col}"></div><div class="mid"></div></div>
        <div class="axislbl"><span>&minus;100</span><span>0</span><span>+100</span></div>
        <div class="sub">as of ${esc(p.latest_month)}</div>`;
    }
    const rows=attr.map(a=>{
      if(a.status==="NO DATA")
        return `<li><span>${esc(a.label)}</span><span class="badge nodata">NO DATA</span></li>`;
      const c=a.contribution===null?"—":(a.contribution>0?"+":"")+a.contribution.toFixed(1);
      return `<li title="z=${a.z?.toFixed(2)} as of ${esc(a.as_of)}${a.inverted?" (inverted)":""}">
        <span>${esc(a.label)}${a.inverted?" ↓":""}</span><span>${c}</span></li>`;
    }).join("");
    card.innerHTML=`<h3>${esc(p.title)}</h3>${body}
      <ul class="attr">${rows}</ul>`;
    host.appendChild(card);
    if(p.history&&p.history.length>1){
      const svg=lineChart(p.history.map(h=>({d:h.month,v:h.value})),{units:"",height:110,diverging:true});
      card.appendChild(svg);
    }
  }
})();

/* ---- technology pipeline ---- */
(function(){
  const t=DATA.pulses.technology||{};
  const host=$("#techCard");
  const card=document.createElement("div");card.className="card";card.style.gridColumn="1/-1";
  if(!t.available){
    card.innerHTML=`<h3>Technology pipeline</h3><div class="nodata">NO DATA</div>
      <div class="small">${esc(t.note||"")}</div>`;
  }else{
    const stages=t.stages.map(s=>{
      const d=t.stage_deltas_6m[s];
      return `<div class="stage"><b>${t.stage_counts[s]}</b>${esc(s.replace("_"," "))}
        <div class="d">${d>0?"+":""}${d} / 6m</div></div>`;
    }).join("");
    const ev=(t.events||[]).slice(0,10).map(e=>
      `<tr><td>${esc(e.date)}</td><td>${esc(e.stage)}</td><td>${esc(e.counterparty||"—")}</td>
       <td class="num">${e.value_aud?fmt(e.value_aud):"—"}</td>
       <td><a href="${esc(e.source_pdf_url)}" rel="noopener">${esc(e.description||"")}</a></td></tr>`).join("");
    card.innerHTML=`<h3>Technology pipeline</h3>
      <div class="stage-row">${stages}</div>
      <div class="small">Contracted value (where stated): <b>${fmt(t.contracted_value_aud_where_stated)}</b>
       &middot; recognised: <b>${fmt(t.recognised_aud_where_stated)}</b>
       &middot; contracted-unrecognised: <b>${fmt(t.contracted_unrecognised_aud_where_stated)}</b></div>
      <table><thead><tr><th>Date</th><th>Stage</th><th>Counterparty</th>
        <th class="num">Value A$</th><th>Description</th></tr></thead>
        <tbody>${ev||'<tr><td colspan="5">No events yet</td></tr>'}</tbody></table>`;
  }
  host.appendChild(card);
})();

/* ---- freshness ---- */
(function(){
  const rows=DATA.freshness.map(f=>{
    const cls=f.status==="OK"?"ok":(f.status==="STALE"?"stale":"nodata");
    const src=f.source_url?`<a href="${esc(f.source_url)}" rel="noopener">source</a>`:"—";
    return `<tr><td>${esc(f.label)}<div class="pill">${esc(f.series)}</div></td>
      <td>${esc(f.last_date||"—")}</td><td class="num">${fmt(f.last_value)}</td>
      <td class="num">${f.rows.toLocaleString()}</td>
      <td><span class="badge ${cls}">${esc(f.status)}</span></td><td>${src}</td></tr>`;
  }).join("");
  $("#freshness").innerHTML=`<table><thead><tr><th>Series</th><th>Last obs</th>
    <th class="num">Last value</th><th class="num">Rows</th><th>Status</th><th>Link</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
})();

/* ---- svg line chart with crosshair tooltip ---- */
function lineChart(points,opts){
  opts=opts||{};
  const W=460,H=opts.height||180,m={t:12,r:14,b:22,l:46};
  const ns="http://www.w3.org/2000/svg";
  const svg=document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
  const pts=points.filter(p=>p.v!==null&&p.v!==undefined&&!Number.isNaN(p.v));
  if(pts.length<2){
    const t=document.createElementNS(ns,"text");
    t.setAttribute("x",W/2);t.setAttribute("y",H/2);t.setAttribute("text-anchor","middle");
    t.setAttribute("fill","var(--muted)");t.setAttribute("font-size","13");
    t.setAttribute("font-weight","600");
    t.textContent=pts.length===1?"1 datapoint — accumulating":"NO DATA";
    svg.appendChild(t);return svg;
  }
  const xs=pts.map(p=>new Date(p.d).getTime());
  const ys=pts.map(p=>p.v);
  let ymin=Math.min(...ys),ymax=Math.max(...ys);
  if(opts.diverging){ymax=Math.max(Math.abs(ymin),Math.abs(ymax),10);ymin=-ymax;}
  if(ymin===ymax){ymin-=1;ymax+=1;}
  const pad=(ymax-ymin)*.07;if(!opts.diverging){ymin-=pad;ymax+=pad;}
  const x0=Math.min(...xs),x1=Math.max(...xs);
  const X=t=>m.l+(t-x0)/(x1-x0)*(W-m.l-m.r);
  const Y=v=>H-m.b-(v-ymin)/(ymax-ymin)*(H-m.t-m.b);
  // gridlines + y ticks (clean values)
  const ticks=cleanTicks(ymin,ymax,4);
  for(const tv of ticks){
    const ln=document.createElementNS(ns,"line");
    ln.setAttribute("x1",m.l);ln.setAttribute("x2",W-m.r);
    ln.setAttribute("y1",Y(tv));ln.setAttribute("y2",Y(tv));
    ln.setAttribute("stroke","var(--grid)");ln.setAttribute("stroke-width","1");
    svg.appendChild(ln);
    const tx=document.createElementNS(ns,"text");
    tx.setAttribute("x",m.l-6);tx.setAttribute("y",Y(tv)+4);
    tx.setAttribute("text-anchor","end");tx.setAttribute("font-size","10");
    tx.setAttribute("fill","var(--muted)");tx.textContent=fmt(tv);
    svg.appendChild(tx);
  }
  // x ticks
  const nx=4;
  for(let i=0;i<=nx;i++){
    const t0=x0+(x1-x0)*i/nx,d=new Date(t0);
    const tx=document.createElementNS(ns,"text");
    tx.setAttribute("x",X(t0));tx.setAttribute("y",H-6);
    tx.setAttribute("text-anchor",i===0?"start":(i===nx?"end":"middle"));
    tx.setAttribute("font-size","10");tx.setAttribute("fill","var(--muted)");
    tx.textContent=d.getFullYear()+(((x1-x0)<5e10)?("-"+String(d.getMonth()+1).padStart(2,"0")):"");
    svg.appendChild(tx);
  }
  // area wash + path (break on gaps in the raw series)
  let d="",area="";
  const segs=[];let cur=[];
  for(const p of points){
    if(p.v===null||p.v===undefined||Number.isNaN(p.v)){if(cur.length)segs.push(cur),cur=[];}
    else cur.push(p);
  }
  if(cur.length)segs.push(cur);
  for(const seg of segs){
    if(seg.length<2)continue;
    let sd="";
    seg.forEach((p,i)=>{sd+=(i?"L":"M")+X(new Date(p.d).getTime()).toFixed(1)+" "+Y(p.v).toFixed(1)+" ";});
    d+=sd;
    const first=seg[0],last=seg[seg.length-1];
    area+=sd+`L${X(new Date(last.d).getTime()).toFixed(1)} ${Y(opts.diverging?0:ymin).toFixed(1)} L${X(new Date(first.d).getTime()).toFixed(1)} ${Y(opts.diverging?0:ymin).toFixed(1)} Z `;
  }
  const ap=document.createElementNS(ns,"path");
  ap.setAttribute("d",area);ap.setAttribute("fill","var(--s1)");ap.setAttribute("opacity","0.1");
  svg.appendChild(ap);
  if(opts.diverging){
    const zl=document.createElementNS(ns,"line");
    zl.setAttribute("x1",m.l);zl.setAttribute("x2",W-m.r);
    zl.setAttribute("y1",Y(0));zl.setAttribute("y2",Y(0));
    zl.setAttribute("stroke","var(--axis)");zl.setAttribute("stroke-width","1");
    svg.appendChild(zl);
  }
  const path=document.createElementNS(ns,"path");
  path.setAttribute("d",d);path.setAttribute("fill","none");
  path.setAttribute("stroke","var(--s1)");path.setAttribute("stroke-width","2");
  path.setAttribute("stroke-linejoin","round");path.setAttribute("stroke-linecap","round");
  svg.appendChild(path);
  // end marker with surface ring
  const lastP=pts[pts.length-1];
  const dot=document.createElementNS(ns,"circle");
  dot.setAttribute("cx",X(new Date(lastP.d).getTime()));dot.setAttribute("cy",Y(lastP.v));
  dot.setAttribute("r","4");dot.setAttribute("fill","var(--s1)");
  dot.setAttribute("stroke","var(--surface-1)");dot.setAttribute("stroke-width","2");
  svg.appendChild(dot);
  // crosshair + tooltip
  const cross=document.createElementNS(ns,"line");
  cross.setAttribute("y1",m.t);cross.setAttribute("y2",H-m.b);
  cross.setAttribute("stroke","var(--axis)");cross.setAttribute("stroke-width","1");
  cross.style.display="none";svg.appendChild(cross);
  const hdot=document.createElementNS(ns,"circle");
  hdot.setAttribute("r","4");hdot.setAttribute("fill","var(--s1)");
  hdot.setAttribute("stroke","var(--surface-1)");hdot.setAttribute("stroke-width","2");
  hdot.style.display="none";svg.appendChild(hdot);
  const tt=$("#tt");
  svg.addEventListener("mousemove",ev=>{
    const r=svg.getBoundingClientRect();
    const px=(ev.clientX-r.left)/r.width*W;
    if(px<m.l||px>W-m.r){cross.style.display="none";hdot.style.display="none";tt.style.display="none";return;}
    const t0=x0+(px-m.l)/(W-m.l-m.r)*(x1-x0);
    let best=0,bd=1/0;
    xs.forEach((t,i)=>{const dd=Math.abs(t-t0);if(dd<bd){bd=dd;best=i;}});
    const p=pts[best],cx=X(xs[best]),cy=Y(p.v);
    cross.setAttribute("x1",cx);cross.setAttribute("x2",cx);cross.style.display="";
    hdot.setAttribute("cx",cx);hdot.setAttribute("cy",cy);hdot.style.display="";
    tt.style.display="block";
    tt.innerHTML=`<b>${fmt(p.v)}</b>${opts.units?" "+esc(opts.units):""}<br>${esc(p.d)}`;
    tt.style.left=Math.min(ev.clientX+14,window.innerWidth-160)+"px";
    tt.style.top=(ev.clientY+14)+"px";
  });
  svg.addEventListener("mouseleave",()=>{cross.style.display="none";hdot.style.display="none";tt.style.display="none";});
  return svg;
}
function cleanTicks(lo,hi,n){
  const span=hi-lo,step0=span/n,mag=Math.pow(10,Math.floor(Math.log10(step0)));
  const step=[1,2,2.5,5,10].map(k=>k*mag).find(s=>span/s<=n+1)||mag*10;
  const out=[];for(let v=Math.ceil(lo/step)*step;v<=hi+1e-9;v+=step)out.push(v);
  return out;
}

/* ---- series charts ---- */
(function(){
  const host=$("#charts");
  for(const s of DATA.series){
    const card=document.createElement("div");card.className="card chart-card";
    const last=s.last_value===null?'<span class="badge nodata">NO DATA</span>'
      :`<b>${fmt(s.last_value)}</b> <span class="pill">${esc(s.units)} · ${esc(s.last_date)}</span>`;
    card.innerHTML=`<h3>${esc(s.title)}</h3>
      <div class="sub">${esc(s.id)} &middot; ${s.n.toLocaleString()} obs &middot; ${last}</div>`;
    card.appendChild(lineChart(s.points,{units:s.units}));
    if(s.source_url)card.insertAdjacentHTML("beforeend",
      `<div class="small"><a href="${esc(s.source_url)}" rel="noopener">latest source</a></div>`);
    host.appendChild(card);
  }
})();

/* ---- flags + announcements ---- */
(function(){
  const f=DATA.tungsten_flags;
  $("#flags").innerHTML=f.length?`<table><thead><tr><th>Published</th><th>Keyword</th><th>Headline</th></tr></thead><tbody>${
    f.map(x=>`<tr><td>${esc(x.published)}</td><td>${esc(x.keyword)}</td>
      <td><a href="${esc(x.url)}" rel="noopener">${esc(x.title)}</a></td></tr>`).join("")
  }</tbody></table>`:'<div class="card"><div class="nodata">NO DATA</div></div>';
  const a=DATA.announcements;
  $("#announcements").innerHTML=a.length?`<table><thead><tr><th>Date</th><th>Ticker</th><th>Headline</th></tr></thead><tbody>${
    a.map(x=>`<tr><td>${esc(x.date)}</td><td>${esc(x.ticker)}</td>
      <td><a href="${esc(x.url)}" rel="noopener">${esc(x.headline)}</a></td></tr>`).join("")
  }</tbody></table>`:'<div class="card"><div class="nodata">NO DATA</div></div>';
})();

/* ---- backtest ---- */
(function(){
  const b=DATA.backtest,host=$("#backtestWrap");
  if(!b)return;
  if(!b.available){
    host.innerHTML=`<h2>Backtest</h2><div class="card"><div class="nodata">NOT POPULATED</div>
      <div class="small">${esc(b.note||"")}</div></div>`;return;
  }
  const rows=b.results.map(r=>`<tr><td>${esc(r.pulse)}</td><td>${esc(r.segment)}</td>
    <td class="num">${r.lag}</td><td class="num">${r.n}</td>
    <td class="num">${r.spearman??"—"}</td><td class="num">${r.ridge_loo_mae??"—"}</td>
    <td class="num">${r.naive_seasonal_mae??"—"}</td>
    <td>${r.beats_naive===true?'<span class="badge ok">beats naive</span>'
      :(r.beats_naive===false?'<span class="badge stale">worse</span>':"—")}</td></tr>`).join("");
  host.innerHTML=`<h2>Backtest <span class="pill">(vs naive seasonal base case, ${b.halves} halves)</span></h2>
    <div class="scroll"><table><thead><tr><th>Pulse</th><th>Segment</th><th class="num">Lag (m)</th>
    <th class="num">n</th><th class="num">Spearman</th><th class="num">Ridge LOO MAE</th>
    <th class="num">Naive MAE</th><th>Verdict</th></tr></thead><tbody>${rows}</tbody></table></div>`;
})();
</script>
</body></html>
"""
