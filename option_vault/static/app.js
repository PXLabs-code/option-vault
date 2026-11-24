"use strict";
const $ = id => document.getElementById(id);
const names = {efficiency:"收租效率", history:"历史结果", tail:"尾部风险", quality:"报价质量"};
const state = {side:"P", page:1, weights:{efficiency:45,history:20,tail:20,quality:15}, overview:null, rows:[], detail:null, view:"ranking"};
const titles = {ranking:"策略排行榜",overview:"波动率概览",explore:"期限探索",watch:"关注合约",health:"采集与证据",method:"评分方法",detail:"合约研究"};
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const num = (value, digits=2) => value === null || value === undefined || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(digits);
const pct = value => value == null ? "—" : `${num(value*100,1)}%`;
function showError(error) { $("error").textContent=error.message || String(error); $("error").hidden=false; }
async function get(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
async function action(callback) {
  $("error").hidden=true;
  try { await callback(); } catch(error) { showError(error); }
}
function params() {
  return new URLSearchParams({strategy:state.side,symbol:$("symbol-filter").value,min_dte:$("min-dte").value,
    max_dte:$("max-dte").value,include_low:$("include-low").checked?"1":"0",page:state.page,snapshot:$("snapshot-run").value,...state.weights});
}
function chart(id, traces, layout={}) {
  const base = {paper_bgcolor:"transparent",plot_bgcolor:"transparent",font:{family:"-apple-system, sans-serif",color:"#6d7f89",size:11},
    margin:{l:55,r:20,t:20,b:55},xaxis:{gridcolor:"#edf1f3",zeroline:false},yaxis:{gridcolor:"#edf1f3",zeroline:false},
    legend:{orientation:"h",y:1.13},hovermode:"closest",...layout};
  Plotly.react($(id),traces,base,{responsive:true,displaylogo:false,modeBarButtonsToRemove:["lasso2d","select2d"]});
}
function emptyChart(id, text) {
  chart(id,[],{annotations:[{text,x:.5,y:.5,xref:"paper",yref:"paper",showarrow:false}],xaxis:{visible:false},yaxis:{visible:false}});
}
function view(name) {
  state.view=name;
  document.querySelectorAll(".view").forEach(el => {el.hidden=el.id!==name;});
  document.querySelectorAll(".nav").forEach(el => el.classList.toggle("active",el.dataset.view===name));
  $("page-title").textContent=titles[name];
  if(name==="explore") action(loadStrikes);
  if(name==="watch") action(loadWatch);
  if(name==="overview" && state.overview) drawIV();
  if(name==="ranking") action(loadRankings);
  requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
}
function drawWeights() {
  $("weight-controls").innerHTML=Object.entries(state.weights).map(([key,value]) =>
    `<label class="weight-control"><span class="weight-label">${names[key]}<output id="out-${key}">${value}</output></span><input aria-label="${names[key]}权重" data-weight="${key}" type="range" min="0" max="100" value="${value}"></label>`).join("");
  let timer;
  document.querySelectorAll("[data-weight]").forEach(input => input.addEventListener("input",() => {
    state.weights[input.dataset.weight]=Number(input.value); $(`out-${input.dataset.weight}`).value=input.value;
    const total=Object.values(state.weights).reduce((a,b)=>a+b,0);
    $("weight-message").textContent=total?`归一化权重：${Object.entries(state.weights).map(([k,v])=>`${names[k]} ${Math.round(v/total*100)}%`).join(" / ")}`:"至少设置一个正权重";
    clearTimeout(timer);
    if(total) timer=setTimeout(()=>{state.page=1;action(loadRankings);},250);
  }));
}
let rankingRequest=0;
async function loadRankings() {
  const token=++rankingRequest;
  $("candidate-count").innerHTML="…<small>查询中</small>";
  const data=await get(`/api/rankings?${params()}`);
  if(token!==rankingRequest) return;
  state.rows=data.rows;
  $("candidate-count").innerHTML=`${data.total.toLocaleString()}<small>当前筛选候选</small>`;
  $("result-count").textContent=`/ ${data.total} 份`;
  $("ranking-csv").href=`/api/export?kind=rankings&${params()}`;
  $("page-label").textContent=`${data.page} / ${Math.max(1,Math.ceil(data.total/50))}`;
  $("prev").disabled=data.page<=1; $("next").disabled=data.page*50>=data.total;
  $("ranking-empty").textContent=data.total?"":"当前没有符合条件的合约。可能报价陈旧、样本不足或筛选范围无记录；可勾选“显示低可信度”查看参考数据，不会补造候选。";
  $("ranking-body").innerHTML=data.rows.map((r,i) =>
    `<tr class="selectable" tabindex="0" data-row="${i}" aria-label="查看 ${esc(r.occ_symbol)}">
      <td><strong>${esc(r.symbol)}</strong><small>${esc(r.expiry)} · ${r.dte} DTE</small></td>
      <td>$${num(r.strike)}</td><td>${num(r.bid)} / <strong>${num(r.mid)}</strong> / ${num(r.ask)}</td>
      <td>${pct(r.efficiency)}</td><td>${pct(r.statistics.rate)}</td><td>${r.statistics.samples}</td>
      <td class="score">${num(r.score,1)}</td><td><span class="badge ${r.low_confidence?"warn":""}">${r.low_confidence?"低可信度":"满足门槛"}</span><small>${esc(r.collected_et?.slice(0,16))} ET · ${esc(r.source)}</small></td></tr>`).join("");
  document.querySelectorAll("[data-row]").forEach(el => {
    const open=()=>action(()=>openContract(data.rows[Number(el.dataset.row)].symbol,data.rows[Number(el.dataset.row)].occ_symbol));
    el.addEventListener("click",open); el.addEventListener("keydown",event=>{if(event.key==="Enter")open();});
  });
  const points=data.rows.filter(r=>r.efficiency!=null && r.statistics.rate!=null);
  if(!points.length){emptyChart("ranking-chart","暂无可绘制的有效报价与历史样本");return;}
  chart("ranking-chart",[{type:"scatter",mode:"markers",x:points.map(r=>r.efficiency*100),y:points.map(r=>r.statistics.rate*100),
    customdata:points.map(r=>[r.symbol,r.occ_symbol]),text:points.map(r=>`${r.symbol} ${r.expiry} $${r.strike}<br>比较分 ${num(r.score,1)} · ${r.statistics.samples}组样本`),
    hovertemplate:"%{text}<br>粗算年化 %{x:.1f}%<br>历史未进价内 %{y:.1f}%<extra></extra>",
    marker:{size:points.map(r=>10+(r.score||0)/6),color:points.map(r=>r.low_confidence?"#c9ad78":"#238f81"),opacity:.75,line:{width:1,color:"#fff"}}}],
    {xaxis:{title:"粗算年化（非实际回报率）%",gridcolor:"#edf1f3"},yaxis:{title:"历史到期未进入价内 %",gridcolor:"#edf1f3",range:[-3,103]}});
  $("ranking-chart").removeAllListeners?.("plotly_click");
  $("ranking-chart").on("plotly_click",event=>{const [symbol,code]=event.points[0].customdata;action(()=>openContract(symbol,code));});
}
function renderOverview(data) {
  state.overview=data;
  $("updated").textContent=`更新 ${new Date(data.generated_utc).toLocaleTimeString()}`;
  $("iv-cards").innerHTML=data.iv.map(r => `<article class="iv-card"><strong>${esc(r.symbol)}</strong><div class="iv-value">${pct(r.iv)}</div><p>${esc(r.day||"尚未记录")} · ${esc(r.source||"—")}</p><p>源时间 ${esc(r.source_timestamp||"未知")}</p><p>IV Rank ${r.rank==null?"历史不足":num(r.rank,1)} / Percentile ${r.percentile==null?"历史不足":num(r.percentile,1)}</p>${r.day!==new Date().toLocaleDateString("en-CA",{timeZone:"America/New_York"})?'<span class="badge warn">非今日收盘记录</span>':""}</article>`).join("");
  $("coverage-body").innerHTML=data.histories.map(r=>`<tr><td>${esc(r.symbol)}</td><td>${r.count}</td><td>${esc(r.first)}</td><td>${esc(r.last)}</td></tr>`).join("");
  $("runs-body").innerHTML=data.runs.map(r=>`<tr><td>${esc(r.day)}</td><td>${esc(r.label)}</td><td><span class="badge ${r.status==="ok"?"":r.status==="running"?"warn":"fail"}">${esc(r.status)}</span></td><td>${esc(r.started)}</td><td>${esc(r.error)}</td></tr>`).join("");
  $("method-config").textContent=JSON.stringify(data.scoring,null,2);
}
function drawIV() {
  const traces=state.overview.iv.filter(r=>r.history.length).map(r=>({name:r.symbol,type:"scatter",mode:"lines+markers",
    x:r.history.map(p=>p.day),y:r.history.map(p=>p.iv*100),connectgaps:false,hovertemplate:`${r.symbol}<br>%{x}<br>IV %{y:.2f}%<extra></extra>`}));
  if(traces.length)chart("iv-chart",traces,{yaxis:{title:"标准化 IV %",gridcolor:"#edf1f3"},xaxis:{type:"date",rangeslider:{visible:true,thickness:.12}}});
  else emptyChart("iv-chart","暂无收盘标准化 IV；不会用模型补造");
}
let strikeRequest=0;
async function loadStrikes() {
  const token=++strikeRequest;
  const symbol=$("explore-symbol").value, side=$("explore-side").value;
  const data=await get(`/api/strikes?${new URLSearchParams({symbol,strategy:side})}`);
  if(token!==strikeRequest)return;
  $("explore-strike").innerHTML=data.strikes.map(s=>`<option value="${s}">$${s}</option>`).join("");
  if(data.strikes.length)await loadExpiries(); else {$("expiry-notes").textContent="该标的暂无已保存的期限数据。";emptyChart("expiry-chart","该标的与策略没有已保存的候选");}
}
async function loadExpiries() {
  const data=await get(`/api/expiries?${new URLSearchParams({symbol:$("explore-symbol").value,strategy:$("explore-side").value,strike:$("explore-strike").value})}`);
  const rows=data.rows, valid=rows.filter(r=>r.mid!=null);
  const missing=(data.available_expiries||[]).filter(expiry=>!rows.some(r=>r.expiry===expiry));
  $("expiry-notes").textContent=`${rows.length} 个已记录期限，其中 ${valid.length} 个具有有效正报价。采集日期：${[...new Set(rows.map(r=>r.collected_et.slice(0,10)))].join(", ")||"无"}。该行权价缺失期限：${missing.join(", ")||"无已知缺口"}。未采到的期限仍无法确认。`;
  if(!rows.length){emptyChart("expiry-chart","没有该行权价的已保存期限");return;}
  chart("expiry-chart",[{name:"mid / Bid–Ask",type:"scatter",mode:"markers",x:rows.map(r=>r.expiry),y:rows.map(r=>r.mid),
    error_y:{type:"data",symmetric:false,array:rows.map(r=>r.mid==null?null:r.ask-r.mid),arrayminus:rows.map(r=>r.mid==null?null:r.mid-r.bid),color:"#9ac9c1"},
    customdata:rows.map(r=>[r.symbol,r.occ_symbol]),text:rows.map(r=>`${r.occ_symbol}<br>Bid ${num(r.bid)} / Ask ${num(r.ask)}<br>${r.low_confidence?"低可信度":"满足门槛"}`),
    marker:{color:"#087f72",size:9},hovertemplate:"%{text}<br>mid $%{y:.3f}<extra></extra>"}],
    {xaxis:{title:"到期日",type:"category"},yaxis:{title:"每股权利金（USD）",gridcolor:"#edf1f3"}});
  $("expiry-chart").removeAllListeners?.("plotly_click");
  $("expiry-chart").on("plotly_click",event=>{const [symbol,code]=event.points[0].customdata;action(()=>openContract(symbol,code));});
}
function quoteChart(id, rows, fields, percent=false) {
  if(!rows.length){emptyChart(id,"暂无该合约的真实采集记录");return;}
  const colors={bid:"#087f72",ask:"#649ada",mid:"#233d4d",last:"#b39468",iv:"#087f72"};
  chart(id,fields.map(field=>({name:field,type:"scatter",mode:"lines+markers",connectgaps:false,
    x:rows.map(r=>r.collected_utc),y:rows.map(r=>r[field]==null?null:r[field]*(percent?100:1)),
    line:{color:colors[field],width:field==="mid"?2.5:1.4,dash:field==="last"?"dot":"solid"},marker:{size:4}})),
    {xaxis:{type:"date",title:"采集时间（UTC）",rangeslider:{visible:true,thickness:.12}},yaxis:{title:percent?"IV %":fields.includes("bid")?"每股美元":"数据源原值",gridcolor:"#edf1f3"},hovermode:"x unified"});
}
async function loadWatch() {
  const watch=state.overview.watch;
  if(!watch){emptyChart("watch-chart","尚未配置关注合约");return;}
  $("watch-title").textContent=watch.occ_symbol;
  const query=new URLSearchParams({symbol:watch.symbol,code:watch.occ_symbol});
  const data=await get(`/api/contract?${query}`);
  const rows=data.watch.map(r=>({...r,mid:r.bid!=null&&r.ask!=null?(r.bid+r.ask)/2:null}));
  quoteChart("watch-chart",rows,["bid","ask","mid","last"]);
  $("watch-csv").href=`/api/export?kind=watch&${query}`;
  const last=rows.at(-1);
  $("watch-note").textContent=last?`最后采集 ${last.collected_utc} UTC · ${last.source} · 源时间 ${last.source_timestamp||"未知"}。Last 是最近成交，可能陈旧。`:"尚无关注合约记录。";
}
function riskTable(cases, title) {
  if(!cases?.length)return "";
  return `<h3 class="risk-heading">${esc(title)}</h3><div class="table-wrap"><table><thead><tr><th>起止日期</th><th>实际天数</th><th>股价变化</th><th>到期价相对行权价</th><th>到期未进价内</th></tr></thead><tbody>${cases.map(c=>`<tr><td>${c.start} → ${c.end}</td><td>${c.days}</td><td>${pct(c.return)}</td><td>${pct(c.strike_gap)}</td><td>${c.otm?"是":"否"}</td></tr>`).join("")}</tbody></table></div>`;
}
async function openContract(symbol,code) {
  const query=new URLSearchParams({symbol,code,snapshot:$("snapshot-run").value});
  const data=await get(`/api/contract?${query}`);
  state.detail={...data,symbol,code}; view("detail");
  $("detail-title").textContent=code;
  $("detail-csv").href=`/api/export?kind=contract&${query}`;
  const r=data.candidate;
  if(r) {
    const total=Object.values(state.weights).reduce((a,b)=>a+b,0);
    const score=Object.entries(state.weights).some(([k,w])=>w>0&&r.parts[k]==null)?null:Object.entries(state.weights).reduce((sum,[k,w])=>sum+(r.parts[k]||0)*w,0)/total;
    $("detail-summary").innerHTML=`<div class="metric-row">${[["比较分",num(score,1)],["Bid / mid / Ask",`${num(r.bid)} / ${num(r.mid)} / ${num(r.ask)}`],["粗算年化",pct(r.efficiency)],["报价时 / 当前 DTE",`${r.quote_dte} / ${r.current_dte}`]].map(([label,value])=>`<div class="metric"><small>${label}</small><strong>${value}</strong></div>`).join("")}</div><p class="muted">${esc(r.source)} · 采集 ${esc(r.collected_et)} ET · 源时间 ${esc(r.source_timestamp)} · ${r.low_confidence?"低可信度："+esc(r.reasons.join("；")):"满足实验性可信度门槛，不保证交易结果"}</p>`;
    $("score-parts").innerHTML=Object.entries(r.parts).map(([key,value])=>`<div class="part"><div class="part-label"><span>${names[key]} · 权重 ${state.weights[key]}</span><strong>${num(value,1)}</strong></div><div class="bar"><span style="width:${value==null?0:Math.max(0,Math.min(100,value))}%"></span></div></div>`).join("");
    $("trade-explanation").innerHTML=r.side==="P"?`<p>卖出 Put 收到权利金，同时承担被指派时按 $${num(r.strike)} 买入股票的义务。标准100股合约通常需按行权价×100接货，调整合约另论。</p><p>这笔空头期权的最大毛收益是实际成交权利金（未扣费用），不是当前 mid 的保证值。股票仍可大幅下跌至零；历史未进价内不等于持仓盈利或不会提前指派。</p>`:`<p>备兑 Call 以持有可交付股票为前提；被指派时可能需要按 $${num(r.strike)} 交付股票。页面不核实你的持仓，也不是裸卖 Call 建议。</p><p>空头期权腿的最大毛收益是实际收到的权利金，但整个备兑组合收益还取决于持股成本，本工具不计算。股票大跌仍有损失，大涨则可能错失行权价以上上涨；还可能提前指派。</p>`;
  } else {
    $("detail-summary").textContent="没有该合约的当前候选评分，仅展示已保存历史。";
    $("score-parts").textContent="暂无评分";
    $("trade-explanation").textContent="历史记录不是当前可成交报价。";
  }
  const stats=data.statistics;
  $("stats-summary").innerHTML=stats&&stats.samples?`<div class="metric-row"><div class="metric"><small>历史到期未进入价内</small><strong>${pct(stats.rate)}</strong></div><div class="metric"><small>不重叠样本</small><strong>${stats.samples} 组</strong></div><div class="metric"><small>Wilson 95%参考区间</small><strong>${pct(stats.interval[0])}–${pct(stats.interval[1])}</strong></div></div><p>覆盖 ${stats.first_day} 至 ${stats.last_day}。非重叠不代表独立，参考区间不是未来胜率区间；不作当前市场环境匹配。下列幅度不计权利金和费用。</p>`:"没有完整历史情景：可能期限太长、历史不足或缺失。不会硬算比例。";
  $("risk-cases").innerHTML=stats?riskTable(stats.worst,r?.side==="C"?"股票下跌最不利案例（非期权腿损益）":"跌破接货价最不利案例")+riskTable(stats.upside,"Call 错失上涨案例（单独展示）"):"";
  if(stats?.distribution?.length)chart("risk-chart",[{type:"histogram",x:stats.distribution.map(x=>x*100),marker:{color:"#83b9af"},name:"历史情景"}],{xaxis:{title:"历史到期股价相对映射行权价 %"},yaxis:{title:"样本数",gridcolor:"#edf1f3"}});
  else emptyChart("risk-chart","没有足够历史情景");
  drawHistory();
}
function drawHistory() {
  const metric=$("history-metric").value, rows=state.detail?.history||[];
  const fields={quotes:["bid","ask","mid","last"],iv:["iv"],greeks:["delta","gamma","theta","vega"],activity:["volume","oi"]};
  quoteChart("history-chart",rows,fields[metric],metric==="iv");
}
async function refresh() {
  const data=await get("/api/overview"); renderOverview(data);
  if(state.view==="ranking")await loadRankings();
  if(state.view==="overview")drawIV();
  if(state.view==="watch")await loadWatch();
  if(state.view==="explore")await loadExpiries();
  if(state.view==="detail"&&state.detail)await openContract(state.detail.symbol,state.detail.code);
}
document.querySelectorAll("[data-view]").forEach(button=>button.addEventListener("click",()=>view(button.dataset.view)));
document.querySelectorAll("[data-side]").forEach(button=>button.addEventListener("click",()=>{
  state.side=button.dataset.side;state.page=1;
  document.querySelectorAll("[data-side]").forEach(b=>b.classList.toggle("selected",b===button));
  action(loadRankings);
}));
$("apply").addEventListener("click",()=>{state.page=1;action(loadRankings);});
$("snapshot-day").addEventListener("change",()=>action(async()=>{
  const day=$("snapshot-day").value;
  if(!day){$("snapshot-run").innerHTML='<option value="">各标的最新</option>';return;}
  const data=await get(`/api/snapshots?${new URLSearchParams({day})}`);
  $("snapshot-run").innerHTML=data.runs.map(r=>`<option value="${esc(r.id)}">${esc(r.label)} · ${esc(r.status)}</option>`).join("");
  $("include-low").checked=true;
}));
$("prev").addEventListener("click",()=>{state.page--;action(loadRankings);});
$("next").addEventListener("click",()=>{state.page++;action(loadRankings);});
$("refresh").addEventListener("click",()=>action(refresh));
$("reset-weights").addEventListener("click",()=>{state.weights={...state.overview.scoring.weights};drawWeights();action(loadRankings);});
$("explore-symbol").addEventListener("change",()=>action(loadStrikes));
$("explore-side").addEventListener("change",()=>action(loadStrikes));
$("explore-strike").addEventListener("change",()=>action(loadExpiries));
$("history-metric").addEventListener("change",drawHistory);
$("back").addEventListener("click",()=>view("ranking"));
document.querySelectorAll('a[href="#method"]').forEach(a=>a.addEventListener("click",event=>{event.preventDefault();view("method");}));
action(async()=>{
  const data=await get("/api/overview"); renderOverview(data);
  const snapshots=await get("/api/snapshots");
  $("snapshot-day").insertAdjacentHTML("beforeend",snapshots.days.map(day=>`<option value="${day}">${day}</option>`).join(""));
  for(const id of ["symbol-filter","explore-symbol"]) {
    $(id).insertAdjacentHTML("beforeend",data.symbols.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join(""));
  }
  state.weights={...data.scoring.weights};drawWeights();await loadRankings();
});
setInterval(()=>{if(!document.hidden)action(refresh);},60000);
