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

let currentData = null;
let selectedDayIdx = 0;

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

function fmtDateTime(iso) {
  return new Date(iso).toLocaleString('it-IT', {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

function fmtTemp(v)   { return v != null ? `${v.toFixed(1)}°` : '—'; }
function fmtPrecip(v) { return v != null ? `${v.toFixed(1)} mm` : '—'; }

// ── Coverage badge ────────────────────────────────────────────────────────────

function coverageBadge(cov) {
  const allNull = Object.values(cov).every(v => v === null);
  if (allNull) {
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

  const pct = v => Math.max(0, Math.min(100, ((v - ci90_lo) / range) * 100)).toFixed(1);
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

// ── Hourly SVG chart ──────────────────────────────────────────────────────────

function hourlyChart(hourly) {
  if (!hourly || hourly.length === 0) {
    return '<div class="no-hourly">Dati orari non disponibili</div>';
  }

  const W = 700, H = 130;
  const padL = 36, padR = 52, padT = 12, padB = 28;
  const cW = W - padL - padR;
  const cH = H - padT - padB;

  const validTemps = hourly.filter(h => h.temp_c != null).map(h => h.temp_c);
  if (validTemps.length === 0) return '<div class="no-hourly">Dati orari non disponibili</div>';

  const minT = Math.floor(Math.min(...validTemps)) - 1;
  const maxT = Math.ceil(Math.max(...validTemps)) + 1;
  const maxP = Math.max(...hourly.map(h => h.precip_mm ?? 0), 0.5);

  const xPos = h => padL + (h / 23) * cW;
  const yT   = t => padT + (1 - (t - minT) / (maxT - minT)) * cH;

  const precipMaxH = cH * 0.35;
  const yPBase = padT + cH;
  const barW   = Math.max(4, cW / 24 * 0.7);

  const pts = hourly
    .filter(h => h.temp_c != null)
    .map(h => `${xPos(h.hour).toFixed(1)},${yT(h.temp_c).toFixed(1)}`);
  const linePath = pts.length > 1 ? `M ${pts.join(' L ')}` : '';

  const precipBars = hourly.map(h => {
    const p = h.precip_mm ?? 0;
    if (p < 0.05) return '';
    const bH      = (p / maxP) * precipMaxH;
    const x       = xPos(h.hour) - barW / 2;
    const y       = yPBase - bH;
    const opacity = (0.3 + (h.precip_prob ?? 0.5) * 0.7).toFixed(2);
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${bH.toFixed(1)}" fill="#3b82f6" opacity="${opacity}" rx="1"/>`;
  }).join('');

  const xLabels = [0, 3, 6, 9, 12, 15, 18, 21, 23].map(h =>
    `<text x="${xPos(h).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="chart-label">${String(h).padStart(2, '0')}</text>`
  ).join('');

  const ticks = [minT + 1, Math.round((minT + maxT) / 2), maxT - 1];
  const yLabels = ticks.map(t =>
    `<text x="${padL - 4}" y="${yT(t).toFixed(1)}" text-anchor="end" dominant-baseline="middle" class="chart-label">${t}°</text>`
  ).join('');

  const gridLines = [0.25, 0.5, 0.75].map(f => {
    const y = (padT + f * cH).toFixed(1);
    return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" class="chart-grid"/>`;
  }).join('');

  const precipLabel = `<text x="${W - padR + 4}" y="${(yPBase - precipMaxH * 0.5).toFixed(1)}" dominant-baseline="middle" class="chart-label" style="fill:#3b82f6">${maxP.toFixed(1)}mm</text>`;

  return `
    <svg viewBox="0 0 ${W} ${H}" class="hourly-chart" aria-hidden="true">
      ${gridLines}
      ${precipBars}
      ${linePath ? `<path d="${linePath}" fill="none" stroke="#f97316" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` : ''}
      ${xLabels}
      ${yLabels}
      ${precipLabel}
    </svg>
    <div class="chart-legend">
      <span class="legend-temp">— Temperatura</span>
      <span class="legend-precip">▪ Precipitazioni (opacità = probabilità)</span>
    </div>`;
}

// ── Day hero ──────────────────────────────────────────────────────────────────

function renderDayHero(day) {
  const { forecasts: fc, indicators, target_date, lead_time_h } = day;

  const indHtml = Object.entries(indicators).map(([id, ind]) => {
    const meta = INDICATOR_META[id] ?? { label: id, icon: '?' };
    const cls  = VERDICT_CLASS[ind.verdict] ?? 'unknown';
    return `<div class="indicator indicator-${cls}">
      <span class="ind-icon">${meta.icon}</span>
      <span class="ind-label">${meta.label}</span>
      <span class="ind-verdict">${ind.verdict}</span>
    </div>`;
  }).join('');

  return `
    <section class="day-hero">
      <div class="day-title">
        <span class="day-date">${fmtDate(target_date)}</span>
        <span class="day-lead">lead +${lead_time_h}h</span>
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
    </section>`;
}

// ── Day strip ─────────────────────────────────────────────────────────────────

function renderDayStrip(days, activeDayIdx) {
  const header = `<div class="strip-header">
    <span>Data</span><span>Tmin / Tmax</span><span>Precip</span><span>Indicatori</span>
  </div>`;

  const rows = days.map((day, idx) => {
    const { target_date, forecasts: fc, indicators } = day;
    const dots = Object.values(indicators).map(ind => {
      const cls = VERDICT_CLASS[ind.verdict] ?? 'unknown';
      return `<span class="dot dot-${cls}" title="${ind.verdict}"></span>`;
    }).join('');

    return `<div class="strip-row${idx === activeDayIdx ? ' active' : ''}" data-idx="${idx}">
      <span class="strip-date">${fmtDate(target_date)}</span>
      <span class="strip-temp">${fmtTemp(fc.tmin_c.p50)} / ${fmtTemp(fc.tmax_c.p50)}</span>
      <span class="strip-precip">${fmtPrecip(fc.precip_mm.p50)}</span>
      <span class="strip-inds">${dots}</span>
    </div>`;
  }).join('');

  return `<section class="day-strip">
    <h3>Prossimi ${days.length} giorni</h3>
    ${header}${rows}
  </section>`;
}

// ── Render ────────────────────────────────────────────────────────────────────

function render(container, data) {
  const day = data.days[selectedDayIdx];

  container.innerHTML = `
    <div class="meta-bar">Aggiornato: ${fmtDateTime(data.generated_at)}</div>
    ${coverageBadge(data.coverage_empirical_30d)}
    ${renderDayHero(day)}
    <section class="hourly-section">
      <h3>Profilo orario — ${fmtDate(day.target_date)}</h3>
      ${hourlyChart(day.hourly)}
    </section>
    ${renderDayStrip(data.days, selectedDayIdx)}
  `;

  container.querySelectorAll('.strip-row').forEach(row => {
    row.addEventListener('click', () => {
      selectedDayIdx = parseInt(row.dataset.idx, 10);
      render(container, data);
    });
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

window.addEventListener('popstate', () => loadLocation(getActiveLoc()));
loadLocation(getActiveLoc());
