'use strict';

// Dev: ln -s ../data/output frontend/data  then: cd frontend && python3 -m http.server 8080
const DATA_URL = loc => `/data/${loc}.json`;

const LOCATIONS = [
  { id: 'casa_campi',    label: 'Casa Campi' },
  { id: 'lavoro_cosimo', label: 'Lav. Cosimo' },
  { id: 'lavoro_madda',  label: 'Lav. Madda' },
  { id: 'casa_cesto',    label: 'Casa Cesto' },
];

const INDICATOR_META = {
  panni:    { label: 'Panni',     icon: '🧺' },
  motorino: { label: 'Motorino',  icon: '🛵' },
  gelata:   { label: 'Gelata',    icon: '❄️' },
  temporale:{ label: 'Temporale', icon: '⛈️' },
  nebbia:   { label: 'Nebbia',    icon: '🌫️' },
  bisenzio: { label: 'Bisenzio',  icon: '🌊' },
  aria:     { label: 'Aria',      icon: '💨' },
  annaffia: { label: 'Annaffia',  icon: '💧' },
  clima:    { label: 'Clima',     icon: '☀️' },
};

const VERDICT_CLASS = { verde: 'green', giallo: 'yellow', rosso: 'red' };

let currentData  = null;
let selectedDayIdx = 0;
let selectedModel  = 'guazza';

// ── URL routing ───────────────────────────────────────────────────────────────

function getActiveLoc() {
  const p = new URLSearchParams(location.search).get('loc');
  return LOCATIONS.some(l => l.id === p) ? p : 'casa_campi';
}

function navTo(locId) {
  history.pushState({}, '', `?loc=${locId}`);
  loadLocation(locId);
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

function renderTabs(activeLoc) {
  const nav = document.getElementById('tabs');
  nav.innerHTML = LOCATIONS.map(l =>
    `<button class="tab${l.id === activeLoc ? ' active' : ''}" data-loc="${l.id}">${l.label}</button>`
  ).join('');
  nav.querySelectorAll('.tab').forEach(btn =>
    btn.addEventListener('click', () => navTo(btn.dataset.loc))
  );
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function loadLocation(locId) {
  renderTabs(locId);
  selectedDayIdx = 0;
  selectedModel  = 'guazza';
  const app = document.getElementById('app');
  app.innerHTML = '<div class="loading">Caricamento…</div>';
  try {
    const r = await fetch(DATA_URL(locId));
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    currentData = await r.json();
    render(app, currentData);
  } catch (e) {
    app.innerHTML = `<div class="error">Errore: ${e.message}<small>Assicurati che il server stia servendo i JSON da /data/</small></div>`;
    currentData = null;
  }
}

// ── Formatting ────────────────────────────────────────────────────────────────

function fmtDate(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('it-IT', {
    weekday: 'short', day: 'numeric', month: 'short',
  });
}

function fmtDateShort(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('it-IT', { day: 'numeric', month: 'short' });
}

function fmtDayLabel(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const todayMid = new Date();
  todayMid.setHours(0, 0, 0, 0);
  const target = new Date(y, m - 1, d);
  const diff = Math.round((target - todayMid) / 86400000);
  if (diff === 0) return 'Oggi';
  if (diff === 1) return 'Domani';
  if (diff === 2) return 'Dopodomani';
  return target.toLocaleDateString('it-IT', { weekday: 'long' });
}

function fmtDayShort(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const todayMid = new Date();
  todayMid.setHours(0, 0, 0, 0);
  const target = new Date(y, m - 1, d);
  const diff = Math.round((target - todayMid) / 86400000);
  if (diff === 0) return 'Oggi';
  if (diff === 1) return 'Dom.';
  if (diff === 2) return 'Dopo.';
  const wd = target.toLocaleDateString('it-IT', { weekday: 'short' });
  return wd.charAt(0).toUpperCase() + wd.slice(1);
}

function fmtDateTime(iso) {
  return new Date(iso).toLocaleString('it-IT', {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

function fmtTemp(v)   { return v != null ? `${v.toFixed(1)}°` : '—'; }
function fmtPrecip(v) { return v != null ? `${v.toFixed(1)} mm` : '—'; }

function staleWarning(generatedAt) {
  const ageH = (Date.now() - new Date(generatedAt).getTime()) / 3600000;
  if (ageH < 6) return '';
  return ` <span class="stale-warn" title="Dati generati ${ageH.toFixed(0)}h fa">⚠️ dati vecchi</span>`;
}

// ── Coverage badge ────────────────────────────────────────────────────────────

function coverageBadge(cov) {
  if (!cov || Object.values(cov).every(v => v === null)) {
    return '<div class="coverage-badge warn">⚠️ Calibrazione in corso — copertura CI non ancora disponibile (primi 30gg di operatività)</div>';
  }
  const items = [
    ['Tmin CI80', cov.tmin_ci80], ['Tmin CI90', cov.tmin_ci90],
    ['Tmax CI80', cov.tmax_ci80], ['Tmax CI90', cov.tmax_ci90],
    ['Precip CI80', cov.precip_ci80], ['Precip CI90', cov.precip_ci90],
  ].filter(([, v]) => v !== null)
   .map(([k, v]) => `<span class="cov-item">${k}: <strong>${(v * 100).toFixed(0)}%</strong></span>`)
   .join('');
  return `<div class="coverage-badge ok">📊 Copertura empirica 30gg: ${items}</div>`;
}

// ── CI bar ────────────────────────────────────────────────────────────────────

function ciBar(fc, unit) {
  const { p50, ci80_lo, ci80_hi, ci90_lo, ci90_hi } = fc;
  const range = ci90_hi - ci90_lo;
  if (range <= 0) return `<div class="ci-detail">—</div>`;

  const pct     = v => Math.max(0, Math.min(100, ((v - ci90_lo) / range) * 100)).toFixed(1);
  const p80lo    = pct(ci80_lo);
  const p80width = (pct(ci80_hi) - pct(ci80_lo)).toFixed(1);
  const p50pos   = pct(p50);

  return `
    <div class="ci-bar-wrap">
      <div class="ci-bar-track">
        <div class="ci-range-80" style="left:${p80lo}%;width:${p80width}%"></div>
        <div class="ci-p50" style="left:${p50pos}%"></div>
      </div>
      <div class="ci-labels">
        <span>${ci90_lo.toFixed(1)}${unit}</span>
        <span>${ci90_hi.toFixed(1)}${unit}</span>
      </div>
    </div>
    <div class="ci-detail">
      p50 ${p50.toFixed(1)}${unit} &middot; CI80 [${ci80_lo.toFixed(1)}, ${ci80_hi.toFixed(1)}]${unit}
    </div>`;
}

// ── Current conditions card ───────────────────────────────────────────────────

function renderCurrentConditions(current) {
  if (!current || current.temp_c == null) return '';

  const ts   = new Date(current.ts).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  const hum  = current.humidity_pct  != null ? `${current.humidity_pct.toFixed(0)}%`          : '—';
  const wind = current.wind_speed_ms != null ? `${(current.wind_speed_ms * 3.6).toFixed(0)} km/h` : '—';
  const prec = current.precip_mm     != null ? `${current.precip_mm.toFixed(1)} mm`            : '—';

  return `
    <section class="current-card">
      <div class="current-title">Stazioni SIR <span class="current-ts">${ts}</span></div>
      <div class="current-values">
        <div class="current-item">
          <span class="current-icon">🌡</span>
          <span class="current-val">${current.temp_c.toFixed(1)}°</span>
          <span class="current-lbl">temp</span>
        </div>
        <div class="current-item">
          <span class="current-icon">💧</span>
          <span class="current-val">${hum}</span>
          <span class="current-lbl">umidità</span>
        </div>
        <div class="current-item">
          <span class="current-icon">💨</span>
          <span class="current-val">${wind}</span>
          <span class="current-lbl">vento</span>
        </div>
        <div class="current-item">
          <span class="current-icon">🌧</span>
          <span class="current-val">${prec}</span>
          <span class="current-lbl">precip</span>
        </div>
      </div>
    </section>`;
}

// ── Model switch ──────────────────────────────────────────────────────────────

function renderModelSwitch(data) {
  const models = [{ source: 'guazza', label: '★ Guazza ML' }];
  (data.nwp_models_hourly || []).forEach(m => models.push({ source: m.source, label: m.label }));
  const btns = models.map(m =>
    `<button class="model-switch-btn${m.source === selectedModel ? ' active' : ''}" data-src="${m.source}">${m.label}</button>`
  ).join('');
  return `<div class="model-switch">${btns}</div>`;
}

// ── Combined chart: assemble flat time-series ─────────────────────────────────

function buildChartPoints(data, model) {
  const points = [];

  if (model === 'guazza') {
    const todayMid = new Date();
    todayMid.setHours(0, 0, 0, 0);

    (data.today_hourly || []).forEach(h => {
      const ts = new Date(todayMid);
      ts.setHours(h.hour, 0, 0, 0);
      points.push({ ts, temp_c: h.temp_c, humidity_pct: h.humidity_pct,
                    precip_mm: h.precip_mm, precip_prob: h.precip_prob });
    });

    (data.days || []).forEach(day => {
      const [y, m, d] = day.target_date.split('-').map(Number);
      (day.hourly || []).forEach(h => {
        points.push({
          ts: new Date(y, m - 1, d, h.hour, 0, 0),
          temp_c: h.temp_c, humidity_pct: h.humidity_pct,
          precip_mm: h.precip_mm, precip_prob: h.precip_prob,
        });
      });
    });
  } else {
    const modelData = (data.nwp_models_hourly || []).find(m => m.source === model);
    (modelData?.data || []).forEach(pt => {
      points.push({
        ts: new Date(pt.ts),
        temp_c: pt.temp_c, humidity_pct: pt.humidity_pct,
        precip_mm: pt.precip_mm, precip_prob: null,
      });
    });
  }

  return points.sort((a, b) => a.ts - b.ts);
}

// ── Combined multi-day scrollable chart ───────────────────────────────────────

function niceYTicks(min, max) {
  const rawStep = (max - min) / 4;
  const step = rawStep <= 1 ? 1 : rawStep <= 2.5 ? 2 : rawStep <= 5 ? 5 : 10;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let t = start; t <= max && ticks.length < 7; t += step) ticks.push(t);
  return ticks;
}

function renderCombinedChart(data, model) {
  const points = buildChartPoints(data, model);

  if (points.length < 2) {
    return '<div class="no-hourly">Dati grafici non disponibili per il modello selezionato</div>';
  }

  const PX_PER_HOUR = 10;
  const H = 160;
  const padL = 36, padR = 44, padT = 14, padB = 24;
  const cH   = H - padT - padB;

  const t0       = points[0].ts.getTime();
  const tEnd     = points[points.length - 1].ts.getTime();
  const totalMs  = Math.max(1, tEnd - t0);
  const totalHrs = totalMs / 3600000;
  const cW = Math.max(280, totalHrs * PX_PER_HOUR);
  const W  = cW + padL + padR;

  const xPos = ts => padL + ((ts.getTime() - t0) / totalMs) * cW;

  const validTemps = points.filter(p => p.temp_c != null).map(p => p.temp_c);
  if (validTemps.length === 0) {
    return '<div class="no-hourly">Temperatura non disponibile</div>';
  }
  const minT = Math.floor(Math.min(...validTemps)) - 1;
  const maxT = Math.ceil(Math.max(...validTemps)) + 1;
  const yT   = t => padT + (1 - (t - minT) / (maxT - minT)) * cH;
  const yH   = h => padT + (1 - h / 100) * cH;

  const maxP      = Math.max(...points.map(p => p.precip_mm ?? 0), 0.5);
  const precipMaxH = cH * 0.28;
  const yPBase    = padT + cH;
  const barW      = Math.max(3, PX_PER_HOUR * 0.65);

  // Temperature line
  const tPts = points.filter(p => p.temp_c != null)
    .map(p => `${xPos(p.ts).toFixed(1)},${yT(p.temp_c).toFixed(1)}`);
  const tempPath = tPts.length > 1
    ? `<path d="M ${tPts.join(' L ')}" fill="none" stroke="#f97316" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`
    : '';

  // Humidity dashed line
  const hPts = points.filter(p => p.humidity_pct != null)
    .map(p => `${xPos(p.ts).toFixed(1)},${yH(p.humidity_pct).toFixed(1)}`);
  const humPath = hPts.length > 1
    ? `<path d="M ${hPts.join(' L ')}" fill="none" stroke="#3b82f6" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.7"/>`
    : '';

  // Precipitation bars
  const precipBars = points.map(p => {
    const prec = p.precip_mm ?? 0;
    if (prec < 0.05) return '';
    const bH      = (prec / maxP) * precipMaxH;
    const x       = xPos(p.ts) - barW / 2;
    const y       = yPBase - bH;
    const opacity = (0.3 + (p.precip_prob ?? 0.5) * 0.7).toFixed(2);
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${bH.toFixed(1)}" fill="#3b82f6" opacity="${opacity}" rx="1"/>`;
  }).join('');

  // Day transition ticks
  const dayTicks = []; const dayLabels = [];
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1].ts;
    const curr = points[i].ts;
    const sameDay = prev.getDate() === curr.getDate() &&
                    prev.getMonth() === curr.getMonth() &&
                    prev.getFullYear() === curr.getFullYear();
    if (!sameDay) {
      const x = xPos(curr).toFixed(1);
      const lbl = curr.toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric' });
      dayTicks.push(`<line x1="${x}" y1="${padT}" x2="${x}" y2="${padT + cH}" stroke="var(--border)" stroke-width="1" stroke-dasharray="3,3"/>`);
      dayLabels.push(`<text x="${(parseFloat(x) + 2).toFixed(1)}" y="${H - 5}" text-anchor="start" class="chart-label" style="font-size:8px">${lbl}</text>`);
    }
  }

  // Temperature Y-axis labels and grid
  const tempTicks = niceYTicks(minT, maxT);
  const tempLabels = tempTicks.map(t =>
    `<text x="${padL - 4}" y="${yT(t).toFixed(1)}" text-anchor="end" dominant-baseline="middle" class="chart-label">${t}°</text>`
  ).join('');
  const gridLines = tempTicks.map(t =>
    `<line x1="${padL}" y1="${yT(t).toFixed(1)}" x2="${W - padR}" y2="${yT(t).toFixed(1)}" class="chart-grid"/>`
  ).join('');

  const zeroLine = (minT < 0 && maxT > 0)
    ? `<line x1="${padL}" y1="${yT(0).toFixed(1)}" x2="${W - padR}" y2="${yT(0).toFixed(1)}" stroke="#ef4444" stroke-width="1" stroke-dasharray="4,2" opacity="0.6"/>`
    : '';

  // Humidity axis labels (right side)
  const humLabels = [0, 50, 100].map(h =>
    `<text x="${W - padR + 4}" y="${yH(h).toFixed(1)}" dominant-baseline="middle" class="chart-label" style="fill:#3b82f6">${h}%</text>`
  ).join('');

  const precipLabel = maxP > 0.5
    ? `<text x="${padL + 4}" y="${(yPBase - precipMaxH * 0.85).toFixed(1)}" class="chart-label" style="fill:#3b82f6;font-size:8px">${maxP.toFixed(1)}mm</text>`
    : '';

  return `
    <div class="combined-chart-wrap">
      <svg width="${W}" height="${H}" class="combined-chart" aria-hidden="true">
        ${gridLines}
        ${zeroLine}
        ${dayTicks.join('')}
        ${precipBars}
        ${humPath}
        ${tempPath}
        ${tempLabels}
        ${humLabels}
        ${precipLabel}
        ${dayLabels.join('')}
      </svg>
    </div>
    <div class="chart-legend">
      <span class="legend-temp">— Temperatura</span>
      <span class="legend-hum">‐ ‐ Umidità</span>
      <span class="legend-precip">▪ Precipitazioni</span>
    </div>`;
}

// ── Day cards strip ───────────────────────────────────────────────────────────

function renderDayCards(days, activeDayIdx) {
  const cards = days.map((day, idx) => {
    const { target_date, forecasts: fc, indicators } = day;
    const dots = Object.entries(indicators).map(([id, ind]) => {
      const meta = INDICATOR_META[id] ?? { label: id };
      const cls  = VERDICT_CLASS[ind.verdict] ?? 'unknown';
      return `<span class="dot dot-${cls}" title="${meta.label}: ${ind.verdict}"></span>`;
    }).join('');
    const hasRain = fc.precip_mm.p50 != null && fc.precip_mm.p50 >= 0.1;

    return `<div class="day-card${idx === activeDayIdx ? ' active' : ''}" data-idx="${idx}">
      <div class="day-card-label">${fmtDayShort(target_date)}</div>
      <div class="day-card-date">${fmtDateShort(target_date)}</div>
      <div class="day-card-temps">
        <span class="day-card-tmax">${fmtTemp(fc.tmax_c.p50)}</span>
        <span class="day-card-tmin">${fmtTemp(fc.tmin_c.p50)}</span>
      </div>
      ${hasRain ? `<div class="day-card-precip">${fmtPrecip(fc.precip_mm.p50)}</div>` : '<div class="day-card-precip"></div>'}
      <div class="day-card-dots">${dots}</div>
    </div>`;
  }).join('');

  return `<section class="day-strip-cards">${cards}</section>`;
}

// ── NWP model comparison table ────────────────────────────────────────────────

function renderNwpComparison(day) {
  const nwp = day.nwp_comparison;
  const fc  = day.forecasts;
  if (!nwp || nwp.length === 0) return '';

  const nwpRows = nwp.map(m => `
    <tr>
      <td class="model-name">${m.label}</td>
      <td>${m.tmin_c != null ? m.tmin_c.toFixed(1) + '°' : '—'}</td>
      <td>${m.tmax_c != null ? m.tmax_c.toFixed(1) + '°' : '—'}</td>
      <td>${m.precip_mm != null ? m.precip_mm.toFixed(1) + ' mm' : '—'}</td>
    </tr>`).join('');

  return `
    <div class="nwp-comparison">
      <h4>Confronto modelli</h4>
      <table class="model-table">
        <thead>
          <tr><th>Modello</th><th>Tmin</th><th>Tmax</th><th>Precip</th></tr>
        </thead>
        <tbody>
          ${nwpRows}
          <tr class="model-row-guazza">
            <td class="model-name">★ Guazza ML</td>
            <td>${fmtTemp(fc.tmin_c.p50)}</td>
            <td>${fmtTemp(fc.tmax_c.p50)}</td>
            <td>${fmtPrecip(fc.precip_mm.p50)}</td>
          </tr>
        </tbody>
      </table>
    </div>`;
}

// ── Day expanded (detail view for selected day) ───────────────────────────────

function renderDayExpanded(day) {
  const { forecasts: fc, indicators, target_date, lead_time_h } = day;

  const indHtml = Object.entries(indicators).map(([id, ind]) => {
    const meta = INDICATOR_META[id] ?? { label: id, icon: '?' };
    const cls  = VERDICT_CLASS[ind.verdict] ?? 'unknown';
    return `<div class="indicator indicator-${cls}" title="${ind.rule_matched}">
      <span class="ind-icon">${meta.icon}</span>
      <span class="ind-label">${meta.label}</span>
      <span class="ind-verdict">${ind.verdict}</span>
    </div>`;
  }).join('');

  return `
    <section class="day-expanded">
      <div class="day-title">
        <span class="day-date">${fmtDayLabel(target_date)}</span>
        <span class="day-date-sub">${fmtDate(target_date)}</span>
        <span class="day-lead">+${lead_time_h}h</span>
      </div>
      <div class="forecast-grid">
        <div class="forecast-card">
          <div class="fc-label">🌡 Tmin</div>
          <div class="fc-val">${fmtTemp(fc.tmin_c.p50)}</div>
          ${ciBar(fc.tmin_c, '°')}
        </div>
        <div class="forecast-card">
          <div class="fc-label">🌡 Tmax</div>
          <div class="fc-val">${fmtTemp(fc.tmax_c.p50)}</div>
          ${ciBar(fc.tmax_c, '°')}
        </div>
        <div class="forecast-card">
          <div class="fc-label">🌧 Precip</div>
          <div class="fc-val">${fmtPrecip(fc.precip_mm.p50)}</div>
          ${ciBar(fc.precip_mm, ' mm')}
        </div>
      </div>
      <div class="indicators-grid">${indHtml}</div>
      ${renderNwpComparison(day)}
    </section>`;
}

// ── Render ────────────────────────────────────────────────────────────────────

function render(container, data) {
  const day = data.days[selectedDayIdx];

  container.innerHTML = `
    <div class="meta-bar">Aggiornato: ${fmtDateTime(data.generated_at)}${staleWarning(data.generated_at)}</div>
    ${renderCurrentConditions(data.current)}
    <section class="chart-section">
      <div class="chart-header">
        <h3 class="chart-title">Tendenza meteo</h3>
        ${renderModelSwitch(data)}
      </div>
      ${renderCombinedChart(data, selectedModel)}
    </section>
    ${renderDayCards(data.days, selectedDayIdx)}
    ${renderDayExpanded(day)}
    ${coverageBadge(data.coverage_empirical_30d)}
  `;

  container.querySelectorAll('.day-card').forEach(card => {
    card.addEventListener('click', () => {
      selectedDayIdx = parseInt(card.dataset.idx, 10);
      render(container, data);
      const expanded = container.querySelector('.day-expanded');
      if (expanded) {
        const headerH = document.querySelector('header')?.offsetHeight ?? 0;
        window.scrollTo({ top: expanded.getBoundingClientRect().top + window.scrollY - headerH - 8, behavior: 'smooth' });
      }
    });
  });

  container.querySelectorAll('.model-switch-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      selectedModel = btn.dataset.src;
      render(container, data);
    });
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

window.addEventListener('popstate', () => loadLocation(getActiveLoc()));
loadLocation(getActiveLoc());
