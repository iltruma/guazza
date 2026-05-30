# Frontend — librerie client-side (CDN jsDelivr)

Riferimento on-demand. Per il design system completo vedi `DESIGN.md`.

Il frontend usa **CSS custom** (`style.css`, classi prefissate `g-*`) — nessun framework
CSS (Tailwind/DaisyUI rimossi nel redesign v2, vedi `DESIGN.md`). Font caricati via CDN:
**Geist** (display/titoli) + **JetBrains Mono** (dati numerici). Librerie client-side
caricate via CDN jsDelivr: **Chart.js** (+ adapter date-fns), **Leaflet 1.9.4**, oltre a:

- **twemoji@14.0.2** — emoji Unicode convertite in SVG per consistenza cross-browser.
  `twemoji.parse(container, TWEMOJI_OPTS)` va chiamato **dopo ogni update di `innerHTML`
  nel container `#app`** e una volta sull'`header` all'init. Il fix CSS
  `img.emoji { height:1em; width:1em; vertical-align:-0.1em; }` in `style.css` allinea gli
  SVG al testo circostante.
- **suncalc** — alba/tramonto (`SunCalc.getTimes`) e fase lunare
  (`SunCalc.getMoonIllumination().phase`) calcolati client-side dalle coordinate location.
  Fase lunare: 8 emoji (🌑→🌘) con tooltip nome in italiano. Aurora/crepuscolo civile
  non mostrati.
- **@meteocons/svg@0.1.0** (MIT) — icone meteo **animate** (SVG SMIL embedded, si
  riproducono dentro `<img src>` senza JS) per le sole condizioni `weather_code` WMO
  (hero, striscia giorni, dettaglio giorno). CDN:
  `https://cdn.jsdelivr.net/npm/@meteocons/svg@0.1.0/fill/<slug>.svg`. Renderizzate come
  `<img class="g-wicon g-wicon--{hero|strip|detail}">` — twemoji.parse le ignora
  (non sono Unicode). L'emoji twemoji resta in `onerror` come fallback. Tutte le altre
  icone (indicatori DLE, mini-stats, header, luna, alba/tramonto) restano twemoji.
