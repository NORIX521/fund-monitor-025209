const DEFAULT_SETTINGS = { watchLow: 2.25, watchHigh: 2.35, recoveryLine: 2.50, peakLine: 3.0864 };
let dashboardData = null;
let settings = loadSettings();

const $ = (id) => document.getElementById(id);
const formatPct = (value, digits = 2) => value == null ? '—' : `${value > 0 ? '+' : ''}${Number(value).toFixed(digits)}%`;
const formatNav = (value) => value == null ? '—' : Number(value).toFixed(4);

function loadSettings() {
  try { return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem('fundMonitorSettings') || '{}') }; }
  catch { return { ...DEFAULT_SETTINGS }; }
}

async function loadData(force = false) {
  $('refreshButton').disabled = true;
  $('refreshButton').textContent = '读取中…';
  try {
    const response = await fetch(`data/dashboard.json${force ? `?t=${Date.now()}` : ''}`, { cache: force ? 'no-store' : 'default' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    dashboardData = await response.json();
    render();
    maybeNotify();
  } catch (error) {
    $('dataFreshness').textContent = '数据读取失败';
    console.error(error);
  } finally {
    $('refreshButton').disabled = false;
    $('refreshButton').textContent = '刷新数据';
  }
}

function render() {
  const fund = dashboardData.fund;
  const latest = fund.history.at(-1) || {};
  const high = settings.peakLine || fund.peak_nav;
  const drawdown = latest.nav ? (latest.nav / high - 1) * 100 : null;

  $('latestNav').textContent = formatNav(latest.nav);
  $('navDate').textContent = latest.date || '—';
  $('dailyChange').textContent = formatPct(latest.change_pct);
  setPill($('dailyChange'), latest.change_pct);
  $('dataFreshness').textContent = dashboardData.status?.data_mode === 'live' ? '自动更新' : '示例/缓存数据';
  $('drawdown').textContent = formatPct(drawdown);
  $('peakNav').textContent = `高点 ${formatNav(high)}`;
  $('drawdownBar').style.width = `${Math.min(Math.abs(drawdown || 0) / 35 * 100, 100)}%`;

  const zone = getZone(latest.nav, drawdown, fund.history);
  $('zoneName').textContent = zone.name;
  $('zoneText').textContent = zone.text;

  renderHoldings(dashboardData.holdings || []);
  renderStorage(dashboardData.storage || {});
  renderAlerts(calculateAlerts(latest.nav, drawdown, dashboardData));
  drawNavChart(fund.history || []);
  $('lastUpdated').textContent = `最后更新：${dashboardData.generated_at || '—'}`;
}

function getZone(nav, drawdown, history) {
  if (nav == null) return { name: '暂无数据', text: '等待下一次工作流更新。' };
  const lastThree = history.slice(-3).map(x => x.nav);
  const rising = lastThree.length === 3 && lastThree[0] < lastThree[1] && lastThree[1] < lastThree[2];
  if (nav < settings.watchLow) return { name: '跌破风险线', text: '净值低于观察区下沿，不能按“越跌越买”处理，需要先观察重仓股是否止跌。' };
  if (nav <= settings.watchHigh) return { name: '重点观察区', text: '风险收益比开始改善，但仍需重仓股广度和存储价格信号共同确认。' };
  if (nav >= settings.recoveryLine && rising) return { name: '连续修复', text: '净值站上修复线且连续回升，属于趋势修复信号，不等于重新进入主升。' };
  if (drawdown <= -25) return { name: '深度回撤', text: '距高点回撤超过25%，波动释放较多，但产业拐点仍决定后续方向。' };
  return { name: '中间震荡区', text: '既不是低估值底部，也未形成明确修复，重点看持仓共振与存储涨价斜率。' };
}

function renderHoldings(holdings) {
  const validChanges = holdings.map(x => x.change_pct).filter(v => Number.isFinite(v));
  const up = validChanges.filter(v => v > 0).length;
  const down = validChanges.filter(v => v < 0).length;
  const average = validChanges.length ? validChanges.reduce((a,b) => a+b,0) / validChanges.length : null;
  $('breadth').textContent = validChanges.length ? `${up}涨 ${down}跌` : '等待行情';
  $('breadthDetail').textContent = average == null ? '—' : `均值 ${formatPct(average)}`;
  $('holdingSignal').textContent = up >= 7 && average >= 2 ? '多数重仓股同步走强，形成板块级反弹共振。' : down >= 7 && average <= -2 ? '多数重仓股同步走弱，属于板块级风险释放。' : '重仓股走势分化，暂未形成明确共振。';
  $('holdingReportDate').textContent = dashboardData.holding_report_date ? `报告期 ${dashboardData.holding_report_date}` : '报告期未获取';

  $('holdingsBody').innerHTML = holdings.map((item, index) => {
    const cls = item.change_pct > 0 ? 'positive' : item.change_pct < 0 ? 'negative' : '';
    const status = item.change_pct >= 3 ? '强势' : item.change_pct <= -3 ? '弱势' : '震荡';
    return `<tr><td>${index + 1}</td><td class="name">${escapeHtml(item.name || '—')}</td><td>${escapeHtml(item.code || '—')}</td><td>${item.weight_pct == null ? '—' : `${Number(item.weight_pct).toFixed(2)}%`}</td><td class="stock-change ${cls}">${formatPct(item.change_pct)}</td><td><span class="pill ${cls || 'neutral'}">${status}</span></td></tr>`;
  }).join('');
}

function renderStorage(storage) {
  const dram = storage.dram_qoq_range || [];
  const nand = storage.nand_qoq_range || [];
  $('dramRange').textContent = dram.length === 2 ? `+${dram[0]}–${dram[1]}%` : '—';
  $('nandRange').textContent = nand.length === 2 ? `+${nand[0]}–${nand[1]}%` : '—';
  $('dramTrend').textContent = storage.dram_trend || '暂无判断';
  $('nandTrend').textContent = storage.nand_trend || '暂无判断';
  $('storageSummary').textContent = storage.summary || '尚未获取行业信号。';
  $('storageBadge').textContent = storage.signal || '待更新';
  setPillByLabel($('storageBadge'), storage.signal);
  $('storageSource').href = storage.source_url || '#';
  $('storageSource').style.display = storage.source_url ? 'inline-block' : 'none';
}

function calculateAlerts(nav, drawdown, data) {
  const alerts = [...(data.alerts || [])];
  if (nav != null && nav >= settings.watchLow && nav <= settings.watchHigh) alerts.unshift({ level: 'warning', title: '净值进入观察区', message: `当前净值 ${formatNav(nav)}，进入 ${settings.watchLow.toFixed(2)}–${settings.watchHigh.toFixed(2)} 元区间。`, date: data.generated_at });
  if (nav != null && nav < settings.watchLow) alerts.unshift({ level: 'danger', title: '净值跌破风险线', message: `当前净值 ${formatNav(nav)}，低于 ${settings.watchLow.toFixed(2)} 元。`, date: data.generated_at });
  if (drawdown != null && drawdown <= -30) alerts.unshift({ level: 'danger', title: '回撤达到30%', message: `相对阶段高点回撤 ${formatPct(drawdown)}。`, date: data.generated_at });
  else if (drawdown != null && drawdown <= -25) alerts.unshift({ level: 'warning', title: '回撤达到25%', message: `相对阶段高点回撤 ${formatPct(drawdown)}。`, date: data.generated_at });
  return dedupeAlerts(alerts).slice(0, 8);
}

function renderAlerts(alerts) {
  $('alertCount').textContent = alerts.length;
  setPill($('alertCount'), alerts.some(x => x.level === 'danger') ? -1 : alerts.length ? 1 : 0);
  $('alertsList').innerHTML = alerts.length ? alerts.map(alert => `<div class="alert-item"><div class="alert-top"><strong>${escapeHtml(alert.title)}</strong><span class="pill ${alert.level === 'danger' ? 'negative' : alert.level === 'warning' ? 'warning' : 'neutral'}">${alert.level === 'danger' ? '高风险' : alert.level === 'warning' ? '关注' : '信息'}</span></div><p>${escapeHtml(alert.message)}</p><time>${escapeHtml(alert.date || '')}</time></div>`).join('') : '<div class="empty-state">当前没有触发关键阈值。<br>这通常比“每天都发信号”更有价值。</div>';
}

function drawNavChart(history) {
  const canvas = $('navChart');
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  canvas.width = width * dpr; canvas.height = height * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  ctx.clearRect(0,0,width,height);
  if (!history.length) return;

  const points = history.slice(-90);
  const values = points.map(p => p.nav).filter(Number.isFinite);
  const guides = [settings.watchLow, settings.watchHigh, settings.recoveryLine];
  const min = Math.min(...values, ...guides) * .97;
  const max = Math.max(...values, settings.peakLine) * 1.02;
  const pad = { left: 46, right: 14, top: 14, bottom: 30 };
  const x = i => pad.left + i / Math.max(points.length - 1, 1) * (width - pad.left - pad.right);
  const y = v => pad.top + (max - v) / (max - min) * (height - pad.top - pad.bottom);

  ctx.font = '11px system-ui'; ctx.fillStyle = '#7f8896'; ctx.strokeStyle = 'rgba(255,255,255,.07)'; ctx.lineWidth = 1;
  for (let i=0;i<5;i++) { const val = min + (max-min) * i/4; const py = y(val); ctx.beginPath(); ctx.moveTo(pad.left,py); ctx.lineTo(width-pad.right,py); ctx.stroke(); ctx.fillText(val.toFixed(2), 4, py+4); }
  [settings.watchLow, settings.watchHigh, settings.recoveryLine].forEach((val, idx) => { ctx.save(); ctx.strokeStyle = idx < 2 ? 'rgba(255,212,121,.35)' : 'rgba(138,167,255,.35)'; ctx.setLineDash([5,5]); ctx.beginPath(); ctx.moveTo(pad.left,y(val)); ctx.lineTo(width-pad.right,y(val)); ctx.stroke(); ctx.restore(); });

  const gradient = ctx.createLinearGradient(0,pad.top,0,height-pad.bottom); gradient.addColorStop(0,'rgba(119,240,182,.30)'); gradient.addColorStop(1,'rgba(119,240,182,0)');
  ctx.beginPath(); points.forEach((p,i) => { const px=x(i), py=y(p.nav); i ? ctx.lineTo(px,py) : ctx.moveTo(px,py); }); ctx.lineTo(x(points.length-1),height-pad.bottom); ctx.lineTo(x(0),height-pad.bottom); ctx.closePath(); ctx.fillStyle=gradient; ctx.fill();
  ctx.beginPath(); points.forEach((p,i) => { const px=x(i), py=y(p.nav); i ? ctx.lineTo(px,py) : ctx.moveTo(px,py); }); ctx.strokeStyle='#77f0b6'; ctx.lineWidth=2.2; ctx.stroke();

  canvas.onmousemove = (event) => { const rect=canvas.getBoundingClientRect(); const mx=event.clientX-rect.left; const idx=Math.max(0,Math.min(points.length-1,Math.round((mx-pad.left)/(width-pad.left-pad.right)*(points.length-1)))); const p=points[idx]; const tip=$('chartTooltip'); tip.style.display='block'; tip.style.left=`${Math.min(x(idx)+10,width-150)}px`; tip.style.top=`${Math.max(y(p.nav)-48,5)}px`; tip.innerHTML=`<strong>${formatNav(p.nav)}</strong><br><span>${p.date} · ${formatPct(p.change_pct)}</span>`; };
  canvas.onmouseleave = () => $('chartTooltip').style.display='none';
}

function setPill(node, value) { node.className = `pill ${value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral'}`; }
function setPillByLabel(node, label='') { const text=String(label); node.className = `pill ${/转弱|下跌|风险/.test(text) ? 'negative' : /收敛|观察/.test(text) ? 'warning' : /强|上涨|紧缺/.test(text) ? 'positive' : 'neutral'}`; }
function dedupeAlerts(alerts) { const seen=new Set(); return alerts.filter(a => { const k=`${a.title}|${a.message}`; if(seen.has(k)) return false; seen.add(k); return true; }); }
function escapeHtml(value) { return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }

$('refreshButton').addEventListener('click', () => loadData(true));
$('notifyButton').addEventListener('click', async () => { if (!('Notification' in window)) return alert('当前浏览器不支持通知。'); const permission=await Notification.requestPermission(); $('notifyButton').textContent=permission==='granted'?'浏览器提醒已开启':'浏览器提醒未授权'; });
$('settingsButton').addEventListener('click', () => { $('watchLow').value=settings.watchLow; $('watchHigh').value=settings.watchHigh; $('recoveryLine').value=settings.recoveryLine; $('peakLine').value=settings.peakLine; $('settingsDialog').showModal(); });
$('saveSettings').addEventListener('click', (event) => { event.preventDefault(); settings={ watchLow:Number($('watchLow').value), watchHigh:Number($('watchHigh').value), recoveryLine:Number($('recoveryLine').value), peakLine:Number($('peakLine').value) }; localStorage.setItem('fundMonitorSettings',JSON.stringify(settings)); $('settingsDialog').close(); render(); });
window.addEventListener('resize', () => dashboardData && drawNavChart(dashboardData.fund.history));

function maybeNotify() {
  if (Notification.permission !== 'granted' || !dashboardData) return;
  const latest=dashboardData.fund.history.at(-1); const drawdown=(latest.nav/settings.peakLine-1)*100; const alerts=calculateAlerts(latest.nav,drawdown,dashboardData); if(!alerts.length) return;
  const key=`${dashboardData.generated_at}|${alerts[0].title}`; if(localStorage.getItem('lastNotified')===key) return;
  new Notification(`025209：${alerts[0].title}`, { body: alerts[0].message, icon: 'assets/icon.svg' }); localStorage.setItem('lastNotified',key);
}

if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(console.error);
loadData();
