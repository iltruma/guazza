---
name: Guazza
description: ML weather forecasting dashboard for Tuscan microclimates
colors:
  surface-base: "#0C1210"
  surface-panel: "#121815"
  surface-raised: "#172019"
  surface-deep: "#1D2823"
  accent: "#3BA4C2"
  text-primary: "oklch(92% 0.012 160)"
  verde: "#34D399"
  giallo: "#FBBF24"
  rosso: "#F87171"
typography:
  display:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "clamp(3.5rem, 9vw, 5rem)"
    fontWeight: 700
    lineHeight: 0.9
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "clamp(1.75rem, 4vw, 2.5rem)"
    fontWeight: 700
    lineHeight: 1.05
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "clamp(1.6rem, 3.5vw, 2.2rem)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "10px"
    fontWeight: 700
    letterSpacing: "0.12em"
  data:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 600
    letterSpacing: "normal"
    fontFeature: "tnum"
rounded:
  sm: "4px"
  md: "8px"
  lg: "12px"
  xl: "16px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  tab-active:
    backgroundColor: "rgba(59,164,194,0.10)"
    textColor: "{colors.accent}"
    rounded: "{rounded.md}"
    padding: "5px 12px"
    typography: "label"
  tab-default:
    backgroundColor: "transparent"
    textColor: "oklch(92% 0.012 160 / 0.48)"
    rounded: "{rounded.md}"
    padding: "5px 12px"
    typography: "label"
  pill-verde:
    backgroundColor: "rgba(52,211,153,0.10)"
    textColor: "{colors.verde}"
    rounded: "8px"
    padding: "8px 12px"
  pill-giallo:
    backgroundColor: "rgba(251,191,36,0.10)"
    textColor: "{colors.giallo}"
    rounded: "8px"
    padding: "8px 12px"
  pill-rosso:
    backgroundColor: "rgba(248,113,113,0.10)"
    textColor: "{colors.rosso}"
    rounded: "8px"
    padding: "8px 12px"
  card:
    backgroundColor: "{colors.surface-panel}"
    rounded: "{rounded.xl}"
    padding: "20px 24px"
---

# Design System: Guazza

## 1. Overview

**Creative North Star: "La Pietra Toscana"**

Guazza è uno strumento scientifico personale, non una consumer app. Il design rispecchia questo: superfici opache che ricordano la pietra serena bagnata di nebbia mattutina, tinte di verde-grigio mineral che non imitano il cielo ma la terra, un unico accento petrol-blue che si riserva per i dati vivi — la temperatura corrente, lo stato attivo, il radar che pulsa. L'oscurità non è "dark mode": è il buio di chi guarda fuori dalla finestra alle 7 di mattina per decidere se prendere il motorino.

La doppia velocità è il principio organizzativo centrale. Ogni sezione funziona a 3 secondi (indicatori DLE a colori, temperatura hero, CI bar) e a 30 secondi (tabella NWP, confronto modelli, coverage empirica). La gerarchia visiva non è decorativa: il dato operativo è sempre più grande del dato tecnico, sempre.

L'interfaccia rifiuta esplicitamente tre categorie: il cheerful consumer-weather con icone a colori e pubblicità, il crypto-dark con neon e glow eccessivo, il SaaS analytics con hero-metric grid identici e blu navy piatto. Ogni scelta di colore, spaziatura e motion qui è una presa di posizione contro quei pattern.

**Key Characteristics:**
- Verde-grigio mineral con 4 livelli di superficie, zero neri puri
- Accento petrol-blue riservato agli elementi vivi (LIVE pulse, tab attivo, accent hero, CI bar)
- Doppia tipografia: Geist per display e titoli, JetBrains Mono per tutti i dati numerici
- Elevazione tonal, senza ombre — la profondità viene dalla differenza di superficie
- Grain overlay (`opacity: 0.022`, `mix-blend-mode: overlay`) che rompe la piattezza digitale senza essere visibile direttamente
- Animazioni con scopo preciso: CI expand, fade-up, sonar radar — nessuna ornamentale

## 2. Colors: The Tuscan Mineral Palette

Quattro livelli di pietra, un accento acqua, tre segnali operativi.

### Primary
- **Petrol Blue** (`#3BA4C2`): l'unico colore saturato del sistema. Usato esclusivamente per elementi che comunicano stato attivo o dati vivi: tab selezionato, pulse dot LIVE, linea accent in cima all'hero band, CI bar range 80%, mediana CI bar, radar sonar ring, slider radar. Il suo background tint è `rgba(59,164,194,0.10)`, il suo bordo semitrasparente è `rgba(59,164,194,0.35)`.

### Secondary
- **Verde Operativo** (`#34D399`): segnale "sicuro, procedi". Usato esclusivamente nei pill indicatore DLE e nei valori qualità aria nella fascia bassa. Background tint `rgba(52,211,153,0.10)`, bordo `rgba(52,211,153,0.22)`.
- **Giallo Cautela** (`#FBBF24`): segnale intermedio. Stessa struttura del verde: tint bg + bordo semitrasparente. Solo nei pill DLE e AQ.
- **Rosso Allerta** (`#F87171`): segnale critico. Solo nei pill DLE, valori AQ alti, error state. Background `rgba(248,113,113,0.10)`.

### Neutral
- **Pietra Base** (`#0C1210`): fondo del body. Verde-grigio scurissimo, non nero. Punto zero della scala.
- **Pietra Panel** (`#121815`): superficie di card, hero band, collapsibili. Il livello 1 della scala.
- **Pietra Rialzata** (`#172019`): hover state dei day card, riga Guazza nella NWP table, strip cell background. Livello 2.
- **Pietra Profonda** (`#1D2823`): hover della riga Guazza in NWP, superficie più elevata. Livello 3.
- **Testo Primario** (`oklch(92% 0.012 160)`): bianco caldo leggermente tinto di verde. Tutti i valori importanti, titoli, temperatura hero.
- **Testo Secondario** (`oklch(92% 0.012 160 / 0.62)`): testo descrittivo, date, valori NWP non-Guazza.
- **Testo Terziario** (`oklch(92% 0.012 160 / 0.48)`): label uppercase mono, kicker, unità, metadati. Solo informazioni contestuali.
- **Bordo Sottile** (`rgba(255,255,255,0.07)`): divisori tra celle, bordi card. Quasi invisibile.
- **Bordo Medio** (`rgba(255,255,255,0.10)`): bordo su elementi in stato active/hover.

### Named Rules
**The One Accent Rule.** Il Petrol Blue (`#3BA4C2`) appare su meno del 15% di qualsiasi schermata. Il suo utilizzo esclusivo agli elementi "vivi" ne fa un segnale, non una decorazione. Usarlo per ornamento svuota il sistema.

**The Mineral Progression Rule.** I quattro livelli superficie si usano in ordine stretto: surface-0 (body) → surface-1 (card) → surface-2 (hover/elevated) → surface-3 (deepest hover). Non saltare livelli. Non inventare un quinto senza aggiornare la scala.

## 3. Typography

**Display Font:** Geist (con `system-ui, sans-serif` come fallback)
**Data Font:** JetBrains Mono (con `ui-monospace, monospace` come fallback)

**Character:** Il pairing è funzionale, non estetico. Geist porta i numeri grandi e i titoli con personalità moderna. JetBrains Mono prende tutto il resto: ogni valore numerico, ogni label uppercase, ogni timestamp. La distinzione è assoluta: se è un dato, è mono.

### Hierarchy
- **Display** (700, `clamp(3.5rem, 9vw, 5rem)`, line-height 0.9, tracking -0.025em): temperatura hero corrente. Solo qui.
- **Headline** (700, `clamp(1.75rem, 4vw, 2.5rem)`, line-height 1.05, tracking -0.02em): titolo del day detail (nome giorno).
- **Title** (700, `clamp(1.6rem, 3.5vw, 2.2rem)`, line-height 1, tracking -0.02em, tabular-nums): valori metrici nel bento (Tmin, Tmax, precip).
- **Body** (400, 14px, line-height 1.5): testo descrittivo, condizione meteo testuale. Max 65ch.
- **Label** (JetBrains Mono, 10-11px, weight 700, uppercase, tracking 0.10-0.14em, `text-3`): kicker di sezione, intestazioni colonna, unità. Non è il dato; è il contenitore del dato.
- **Data** (JetBrains Mono, 12-13px, weight 600, tabular-nums): valori numerici NWP table, stat strip, hero stats.

### Named Rules
**The Mono-for-Numbers Rule.** Se il valore è numerico, è JetBrains Mono con `font-variant-numeric: tabular-nums`. Nessuna eccezione. Un numero in Geist è un errore di sistema.

## 4. Elevation

Questo sistema è flat per principio. La profondità viene interamente dalla progressione di superficie (`surface-0` → `surface-3`) e dai bordi semitrasparenti. Nessuna `box-shadow` sui componenti a riposo.

Due sole eccezioni, entrambe funzionali: il chart tooltip (`0 8px 32px rgba(8,10,24,0.5)`) e l'indicator tooltip (`0 8px 32px rgba(8,10,24,0.6)`) usano ombra profonda perché devono galleggiare sopra al contenuto. In entrambi i casi l'ombra è scura, profonda (32px blur), senza spread — l'opposto del glow decorativo.

Il grain overlay sul body (`opacity: 0.022`, `mix-blend-mode: overlay`) rompe la piattezza digitale senza essere percettibile come texture esplicita.

### Named Rules
**The Flat-By-Default Rule.** Le superfici non hanno ombra a riposo. L'ombra appare solo quando un elemento deve galleggiare sul contenuto (tooltip overlay). Hover non aggiunge shadow: aggiunge `surface-raised` come background.

## 5. Components

### Navigation Tabs
Tono rarefatto, reazione precisa. Il tab default è quasi invisibile fino all'interazione.
- **Shape:** 8px (`--r-md`)
- **Default:** Background trasparente, testo `text-3`, bordo trasparente
- **Hover:** Background `rgba(255,255,255,0.05)`, testo `text-2`
- **Active:** Background `rgba(59,164,194,0.10)`, bordo `rgba(59,164,194,0.30)`, testo accent, weight 600
- **Tipografia:** JetBrains Mono 12px, weight 500 default → 600 active
- **Touch target:** min-height 36px su dispositivi touch

### Indicator Pills (DLE Verdict)
Il componente di risposta operativa primaria. Griglia 2-colonna con icona a sinistra (span 2 righe) e label + verdict a destra.
- **Shape:** 8px radius
- **Verde/Giallo/Rosso:** Background tinted 10% + bordo 22% del colore segnale. Verdict text usa il colore pieno.
- **Hover:** `opacity: 0.82` — nessun cambio di colore, solo attenuazione
- **Press:** `scale(0.95)` — feedback tattile immediato
- **Width:** Full-width nel contesto DLE grid

### Day Strip Cards
- **Shape:** 12px (`--r-lg`)
- **Default:** `surface-1` background, `border-1` border
- **Hover:** `surface-2` background, `translateY(-2px)` lift
- **Active:** `surface-2` + `border-2` + linea accent 2px in basso, centrata, 28px di larghezza
- **Width:** 116px fisso su mobile, flex su ≥768px

### CI Bar (Confidence Interval)
Componente signature: visualizza due range sovrapposti (90% e 80%) con mediana animata.
- **Track:** 6px, `rgba(255,255,255,0.07)`, border-radius 3px
- **Range 90%:** `rgba(255,255,255,0.10)` — il range esterno, quasi invisibile
- **Range 80%:** `rgba(59,164,194,0.35)` — il range principale, visibile
- **Median dot:** 10px circle, border 2px accent su `surface-1`
- **Animazione:** `ci-expand` 400ms ease-out per i range, `ci-pop` 400ms spring per il dot

### NWP Comparison Table
- **Header:** Mono 10px uppercase, tracking 0.10em, `text-3`, border-bottom
- **Row default:** 9px 12px padding, `text-2`, border-top, hover `rgba(255,255,255,0.025)`
- **Row Guazza:** Background `surface-2`, `text-1`, weight 600, nome in accent
- **Delta chips:** 4px radius, 10px, inline — warm orange, cold blue, wet rosso, dry verde
- **Mobile:** Prima colonna sticky left

### Collapsible Panels (Radar, AQ)
- **Shape:** 16px (`--r-xl`), overflow hidden
- **Header hover:** `rgba(255,255,255,0.02)` — quasi invisibile per non sovrastare il contenuto
- **Chevron:** Rotazione 180° su `details[open]`, `var(--dur-base)` ease-out

### Chart Tooltip
- **Background:** `rgba(18,24,21,0.97)` — quasi opaco, tinto di verde come le superfici
- **Shadow:** `0 8px 32px rgba(8,10,24,0.5)` — l'unica ombra profonda del sistema
- **Blur:** `backdrop-filter: blur(12px)` — glassmorphism funzionale, non decorativo

## 6. Do's and Don'ts

### Do:
- **Do** usare JetBrains Mono con `font-variant-numeric: tabular-nums` per ogni valore numerico, senza eccezioni.
- **Do** mantenere l'accento `#3BA4C2` riservato a stati vivi e dati in tempo reale. Il valore viene dalla rarità.
- **Do** comunicare la profondità cambiando livello di superficie (`surface-1` → `surface-2`), non aggiungendo shadow su hover.
- **Do** usare `clamp()` per le dimensioni dei display numerici — la temperatura hero scala da 3.5rem a 5rem fluidamente.
- **Do** animare con scopo: CI expand, fade-up, sonar radar hanno funzione. Animazioni ornamentali vanno rimosse.
- **Do** mostrare l'incertezza: CI bar, lead time badge, coverage empirica sono obbligatori. L'onestà visiva è un principio di progetto, non un'opzione.
- **Do** rispettare `prefers-reduced-motion`: tutte le animazioni a `0.01ms` via media query. Già implementato nel CSS base.

### Don't:
- **Don't** usare `border-left` o `border-right` colorato come accent su card o list item. Le uniche linee accent sono orizzontali (bottom del card active, top dell'hero band). La stripe laterale è un pattern SaaS da evitare esplicitamente.
- **Don't** usare gradient text (`background-clip: text`). I valori importanti sono testo solido; il gradiente non aggiunge informazione.
- **Don't** imitare Weather.com o 3BMeteo: icone consumer coloratissime, palette cheerful, tono positivo che nasconde l'incertezza. Questo sistema è per chi vuole i dati grezzi.
- **Don't** imitare il crypto/NFT dark UI: neon su nero, glow eccessivo, glassmorphism di default. Il blur esiste solo nel chart tooltip e nel radar controls — elementi che devono galleggiare sul contenuto.
- **Don't** costruire hero metric + KPI card grid identici. Il SaaS analytics dashboard (blu navy, grigi piatti, zero personalità) è anti-reference esplicito.
- **Don't** aggiungere un quinto livello di superficie senza aggiornare la scala `--surface-*`. Un `#222` a caso rompe la progressione mineral green.
- **Don't** usare Geist per valori numerici tabellari. La distinzione sans/mono è semantica: Geist è testo, mono è dato.
- **Don't** colorare elementi neutri con l'accento petrol blue. Se è accent, è perché quel dato è vivo o quello stato è attivo.
