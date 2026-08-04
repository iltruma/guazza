// Guazza · Affidabilità — redesign completo
//
// Struttura:
//   1. Ranking compatto (D+1, per-modello da skill_history)
//   2. MAE per orizzonte D+0..D+7 (Guazza vs NWP-consensus da skill.json)
//   3. Errore rolling nel tempo (errore giornaliero |forecast−actual| da skill_history)
//   4. Chi vince sulla temperatura? (Win Rate bar + barre giornaliere vincitore)
//   5. Chi vince sulle precipitazioni? (stesso schema, solo wet days)
//
// Self-contained: nessuna dipendenza da app.js.

// ── Costanti ─────────────────────────────────────────────────────────────────

const AFF_LOCATIONS = [
  { id: 'casa_campi',    label: 'Casa Campi' },
  { id: 'lavoro_cosimo', label: 'Lav. Cosimo' },
  { id: 'lavoro_madda',  label: 'Lav. Madda' },
  { id: 'casa_cesto',    label: 'Casa Cesto' },
  { id: 'casa_nicco',    label: 'Casa Nicco' },
  { id: 'casa_cercina',  label: 'Casa Cercina' },
];

const SKILL_URL         = '/data/skill.json';
const SKILL_HISTORY_URL = '/data/skill_history.json';

// Chiavi sorgente NWP nel JSON (ordine stabile, allineato al backend)
const NWP_SOURCES = [
  'open_meteo_ecmwf_ifs',
  'open_meteo_icon_eu',
  'open_meteo_arome_france',
  'open_meteo_italia_meteo_arpae_icon_2i',
];

const NWP_LABELS = {
  open_meteo_ecmwf_ifs:                   'ECMWF IFS',
  open_meteo_icon_eu:                     'ICON-EU',
  open_meteo_arome_france:                'AROME France',
  open_meteo_italia_meteo_arpae_icon_2i:  'ARPAE ICON-2I',
};

// Colori per modello — usati in tutti e tre i grafici
const MODEL_COLORS = {
  guazza:                                 '#6B7FD4',  // iris accent
  open_meteo_ecmwf_ifs:                   '#F97316',  // warm orange
  open_meteo_icon_eu:                     '#34D399',  // verde
  open_meteo_arome_france:                '#FBBF24',  // giallo
  open_meteo_italia_meteo_arpae_icon_2i:  '#A78BFA',  // viola
};

// Classe CSS dot per la legenda HTML
const MODEL_DOT_CLASS = {
  guazza:                                 'aff-legend-dot--guazza',
  open_meteo_ecmwf_ifs:                   'aff-legend-dot--ecmwf',
  open_meteo_icon_eu:                     'aff-legend-dot--iconeu',
  open_meteo_arome_france:                'aff-legend-dot--arome',
  open_meteo_italia_meteo_arpae_icon_2i:  'aff-legend-dot--icon2i',
};

// ── Stato applicazione ────────────────────────────────────────────────────────

let skillData   = null;    // skill.json
let histData    = null;    // skill_history.json

let affLocId       = 'casa_campi';
let rankingVar     = 'tmax_c';   // tmax_c | tmin_c
let horizonVar     = 'tmax_c';   // tmax_c | tmin_c
let rollingVar     = 'tmax_c';   // tmax_c | tmin_c | precip_mm
let rollingWindow  = 90;         // giorni; 0 = totale

let horizonChart    = null;
let rollingChart    = null;
let winnerTempChart = null;
let winnerPrecipChart = null;

let winnerTempVar = 'tmax_c';   // tmax_c | tmin_c

// ── Utilities ─────────────────────────────────────────────────────────────────

function showEl(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hideEl(id) { document.getElementById(id)?.classList.add('hidden'); }

function fmtDate(iso) {
  if (!iso) return '';
  const [y, m, d] = iso.split('-');
  return `${d}/${m}/${y.slice(2)}`;
}

// Calcola MAE di un array di valori forecast vs actual (ignora null/null)
function computeMAE(forecasts, actuals) {
  let sum = 0, n = 0;
  for (let i = 0; i < forecasts.length; i++) {
    const f = forecasts[i];
    const a = actuals[i];
    if (f == null || a == null) continue;
    sum += Math.abs(f - a);
    n++;
  }
  return n > 0 ? sum / n : null;
}

// Calcola errore assoluto giornaliero: array parallelo a dates, null se mancante
function absError(forecasts, actuals) {
  if (!forecasts || !actuals) return [];
  return forecasts.map((f, i) => {
    const a = actuals[i];
    return f != null && a != null ? Math.abs(f - a) : null;
  });
}

// Media mobile: finestra `k` giorni, null se non abbastanza valori (min 3)
function rolling(arr, k) {
  const out = new Array(arr.length).fill(null);
  for (let i = 0; i < arr.length; i++) {
    const start = Math.max(0, i - k + 1);
    const slice = arr.slice(start, i + 1).filter(v => v != null);
    if (slice.length >= Math.min(3, k)) {
      out[i] = slice.reduce((s, v) => s + v, 0) / slice.length;
    }
  }
  return out;
}

// Legge CSS custom property dal root
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ── Fetch ─────────────────────────────────────────────────────────────────────

async function loadSkill() {
  try {
    const r = await fetch(SKILL_URL, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    skillData = await r.json();
  } catch {
    skillData = null;
  }
}

async function loadHistory() {
  try {
    const r = await fetch(SKILL_HISTORY_URL, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    histData = await r.json();
  } catch {
    // history è best-effort: la pagina resta valida anche senza
    histData = null;
  }
}

// ── Location tabs ─────────────────────────────────────────────────────────────

function renderLocationTabs() {
  const nav = document.getElementById('aff-locations');
  nav.innerHTML = AFF_LOCATIONS.map(l => {
    const active = l.id === affLocId;
    return `<button class="g-tab${active ? ' g-tab--active' : ''}" data-loc="${l.id}"${active ? ' aria-current="page"' : ''}>${l.label}</button>`;
  }).join('');
  nav.querySelectorAll('[data-loc]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.loc !== affLocId) {
        affLocId = btn.dataset.loc;
        renderLocationTabs();
        renderAll();
      }
    });
  });
}

// ── Header meta ───────────────────────────────────────────────────────────────

function renderHeaderMeta() {
  const meta = document.getElementById('header-meta');
  if (!meta) return;
  const ts = (skillData?.generated_at || histData?.generated_at || '').replace('T', ' ').slice(0, 16);
  if (ts) meta.textContent = `skill aggiornato ${ts}`;
}

// ── 1. Ranking compatto ───────────────────────────────────────────────────────

function renderRanking() {
  const loc = histData?.locations?.[affLocId];
  if (!loc) { hideEl('aff-ranking-card'); return; }

  const series = loc[rankingVar];
  if (!series?.dates?.length || !series.actual) { hideEl('aff-ranking-card'); return; }

  showEl('aff-ranking-card');

  // Calcola MAE D+1 per ogni modello dalla history (tutti i dati disponibili, no slice)
  const actual = series.actual;
  const models = [
    { key: 'guazza', label: 'Guazza ML', forecasts: series.guazza },
    ...NWP_SOURCES.map(s => ({ key: s, label: NWP_LABELS[s], forecasts: series[s] })),
  ];

  const scored = models
    .map(m => ({
      ...m,
      mae: computeMAE(m.forecasts || [], actual),
    }))
    .filter(m => m.mae != null)
    .sort((a, b) => a.mae - b.mae);

  if (!scored.length) { hideEl('aff-ranking-card'); return; }

  const worstMAE = Math.max(...scored.map(m => m.mae));
  const bestKey  = scored[0].key;

  // Caption data range
  const d1 = fmtDate(histData.min_date);
  const d2 = fmtDate(histData.max_date);
  const varLabel = rankingVar === 'tmax_c' ? 'T max' : 'T min';
  document.getElementById('ranking-sub').textContent =
    `${varLabel} · periodo ${d1}→${d2} · ${series.dates.length} giorni`;
  document.getElementById('ranking-caption').textContent =
    `MAE medio nel periodo su lead 24h. Calcolato da skill_history.json (tutti i giorni disponibili). ` +
    `Verità: stazione SIR pesata. Il miglior modello non è necessariamente Guazza.`;

  const container = document.getElementById('ranking-cards');
  container.innerHTML = scored.map((m, idx) => {
    const isGuazza = m.key === 'guazza';
    const isBest   = m.key === bestKey;
    const dotCls   = MODEL_DOT_CLASS[m.key] || 'aff-legend-dot--nwp';
    const barPct   = worstMAE > 0 ? ((m.mae / worstMAE) * 100).toFixed(1) : '100';
    const barColor = MODEL_COLORS[m.key] || 'rgba(148,163,174,0.55)';

    let badge = '';
    if (isGuazza) badge += `<span class="aff-rank-card__badge aff-rank-card__badge--guazza">Guazza</span>`;
    if (isBest)   badge += `<span class="aff-rank-card__badge aff-rank-card__badge--best">✓ Migliore</span>`;

    const cardClass = [
      'aff-rank-card',
      isGuazza ? 'aff-rank-card--guazza' : '',
      isBest && !isGuazza ? 'aff-rank-card--best' : '',
    ].filter(Boolean).join(' ');

    return `
      <div class="${cardClass}" role="listitem">
        ${badge}
        <div class="aff-rank-card__name">${m.label}</div>
        <div class="aff-rank-card__mae">${m.mae.toFixed(2)}<span style="font-size:0.625rem;color:var(--text-3);font-weight:400;margin-left:2px">°C</span></div>
        <div class="aff-rank-card__rank">#${idx + 1} su ${scored.length}</div>
        <div class="aff-rank-card__bar">
          <div class="aff-rank-card__bar-fill" style="width:${barPct}%;background:${barColor}"></div>
        </div>
      </div>
    `;
  }).join('');
}

// ── 2. MAE per orizzonte (Guazza vs NWP-consensus) ────────────────────────────

function renderHorizon() {
  const loc = skillData?.locations?.[affLocId];
  if (!loc) { hideEl('aff-horizon-card'); return; }

  const pts = loc[horizonVar];
  if (!pts?.length || !pts.some(p => p.mae_ml != null)) {
    hideEl('aff-horizon-card'); return;
  }
  showEl('aff-horizon-card');

  const varLabel = horizonVar === 'tmax_c' ? 'T max' : 'T min';
  const d1 = fmtDate(skillData.window_start);
  const d2 = fmtDate(skillData.window_end);
  document.getElementById('horizon-sub').textContent =
    `${varLabel} · SIR ${loc.sir_station_id} · ${d1}→${d2}`;
  document.getElementById('horizon-caption').textContent =
    `Guazza vs consensus NWP medio (ECMWF + ICON-EU + AROME + ICON-2I aggregati) per orizzonte. ` +
    `Fonte: skill.json (CV out-of-sample, embargo ${skillData.embargo_days}gg). ` +
    `Il dettaglio per-modello è nel grafico precedente (solo D+1).`;

  drawHorizonChart(pts);
}

function drawHorizonChart(pts) {
  const canvas = document.getElementById('aff-horizon-chart');
  if (!canvas || typeof Chart === 'undefined') return;
  if (horizonChart) { horizonChart.destroy(); horizonChart = null; }

  const axis  = cssVar('--chart-axis');
  const grid  = cssVar('--chart-grid');
  const iris  = MODEL_COLORS.guazza;
  const nwpClr = 'rgba(148,163,174,0.60)';

  const labels = pts.map(p => `D+${p.lead_h / 24}`);
  const mlData  = pts.map(p => p.mae_ml  ?? null);
  const nwpData = pts.map(p => p.mae_nwp ?? null);

  horizonChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Guazza ML',
          data: mlData,
          borderColor: iris,
          backgroundColor: iris,
          borderWidth: 2.5,
          pointRadius: 4,
          pointHoverRadius: 6,
          spanGaps: true,
          tension: 0.3,
          order: 1,
        },
        {
          label: 'NWP consensus',
          data: nwpData,
          borderColor: nwpClr,
          backgroundColor: nwpClr,
          borderDash: [5, 4],
          borderWidth: 1.5,
          pointRadius: 3,
          pointHoverRadius: 5,
          spanGaps: true,
          tension: 0.3,
          order: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          grid: { color: grid },
          ticks: { color: axis, font: { family: 'JetBrains Mono, monospace', size: 11 } },
        },
        y: {
          grid: { color: grid },
          ticks: {
            color: axis,
            font: { family: 'JetBrains Mono, monospace', size: 11 },
            callback: v => `${v.toFixed(1)}°`,
          },
          title: { display: true, text: 'MAE (°C)', color: axis, font: { size: 10 } },
        },
      },
      plugins: {
        legend: {
          display: false,  // legenda HTML sopra
        },
        tooltip: {
          backgroundColor: 'rgba(19,19,19,0.97)',
          borderColor: 'rgba(255,255,255,0.09)',
          borderWidth: 1,
          titleColor: axis,
          bodyColor: cssVar('--text-2'),
          callbacks: {
            afterBody: (items) => {
              const i = items[0].dataIndex;
              const p = pts[i];
              if (!p) return '';
              const lines = [];
              if (p.skill_pct != null) {
                const sign = p.skill_pct >= 0 ? '+' : '';
                lines.push(`Skill: ${sign}${p.skill_pct.toFixed(1)}%`);
              }
              if (p.n != null) lines.push(`Campioni: ${p.n}`);
              return lines;
            },
          },
        },
      },
    },
  });
}

// ── 3. Errore rolling nel tempo ────────────────────────────────────────────────

function renderRolling() {
  const loc = histData?.locations?.[affLocId];
  if (!loc) { hideEl('aff-rolling-card'); return; }

  const series = loc[rollingVar];
  if (!series?.dates?.length) { hideEl('aff-rolling-card'); return; }

  const actual = series.actual;
  const hasAny = actual?.some(v => v != null);
  if (!hasAny) { hideEl('aff-rolling-card'); return; }

  showEl('aff-rolling-card');

  // Applica finestra temporale (slice finale)
  let dates    = series.dates;
  let actSlice = actual;
  const sourceArrays = {};
  ['guazza', ...NWP_SOURCES].forEach(k => { sourceArrays[k] = series[k]; });

  if (rollingWindow > 0 && dates.length > rollingWindow) {
    const start = dates.length - rollingWindow;
    dates = dates.slice(start);
    actSlice = actSlice.slice(start);
    for (const k of Object.keys(sourceArrays)) {
      sourceArrays[k] = sourceArrays[k]?.slice(start);
    }
  }

  // Caption
  const varLabel = { tmax_c: 'T max', tmin_c: 'T min', precip_mm: 'Precip' }[rollingVar] || rollingVar;
  const unit     = rollingVar === 'precip_mm' ? 'mm' : '°C';
  const winLabel = rollingWindow === 0 ? 'Totale' : `Ultimi ${rollingWindow}gg`;
  document.getElementById('rolling-sub').textContent =
    `${varLabel} · lead 24h · ${winLabel} · ${dates.length} giorni`;
  document.getElementById('rolling-caption').textContent =
    `Errore assoluto giornaliero |forecast − osservato| senza smoothing. ` +
    `Fonte: skill_history.json. Verità: stazione SIR pesata della location. ` +
    `Un punto basso = quel giorno il modello ha indovinato. ` +
    `Guazza mostra i null nelle prime settimane (warm-up del modello).`;

  drawRollingChart(dates, actSlice, sourceArrays, unit);
}

function drawRollingChart(dates, actual, sourceArrays, unit) {
  const canvas = document.getElementById('aff-rolling-chart');
  if (!canvas || typeof Chart === 'undefined') return;
  if (rollingChart) { rollingChart.destroy(); rollingChart = null; }

  const axis = cssVar('--chart-axis');
  const grid = cssVar('--chart-grid');

  // Dataset: errore assoluto giornaliero per ogni modello
  const allModels = ['guazza', ...NWP_SOURCES];
  const datasets = [];

  for (const key of allModels) {
    const forecasts = sourceArrays[key];
    if (!forecasts) continue;
    const errors = absError(forecasts, actual);
    if (!errors.some(v => v != null)) continue;

    const color  = MODEL_COLORS[key] || 'rgba(148,163,174,0.55)';
    const isGuazza = key === 'guazza';

    datasets.push({
      label: isGuazza ? 'Guazza ML' : NWP_LABELS[key] || key,
      data: errors,
      borderColor: color,
      backgroundColor: color,
      borderWidth: isGuazza ? 2 : 1.2,
      pointRadius: 0,
      spanGaps: true,
      tension: 0.2,
      borderDash: isGuazza ? [] : [4, 3],
      order: isGuazza ? 1 : 2,
    });
  }

  if (!datasets.length) return;

  // Label asse X: "DD/MM" ogni tot
  const labels = dates.map(d => {
    const [, m, day] = d.split('-');
    return `${day}/${m}`;
  });

  rollingChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          grid: { color: grid, display: false },
          ticks: {
            color: axis,
            font: { family: 'JetBrains Mono, monospace', size: 10 },
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 10,
          },
        },
        y: {
          grid: { color: grid },
          min: 0,
          ticks: {
            color: axis,
            font: { family: 'JetBrains Mono, monospace', size: 10 },
            callback: v => `${v.toFixed(1)}${unit}`,
          },
          title: { display: true, text: `|err| (${unit})`, color: axis, font: { size: 10 } },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(19,19,19,0.97)',
          borderColor: 'rgba(255,255,255,0.09)',
          borderWidth: 1,
          titleColor: axis,
          bodyColor: cssVar('--text-2'),
          itemSort: (a, b) => (a.raw ?? Infinity) - (b.raw ?? Infinity),
          callbacks: {
            label: (item) => {
              const v = item.raw;
              return v != null
                ? `${item.dataset.label}: ${v.toFixed(2)} ${unit}`
                : `${item.dataset.label}: —`;
            },
          },
        },
      },
    },
  });
}

// ── Render tutto ──────────────────────────────────────────────────────────────

function renderAll() {
  renderRanking();
  renderHorizon();
  renderRolling();
  renderWinnerTemp();
  renderWinnerPrecip();
}

// ── Event listeners controlli ─────────────────────────────────────────────────

// Ranking: toggle Tmax/Tmin
document.getElementById('aff-ranking-card')?.addEventListener('click', e => {
  const btn = e.target.closest('[data-var]');
  if (!btn || !btn.closest('.aff-ranking__toggle')) return;
  const v = btn.dataset.var;
  if (v === rankingVar) return;
  rankingVar = v;
  btn.closest('.aff-ranking__toggle')
     .querySelectorAll('.aff-ranking__toggle-btn')
     .forEach(b => b.classList.toggle('is-active', b.dataset.var === rankingVar));
  renderRanking();
});

// Horizon: toggle Tmax/Tmin
document.getElementById('aff-horizon-card')?.addEventListener('click', e => {
  const btn = e.target.closest('[data-var]');
  if (!btn || !btn.closest('.aff-ranking__toggle')) return;
  const v = btn.dataset.var;
  if (v === horizonVar) return;
  horizonVar = v;
  btn.closest('.aff-ranking__toggle')
     .querySelectorAll('.aff-ranking__toggle-btn')
     .forEach(b => b.classList.toggle('is-active', b.dataset.var === horizonVar));
  renderHorizon();
});

// Rolling: toggle variabile + finestra
document.getElementById('aff-rolling-card')?.addEventListener('click', e => {
  // Toggle variabile
  const varBtn = e.target.closest('[data-var]');
  if (varBtn && varBtn.closest('.aff-ranking__toggle')) {
    const v = varBtn.dataset.var;
    if (v !== rollingVar) {
      rollingVar = v;
      varBtn.closest('.aff-ranking__toggle')
            .querySelectorAll('.aff-ranking__toggle-btn')
            .forEach(b => b.classList.toggle('is-active', b.dataset.var === rollingVar));
      renderRolling();
    }
    return;
  }
  // Toggle finestra
  const winBtn = e.target.closest('[data-window]');
  if (winBtn && winBtn.closest('.g-skill__seg')) {
    const w = parseInt(winBtn.dataset.window, 10);
    if (w !== rollingWindow) {
      rollingWindow = w;
      winBtn.closest('.g-skill__seg')
            .querySelectorAll('.g-skill__seg-btn')
            .forEach(b => b.classList.toggle('is-active', parseInt(b.dataset.window, 10) === rollingWindow));
      renderRolling();
      renderWinnerTemp();
      renderWinnerPrecip();
    }
  }
});

// ── Utility: calcola vincitori giornalieri ─────────────────────────────────────
//
// Restituisce un array parallelo a `dates` dove ogni elemento è:
//   { winner: key | null, margin: number, errors: { [key]: number|null } }
// winner = null se non ci sono almeno 2 modelli con dati in quel giorno.
// margin = errore del 2° migliore − errore del 1° (>0 significa vittoria netta).

function computeDailyWinners(dates, actual, sourceArrays) {
  const allKeys = ['guazza', ...NWP_SOURCES];
  return dates.map((_, i) => {
    const a = actual[i];
    if (a == null) return { winner: null, margin: 0, errors: {} };

    const errors = {};
    for (const k of allKeys) {
      const f = sourceArrays[k]?.[i];
      errors[k] = (f != null) ? Math.abs(f - a) : null;
    }

    const ranked = Object.entries(errors)
      .filter(([, e]) => e != null)
      .sort(([, a], [, b]) => a - b);

    if (ranked.length < 1) return { winner: null, margin: 0, errors };
    const winner = ranked[0][0];
    const margin = ranked.length >= 2 ? ranked[1][1] - ranked[0][1] : 0;
    return { winner, margin, errors };
  });
}

// Calcola la Win Rate per ogni modello da un array di winners
function computeWinRate(winners) {
  const counts = {};
  let total = 0;
  for (const w of winners) {
    if (!w.winner) continue;
    counts[w.winner] = (counts[w.winner] || 0) + 1;
    total++;
  }
  if (total === 0) return null;
  const rate = {};
  for (const k of Object.keys(counts)) {
    rate[k] = counts[k] / total;
  }
  return { rate, total };
}

// Renderizza la Win Rate stacked bar + legenda in un container
function renderWinRateBar(barEl, legendEl, winRate) {
  if (!winRate) { barEl.innerHTML = ''; legendEl.innerHTML = ''; return; }

  const allKeys = ['guazza', ...NWP_SOURCES];
  // Ordina per win rate decrescente
  const sorted = allKeys
    .filter(k => winRate.rate[k] > 0)
    .sort((a, b) => (winRate.rate[b] || 0) - (winRate.rate[a] || 0));

  barEl.innerHTML = sorted.map(k => {
    const pct = (winRate.rate[k] * 100).toFixed(1);
    const color = MODEL_COLORS[k] || 'rgba(148,163,174,0.55)';
    const label = k === 'guazza' ? 'Guazza' : NWP_LABELS[k] || k;
    return `<div class="aff-winrate__seg" style="width:${pct}%;background:${color}" title="${label}: ${pct}%"></div>`;
  }).join('');

  legendEl.innerHTML = sorted.map(k => {
    const pct = (winRate.rate[k] * 100).toFixed(0);
    const wins = Math.round(winRate.rate[k] * winRate.total);
    const color = MODEL_COLORS[k] || 'rgba(148,163,174,0.55)';
    const label = k === 'guazza' ? 'Guazza ML' : NWP_LABELS[k] || k;
    return `
      <span class="aff-winrate__legend-item">
        <span class="aff-winrate__legend-dot" style="background:${color}"></span>
        ${label} <span class="aff-winrate__legend-pct">${pct}%</span>
        <span style="color:var(--text-3)">(${wins}gg)</span>
      </span>`;
  }).join('');
}

// Crea/aggiorna un grafico a barre giornaliere vincitore
// barAlpha: opacità delle barre nei giorni in cui il vincitore non ha vinto di netto
function drawWinnerChart(canvasId, chartRef, dates, winners, unit) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === 'undefined') return chartRef;

  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();

  if (!winners.some(w => w.winner)) return null;

  const axis = cssVar('--chart-axis');
  const grid = cssVar('--chart-grid');

  // Ogni giorno: una barra colorata col vincitore, alta quanto il margine (≥ 0)
  // Se il margine è 0 (un solo modello disponibile), usiamo 0.01 per rendere la barra visibile
  const labels = dates.map(d => { const [, m, day] = d.split('-'); return `${day}/${m}`; });

  const barColors = winners.map(w =>
    w.winner ? (MODEL_COLORS[w.winner] || 'rgba(148,163,174,0.55)') : 'transparent'
  );
  const margins = winners.map(w => w.margin > 0 ? w.margin : (w.winner ? 0.01 : null));

  const newChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Margine vittoria',
        data: margins,
        backgroundColor: barColors,
        borderWidth: 0,
        borderRadius: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            color: axis,
            font: { family: 'JetBrains Mono, monospace', size: 10 },
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 12,
          },
        },
        y: {
          grid: { color: grid },
          min: 0,
          ticks: {
            color: axis,
            font: { family: 'JetBrains Mono, monospace', size: 10 },
            callback: v => v === 0.01 ? '' : `${v.toFixed(1)}${unit}`,
          },
          title: { display: true, text: `margine (${unit})`, color: axis, font: { size: 10 } },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(19,19,19,0.97)',
          borderColor: 'rgba(255,255,255,0.09)',
          borderWidth: 1,
          titleColor: axis,
          bodyColor: cssVar('--text-2'),
          callbacks: {
            title: (items) => {
              const i = items[0].dataIndex;
              return dates[i] || '';
            },
            label: (item) => {
              const i = item.dataIndex;
              const w = winners[i];
              if (!w?.winner) return 'Dati insufficienti';
              const winnerLabel = w.winner === 'guazza' ? 'Guazza ML' : (NWP_LABELS[w.winner] || w.winner);
              const margin = w.margin > 0 ? `  margine +${w.margin.toFixed(2)}${unit}` : '  (unico dato)';
              return `Vincitore: ${winnerLabel}${margin}`;
            },
            afterLabel: (item) => {
              const i = item.dataIndex;
              const w = winners[i];
              if (!w?.errors) return [];
              // Mostra tutti gli errori ordinati
              return Object.entries(w.errors)
                .filter(([, e]) => e != null)
                .sort(([, a], [, b]) => a - b)
                .map(([k, e]) => {
                  const lbl = k === 'guazza' ? 'Guazza ML' : (NWP_LABELS[k] || k);
                  return `  ${lbl}: ${e.toFixed(2)}${unit}`;
                });
            },
          },
        },
      },
    },
  });

  return newChart;
}

// ── 4. Chi vince sulla temperatura? ──────────────────────────────────────────

function renderWinnerTemp() {
  const loc = histData?.locations?.[affLocId];
  if (!loc) { hideEl('aff-winner-temp-card'); return; }

  const series = loc[winnerTempVar];
  if (!series?.dates?.length || !series.actual?.some(v => v != null)) {
    hideEl('aff-winner-temp-card'); return;
  }
  showEl('aff-winner-temp-card');

  // Applica finestra (stessa di rollingWindow)
  let dates    = series.dates;
  let actual   = series.actual;
  const sourceArrays = {};
  ['guazza', ...NWP_SOURCES].forEach(k => { sourceArrays[k] = series[k]; });

  if (rollingWindow > 0 && dates.length > rollingWindow) {
    const start = dates.length - rollingWindow;
    dates = dates.slice(start);
    actual = actual.slice(start);
    for (const k of Object.keys(sourceArrays)) {
      sourceArrays[k] = sourceArrays[k]?.slice(start);
    }
  }

  const varLabel = winnerTempVar === 'tmax_c' ? 'T max' : 'T min';
  const winLabel = rollingWindow === 0 ? 'Totale' : `Ultimi ${rollingWindow}gg`;
  document.getElementById('winner-temp-sub').textContent =
    `${varLabel} · lead 24h · ${winLabel} · ${dates.length} giorni`;

  const winners = computeDailyWinners(dates, actual, sourceArrays);
  const winRate = computeWinRate(winners);

  renderWinRateBar(
    document.getElementById('winner-temp-winrate-bar'),
    document.getElementById('winner-temp-winrate-legend'),
    winRate,
  );

  winnerTempChart = drawWinnerChart(
    'aff-winner-temp-chart', winnerTempChart, dates, winners, '°C'
  );

  const winDays = winners.filter(w => w.winner).length;
  document.getElementById('winner-temp-caption').textContent =
    `Ogni barra = un giorno. Colore = modello con errore assoluto minore. ` +
    `Altezza = vantaggio rispetto al secondo classificato (barre piatte = vittoria risicata). ` +
    `${winDays} giorni con almeno un modello disponibile su ${dates.length}. ` +
    `Fonte: skill_history.json lead 24h.`;
}

// ── 5. Chi vince sulle precipitazioni? ───────────────────────────────────────

function renderWinnerPrecip() {
  const loc = histData?.locations?.[affLocId];
  if (!loc) { hideEl('aff-winner-precip-card'); return; }

  const series = loc.precip_mm;
  if (!series?.dates?.length || !series.actual?.some(v => v != null)) {
    hideEl('aff-winner-precip-card'); return;
  }
  showEl('aff-winner-precip-card');

  // Applica finestra
  let dates    = series.dates;
  let actual   = series.actual;
  const sourceArrays = {};
  ['guazza', ...NWP_SOURCES].forEach(k => { sourceArrays[k] = series[k]; });

  if (rollingWindow > 0 && dates.length > rollingWindow) {
    const start = dates.length - rollingWindow;
    dates = dates.slice(start);
    actual = actual.slice(start);
    for (const k of Object.keys(sourceArrays)) {
      sourceArrays[k] = sourceArrays[k]?.slice(start);
    }
  }

  // Filtra wet days: almeno un modello o actual > 0.2mm
  const WET_THRESHOLD = 0.2;
  const allKeys = ['guazza', ...NWP_SOURCES];
  const wetMask = dates.map((_, i) => {
    if ((actual[i] ?? 0) > WET_THRESHOLD) return true;
    return allKeys.some(k => (sourceArrays[k]?.[i] ?? 0) > WET_THRESHOLD);
  });

  const wetDates   = dates.filter((_, i) => wetMask[i]);
  const wetActual  = actual.filter((_, i) => wetMask[i]);
  const wetSources = {};
  for (const k of allKeys) {
    wetSources[k] = sourceArrays[k]?.filter((_, i) => wetMask[i]) ?? [];
  }

  const winLabel = rollingWindow === 0 ? 'Totale' : `Ultimi ${rollingWindow}gg`;
  document.getElementById('winner-precip-sub').textContent =
    `Precip · lead 24h · ${winLabel} · ${wetDates.length} wet days su ${dates.length}`;

  const chartArea = document.getElementById('winner-precip-chart-area');

  if (!wetDates.length) {
    // Nessun giorno bagnato nella finestra
    chartArea.innerHTML = '<div class="aff-wet-empty">Nessun giorno bagnato (≥0.2mm) nel periodo selezionato.</div>';
    document.getElementById('winner-precip-winrate-bar').innerHTML = '';
    document.getElementById('winner-precip-winrate-legend').innerHTML = '';
    document.getElementById('winner-precip-caption').textContent = '';
    if (winnerPrecipChart) { winnerPrecipChart.destroy(); winnerPrecipChart = null; }
    return;
  }

  // Ripristina il canvas se era stato sostituito dal messaggio empty
  if (!chartArea.querySelector('canvas')) {
    chartArea.innerHTML = `
      <div class="aff-winner__chart-scroll">
        <div class="aff-winner__canvas-wrap">
          <canvas id="aff-winner-precip-chart"
            aria-label="Vittorie giornaliere sulle precipitazioni: solo wet days"></canvas>
        </div>
      </div>`;
  }

  const winners = computeDailyWinners(wetDates, wetActual, wetSources);
  const winRate = computeWinRate(winners);

  renderWinRateBar(
    document.getElementById('winner-precip-winrate-bar'),
    document.getElementById('winner-precip-winrate-legend'),
    winRate,
  );

  winnerPrecipChart = drawWinnerChart(
    'aff-winner-precip-chart', winnerPrecipChart, wetDates, winners, 'mm'
  );

  const winDays = winners.filter(w => w.winner).length;
  document.getElementById('winner-precip-caption').textContent =
    `Solo wet days (osservato o almeno un forecast ≥${WET_THRESHOLD}mm). ` +
    `Colore = modello con errore assoluto minore. Altezza = margine di vittoria. ` +
    `${winDays} wet days nel periodo · Fonte: skill_history.json lead 24h.`;
}

// ── Event listener: winner temp toggle ───────────────────────────────────────

document.getElementById('aff-winner-temp-card')?.addEventListener('click', e => {
  const btn = e.target.closest('[data-var]');
  if (!btn || !btn.closest('.aff-ranking__toggle')) return;
  const v = btn.dataset.var;
  if (v === winnerTempVar) return;
  winnerTempVar = v;
  btn.closest('.aff-ranking__toggle')
     .querySelectorAll('.aff-ranking__toggle-btn')
     .forEach(b => b.classList.toggle('is-active', b.dataset.var === winnerTempVar));
  renderWinnerTemp();
});

// ── Boot ──────────────────────────────────────────────────────────────────────

(async function boot() {
  await Promise.all([loadSkill(), loadHistory()]);

  if (!skillData && !histData) {
    showEl('aff-error');
    return;
  }

  renderHeaderMeta();
  renderLocationTabs();
  renderAll();
})();
