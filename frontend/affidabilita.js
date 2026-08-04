// Guazza · Affidabilità — redesign completo
//
// Struttura:
//   1. Ranking compatto (D+1, per-modello da skill_history)
//   2. MAE per orizzonte D+0..D+7 (Guazza vs NWP-consensus da skill.json)
//   3. Errore rolling nel tempo (errore giornaliero |forecast−actual| da skill_history)
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

let horizonChart = null;
let rollingChart = null;

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
    }
  }
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
