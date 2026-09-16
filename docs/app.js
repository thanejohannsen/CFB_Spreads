/* CFB Spreads - reads the JSON the pipeline commits and renders the board.
 *
 * INVARIANT: `evaluate` below mirrors pipeline/predict.py:evaluate, so that a
 * spread typed here is judged by exactly the same rules as one graded in the
 * accuracy record. The rule set is deliberately tiny to keep that cheap. If you
 * change one side, change the other, and pipeline/tests pins the Python side.
 */
'use strict';

const MAX_STRIKE_DISTANCE = 3.0;   // pipeline/config.py
const MIN_EDGE_POINTS = 1.0;       // an edge under a point is inside the vig
const EDGE_SOLID = 2.0;
const EDGE_STRONG = 3.5;
const SCALE_RESIDUAL_FLAG = 0.055;  // pipeline/config.py
const STORAGE_KEY = 'cfb-spreads:manual-lines:v1';

// Market quality, keyed by tier letter. Mirrors TIERS in pipeline/config.py.
// Quality describes the *market* -- how precisely Kalshi is quoting this game.
// lean/solid/strong describe the *pick*. They get two visual languages, a grey
// ramp and the accent hue, so neither can be read as the other.
const QUALITY = {
  S: 'Deep market',
  A: 'Tight market',
  B: 'Readable market',
  C: 'Loose market',
  D: 'Unreadable market',
};

const LENSES = {
  master:        { label: 'Master',                        signal: null },
  kalshi_spread: { label: 'Kalshi Spread vs Vegas Spread',  signal: 'kalshi_spread' },
  kalshi_ml:     { label: 'Kalshi ML vs Spread',            signal: 'kalshi_ml' },
  sp_plus:       { label: 'SP+ vs Vegas Spread',            signal: 'sp_plus' },
};

const state = {
  data: null,
  accuracy: null,
  tab: 'master',
  search: '',
  tier: 'C',
  sort: 'edge',
  picksOnly: false,
  manual: loadManual(),
  picks: null,          // picks.json, fetched only when a record is expanded
  expanded: null,       // which record's pick list is open
};

/* ------------------------------------------------------------- storage -- */

function loadManual() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
  } catch (e) {
    return {};                       // private mode, cleared data, blocked storage
  }
}

function saveManual() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.manual));
  } catch (e) { /* non-fatal: the page still works, the value just won't persist */ }
}

/* --------------------------------------------------------------- model -- */

/** P(margin > x) for a distribution centred on `center`. Mirrors
 *  margin_model.margin_survival.
 *
 *  Closed form on two published numbers, which is the point: this used to read
 *  the stored survival table by nearest index while the pipeline evaluated its
 *  spline, so the same game's cover chance differed by up to 3.7 points between
 *  the page and the record. There is now no table and no interpolation for the
 *  two sides to disagree about. */
function marginSurvival(center, scale, x) {
  const z = (x - center) / Math.max(scale, 1e-9);
  if (z > 40) return 0;
  if (z < -40) return 1;
  return 1 / (1 + Math.exp(z));
}

/** Mirrors margin_model.cover_probabilities. */
function coverProbabilities(center, scale, line) {
  if (center === null || center === undefined || !scale) return null;
  let home, away, push;
  if (Math.abs(line - Math.round(line)) < 1e-9) {
    // Margins are integers, so P(M > n) == P(M > n + 0.5); a whole number can push.
    const n = Math.round(line);
    const above = marginSurvival(center, scale, n + 0.5);
    const below = marginSurvival(center, scale, n - 0.5);
    home = above;
    push = Math.max(0, below - above);
    away = Math.max(0, 1 - below);
  } else {
    home = marginSurvival(center, scale, line);
    push = 0;
    away = Math.max(0, 1 - home);
  }
  const total = home + away + push;
  if (total > 0) { home /= total; away /= total; push /= total; }
  return { home, away, push };
}

function strikeDistance(game, line) {
  if (!game.strikes || !game.strikes.length) return Infinity;
  return Math.min(...game.strikes.map((s) => Math.abs(s - line)));
}

/** One trailing period, not two -- mirrors predict._sentence. */
function sentence(text) {
  text = (text || '').trim();
  return /[.!?]$/.test(text) ? text : text + '.';
}

function signalOf(game, key) {
  return (game.signals || []).find((s) => s.key === key) || null;
}

/** The fair-spread estimate a given tab is working from.
 *  Master shrinks toward the line using its composite precision; the lenses use
 *  their raw estimate, because pulling a lens toward the very line it is being
 *  judged against would destroy what its record is meant to measure. */
function estimateFor(game, lens, line) {
  if (lens === 'master') {
    if (game.master == null || game.master.margin == null) return null;
    const w = game.master_shrink_weight == null ? 1 : game.master_shrink_weight;
    if (line === null || line === undefined || Number.isNaN(line)) return game.master.margin;
    return w * game.master.margin + (1 - w) * line;
  }
  const sig = signalOf(game, LENSES[lens].signal);
  return sig && sig.margin != null ? sig.margin : null;
}

/** Mirrors predict.evaluate. Returns the same shape the pipeline stores. */
function evaluate(game, line, lens) {
  lens = lens || 'master';
  const estimate = estimateFor(game, lens, line);
  const isMaster = lens === 'master';

  if (estimate === null) {
    const sig = isMaster ? null : signalOf(game, LENSES[lens].signal);
    const why = (sig && sig.note) || (game.tier_reasons && game.tier_reasons.join('; '))
                || 'no estimate available';
    return { side: null, team: null, line: line || 0, estimate: null, edge: 0, p_cover: null,
             p_push: 0, confidence: 'no-signal', reason: `Insufficient market: ${sentence(why)}` };
  }
  if (isMaster && game.tier === 'D') {
    const why = (game.tier_reasons && game.tier_reasons.join('; ')) || 'unreadable ladder';
    return { side: null, team: null, line: line || 0, estimate, edge: 0, p_cover: null,
             p_push: 0, confidence: 'no-signal', reason: `Insufficient market: ${sentence(why)}` };
  }
  if (line === null || line === undefined || Number.isNaN(line)) {
    return { side: null, team: null, line: 0, estimate, edge: 0, p_cover: null, p_push: 0,
             confidence: 'no-play',
             reason: 'No Vegas line available — enter one to grade this game.' };
  }

  const edge = estimate - line;

  // P(cover) puts the margin distribution on this estimate and reads the line
  // off it. The spread comes from a scale fitted across the whole ladder, not
  // the local slope at the number, which is 1c-tick noise. With no readable
  // ladder there is no distribution, so the pick stands on the edge alone and
  // the probability stays blank rather than made up.
  let probs = null;
  if (game.scale != null) {
    probs = coverProbabilities(estimate, game.scale, line);
  }
  const pPush = probs ? probs.push : 0;
  const best = probs ? Math.max(probs.home, probs.away) : null;

  if (isMaster && game.margin_low != null && game.margin_low <= line && line <= game.margin_high) {
    return { side: null, team: null, line, estimate, edge, p_cover: best, p_push: pPush,
             confidence: 'no-play',
             reason: `Line sits inside the market's own ${game.band.toFixed(1)}pt uncertainty band `
                   + `(${fmtSigned(game.margin_low)} to ${fmtSigned(game.margin_high)}); `
                   + 'no edge worth acting on.' };
  }

  if (Math.abs(edge) < MIN_EDGE_POINTS) {
    return { side: null, team: null, line, estimate, edge, p_cover: best, p_push: pPush,
             confidence: 'no-play',
             reason: `Market and line agree to within ${Math.abs(edge).toFixed(1)} pts. `
                   + `Anything under ${MIN_EDGE_POINTS} pt is inside the vig, so there is `
                   + 'nothing to bet here.' };
  }

  const side = edge > 0 ? 'home' : 'away';
  const team = side === 'home' ? game.home_team : game.away_team;
  const pCover = probs ? (side === 'home' ? probs.home : probs.away) : null;
  const distance = strikeDistance(game, line);
  const unpinned = distance > MAX_STRIKE_DISTANCE;
  const magnitude = Math.abs(edge);

  let confidence = magnitude < EDGE_SOLID || unpinned ? 'lean' : (magnitude < EDGE_STRONG ? 'solid' : 'strong');

  // A loose market cannot support a strong call however large the edge looks,
  // because the edge was measured against a number the market itself is unsure
  // of. Quality and strength read as separate things now, so a big edge showing
  // 'lean' would look like a contradiction unless the reason says why.
  const capped = isMaster && game.tier === 'C' && confidence !== 'lean';
  if (capped) confidence = 'lean';

  let reason = `Estimate ${marginText(estimate, game)}; line is ${marginText(line, game)}. `;
  if (pCover !== null) {
    reason += `At that number the market gives ${team} a ${(pCover * 100).toFixed(0)}% chance to cover. `;
  }
  reason += `Edge ${magnitude.toFixed(1)} pts.`;
  if (unpinned) reason += ` Nearest constraining strike is ${distance.toFixed(1)} pts away.`;
  if (capped) {
    reason += ' Capped at lean: a C-grade market cannot support a stronger call, whatever the edge.';
  }
  if (isMaster && game.master && game.master.label) reason += ` Blend: ${game.master.label}.`;
  reason += ` Take ${team} ${fmtBet(line, side)}.`;

  return { side, team, line, estimate, edge, p_cover: pCover, p_push: pPush, confidence, reason };
}

/* ------------------------------------------------------------ formatting */

/** Pick strength, as an escalating badge: lean outlined, solid tinted, strong filled. */
function strengthBadge(confidence) {
  return el('span', `strength ${confidence}`, confidence.toUpperCase());
}

const fmtSigned = (v) => (v > 0 ? '+' : '') + v.toFixed(1);

function marginText(margin, game) {
  if (Math.abs(margin) < 0.05) return "a pick'em";
  const team = margin > 0 ? game.home_team : game.away_team;
  return `${team} -${Math.abs(margin).toFixed(1)}`;
}

/** The number as the chosen side would actually be bet. */
function fmtBet(line, side) {
  const value = side === 'home' ? line : -line;
  return value === 0 ? 'PK' : fmtSigned(-value);
}

function fmtOdds(o) {
  if (o === null || o === undefined) return '—';
  return o > 0 ? `+${o}` : `${o}`;
}

const pct = (p) => (p === null || p === undefined ? '—' : `${(p * 100).toFixed(1)}%`);

/** Show a time only when one is actually known; otherwise just the date. */
function fmtKick(game) {
  if (game.kickoff_exact && game.kickoff) {
    const d = new Date(game.kickoff);
    if (!isNaN(d)) {
      return d.toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' });
    }
  }
  if (game.game_date) {
    const [y, m, day] = game.game_date.split('-').map(Number);
    const d = new Date(y, m - 1, day);
    if (!isNaN(d)) return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  }
  return '';
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* ------------------------------------------------------------- accuracy */

const LOCK_NOTE = {
  final: 'locked 1h before kickoff, when Kalshi volume peaks',
  decision: 'locked Thursday noon ET — the picks you could actually submit',
};

async function loadPicks() {
  if (state.picks) return state.picks;
  try {
    state.picks = await fetch('data/picks.json', { cache: 'no-store' }).then((r) => r.json());
  } catch (e) {
    state.picks = { entries: [] };
  }
  return state.picks;
}

function renderAccuracy() {
  const box = document.getElementById('accuracy');
  box.textContent = '';
  const a = state.accuracy;
  const graded = a && a.graded_games ? a.graded_games : 0;
  const records = (a && a.records) || [];
  const headline = records.find((r) => r.key === 'headline');

  const head = el('div', 'acc-head');
  if (!graded || !headline) {
    head.append(metric('Record', '—', 'no graded games yet'));
    box.append(head);
    box.append(el('p', 'acc-empty',
      'No games have been graded yet. Each record locks on its own clock and is graded once finals '
      + 'land, so this fills in after the first completed week — deliberately empty rather than '
      + 'showing an untested number.'));
    return;
  }

  head.append(metric(headline.label, recordText(headline.season),
                     headline.season.pct !== null ? `${(headline.season.pct * 100).toFixed(0)}%` : ''));
  if (a.drift && a.drift.median !== null) {
    head.append(metric('Thu → final drift', `${a.drift.median.toFixed(1)} pts`,
                       `same pick ${a.drift.same_pick}/${a.drift.same_pick_total}`));
  }
  const lock = el('div', 'acc-lock');
  lock.append(el('div', '', LOCK_NOTE.final));
  lock.append(el('div', '', `${graded} graded games`));
  head.append(lock);
  box.append(head);

  const table = el('div', 'acc-table');
  const hdr = el('div', 'acc-tr head');
  hdr.append(el('span', 'acc-td name', 'Record'), el('span', 'acc-td', 'Last week'),
             el('span', 'acc-td', 'Season'), el('span', 'acc-td', ''));
  table.append(hdr);

  for (const rec of records) {
    const row = el('button', `acc-tr clickable${rec.key === 'headline' ? ' master' : ''}`);
    row.type = 'button';
    row.setAttribute('aria-expanded', String(state.expanded === rec.key));
    const name = el('span', 'acc-td name');
    name.append(el('span', 'caret', state.expanded === rec.key ? '▾' : '▸'));
    name.append(document.createTextNode(' ' + rec.label));
    row.append(name);
    row.append(el('span', 'acc-td', rec.last_week ? recordText(rec.last_week) : '—'));
    row.append(el('span', 'acc-td', recordText(rec.season)));
    row.append(el('span', 'acc-td muted',
      rec.season.pct !== null ? `${(rec.season.pct * 100).toFixed(0)}%` : ''));
    row.addEventListener('click', async () => {
      state.expanded = state.expanded === rec.key ? null : rec.key;
      if (state.expanded) await loadPicks();
      renderAccuracy();
    });
    table.append(row);
    if (state.expanded === rec.key) table.append(pickList(rec));
  }
  box.append(table);

  const rows = el('div', 'acc-rows');
  rows.append(chipRow('Master by edge', a.by_edge));
  rows.append(chipRow('Master by market quality', a.by_tier));
  box.append(rows);
}

/** The individual picks behind one record. */
function pickList(rec) {
  const wrap = el('div', 'picks');
  const entries = ((state.picks && state.picks.entries) || []).filter((e) => e.record === rec.key);

  const note = el('div', 'picks-note');
  note.append(document.createTextNode(LOCK_NOTE[rec.lock] || ''));
  if (rec.tiers) note.append(document.createTextNode(` · ${rec.tiers.join('/')} markets only`));
  wrap.append(note);

  if (!entries.length) {
    wrap.append(el('div', 'picks-empty', 'No picks recorded for this record yet.'));
    return wrap;
  }

  for (const e of entries) {
    const row = el('div', `pick pick-${e.result}`);
    const when = e.kickoff ? new Date(e.kickoff) : null;
    row.append(el('span', 'pick-date',
      when && !isNaN(when) ? when.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''));

    const main = el('span', 'pick-main');
    main.append(el('span', 'pick-game', e.game || ''));
    const call = el('span', 'pick-call');
    call.append(document.createTextNode(`${e.team} ${fmtBet(e.line, e.side)} `));
    if (e.confidence) call.append(strengthBadge(e.confidence));
    main.append(call);

    const bits = [];
    if (e.tier) bits.push(QUALITY[e.tier] || `tier ${e.tier}`);
    if (e.edge !== null && e.edge !== undefined) bits.push(`edge ${fmtSigned(e.edge)}`);
    if (e.p_cover) bits.push(`${(e.p_cover * 100).toFixed(0)}% to cover`);
    if (e.minutes_before_kickoff !== null && e.minutes_before_kickoff !== undefined) {
      bits.push(`locked ${e.minutes_before_kickoff}m out`);
    }
    main.append(el('span', 'pick-meta', bits.join(' · ')));
    row.append(main);

    const out = el('span', 'pick-out');
    if (e.result === 'pending') {
      out.append(el('span', 'pick-res pending', 'PENDING'));
    } else {
      out.append(el('span', 'pick-res ' + e.result, e.result.toUpperCase()));
      if (e.home_margin !== null && e.home_margin !== undefined) {
        out.append(el('span', 'pick-margin', marginResult(e)));
      }
    }
    row.append(out);
    wrap.append(row);
  }
  return wrap;
}

/** "Miss St by 25" — the actual outcome, next to the number it was bet against. */
function marginResult(e) {
  const m = e.home_margin;
  if (m === 0) return 'tie';
  const winner = m > 0 ? e.home_team : e.away_team;
  return `${winner} by ${Math.abs(m)}`;
}

function metric(k, v, sub) {
  const m = el('div', 'acc-metric');
  m.append(el('span', 'k', k), el('span', 'v', v));
  if (sub) m.append(el('span', 'k', sub));
  return m;
}

function recordText(t) {
  if (!t || !t.total) return '—';
  return `${t.wins}-${t.losses}`;
}

function chipRow(label, groups) {
  const row = el('div', 'acc-row');
  row.append(el('span', 'label', label));
  const entries = Object.entries(groups || {}).filter(([, t]) => t && t.total);
  if (!entries.length) {
    row.append(el('span', 'muted', 'not enough graded games yet'));
    return row;
  }
  for (const [name, t] of entries) {
    const p = t.pct !== null ? ` ${(t.pct * 100).toFixed(0)}%` : '';
    row.append(el('span', 'chip', `${name} ${t.wins}-${t.losses}${p}`));
  }
  return row;
}

/* ---------------------------------------------------------------- board */

const TIER_ORDER = { S: 0, A: 1, B: 2, C: 3, D: 4 };

function visibleGames() {
  const q = state.search.trim().toLowerCase();
  let games = state.data.games.filter((g) => TIER_ORDER[g.tier] <= TIER_ORDER[state.tier]);

  if (q) {
    games = games.filter((g) => `${g.home_team} ${g.away_team} ${g.title}`.toLowerCase().includes(q));
  }
  if (state.picksOnly) {
    games = games.filter((g) => evaluate(g, lineFor(g), state.tab).side !== null);
  }

  const sorters = {
    edge: (a, b) => Math.abs(evaluate(b, lineFor(b), state.tab).edge)
                  - Math.abs(evaluate(a, lineFor(a), state.tab).edge),
    oi: (a, b) => b.open_interest - a.open_interest,
    kickoff: (a, b) => String(a.kickoff || '').localeCompare(String(b.kickoff || '')),
    divergence: (a, b) => Math.abs((b.moneyline && b.moneyline.divergence_pts) || 0)
                        - Math.abs((a.moneyline && a.moneyline.divergence_pts) || 0),
  };
  return games.slice().sort(sorters[state.sort] || sorters.edge);
}

function lineFor(game) {
  const manual = state.manual[game.id];
  if (manual !== undefined && manual !== null && manual !== '') {
    const v = parseFloat(manual);
    if (!Number.isNaN(v)) return v;
  }
  const v = game.vegas_home_favored_by;
  return v === null || v === undefined ? null : v;
}

function render() {
  const board = document.getElementById('board');
  board.textContent = '';
  const games = visibleGames();
  document.getElementById('empty').hidden = games.length > 0;

  const summary = boardSummary();
  if (summary) board.append(summary);

  for (const g of games) board.append(gameCard(g, state.tab));
}

/** A board of "No play" cards should say why, not leave you scrolling. */
function boardSummary() {
  const all = state.data.games;
  const priced = all.filter((g) => lineFor(g) !== null);
  const picks = priced.filter((g) => evaluate(g, lineFor(g), state.tab).side !== null);
  if (picks.length || !priced.length) return null;

  const edges = priced.map((g) => Math.abs(evaluate(g, lineFor(g), state.tab).edge));
  const worst = Math.max(...edges);
  const box = el('div', 'panel summary');
  box.append(el('strong', '', `Nothing clears the threshold on this tab. `));
  box.append(document.createTextNode(
    `Across ${priced.length} priced games this signal and the line never disagree by more than `
    + `${worst.toFixed(1)} pts, and anything under ${MIN_EDGE_POINTS} pt is inside the vig. `
    + 'Two efficient markets agreeing is the normal case, not a failure.'));

  const others = Object.keys(LENSES).filter((k) => k !== state.tab)
    .map((k) => [k, all.filter((g) => evaluate(g, lineFor(g), k).side !== null).length])
    .filter(([, n]) => n > 0);
  if (others.length) {
    box.append(document.createTextNode(' '));
    for (const [k, n] of others) {
      const link = el('button', 'linky', `${LENSES[k].label}: ${n} →`);
      link.type = 'button';
      link.addEventListener('click', () => document.querySelector(`.tab[data-tab=${k}]`).click());
      box.append(link, document.createTextNode(' '));
    }
  }
  return box;
}

function gameHead(game) {
  const head = el('div', 'g-head');
  head.append(el('span', 'g-title', game.title));
  const meta = el('div', 'g-meta');
  meta.append(el('span', `quality ${game.tier}`,
                 `${game.tier} · ${game.tier_label || QUALITY[game.tier] || 'market'}`));
  meta.append(el('span', '', `${(game.open_interest / 1000).toFixed(0)}k OI`));
  const when = fmtKick(game);
  if (when) meta.append(el('span', '', when));
  head.append(meta);
  return head;
}

function numBlock(k, v, sub, cls, flagSub) {
  const n = el('div', 'num');
  n.append(el('span', 'k', k));
  n.append(el('span', `v${cls ? ' ' + cls : ''}`, v));
  if (sub) n.append(el('span', `sub${flagSub ? ' flagged' : ''}`, sub));
  return n;
}

/** One card, rendered for whichever tab is active. */
function gameCard(game, lens) {
  const line = lineFor(game);
  const pick = evaluate(game, line, lens);
  const card = el('div', `game${pick.side ? ' actionable str-' + pick.confidence : ''}`);
  card.append(gameHead(game));

  if (lens === 'master') card.append(signalTable(game));
  if (lens === 'kalshi_ml') card.append(divergenceRow(game));
  if (lens === 'sp_plus') card.append(spPlusRow(game));

  const nums = el('div', 'g-nums');
  nums.append(numBlock(lens === 'master' ? 'Master estimate' : 'This signal says',
                       pick.estimate === null ? '—' : marginText(pick.estimate, game),
                       lens === 'master' && game.master ? game.master.label : ''));
  nums.append(numBlock('Line', line === null ? '—' : marginText(line, game),
                       game.vegas_home_favored_by !== null && line === game.vegas_home_favored_by
                         ? `CFBD · ${game.vegas_books} book${game.vegas_books === 1 ? '' : 's'}`
                         : (line === null ? 'none set' : 'entered by hand')));
  if (line !== null && pick.estimate !== null) {
    nums.append(numBlock('Edge', `${pick.edge >= 0 ? '+' : ''}${pick.edge.toFixed(1)} pts`, '',
                         pick.side ? (pick.edge > 0 ? 'pos' : 'neg') : ''));
    nums.append(numBlock('Cover chance', pct(pick.p_cover),
                         pick.p_push > 0.001 ? `push ${pct(pick.p_push)}` : ''));
  }
  // The fitted curve is two parameters standing in for a whole market. Say how
  // far that stand-in sits from the strikes, so a badly shaped game is visible
  // rather than buried. Every game deviates a little; only the worst are flagged.
  const misfit = game.scale_residual;
  let ladderSub = `${(game.fraction_traded * 100).toFixed(0)}% traded`;
  if (misfit != null) ladderSub += ` · fit ${(misfit * 100).toFixed(1)}¢`;
  nums.append(numBlock('Ladder', `${game.usable_strikes}/${game.total_strikes}`, ladderSub,
                       '', misfit != null && misfit > SCALE_RESIDUAL_FLAG));
  card.append(nums);

  const cls = pick.confidence === 'no-signal' ? 'nosignal' : (pick.side ? 'play' : 'noplay');
  const verdict = el('div', `verdict ${cls}`);
  if (pick.side) {
    verdict.append(el('span', 'call', `${pick.team} ${fmtBet(line, pick.side)}`),
                   strengthBadge(pick.confidence));
  } else {
    verdict.append(el('span', 'call', pick.confidence === 'no-signal' ? 'No signal' : 'No play'));
  }
  verdict.append(document.createTextNode(' — ' + pick.reason));
  card.append(verdict);

  card.append(manualRow(game));
  return card;
}

/** The three signals side by side, with the precision that sets their weight. */
function signalTable(game) {
  const wrap = el('div', 'signals');
  for (const sig of game.signals || []) {
    const row = el('div', 'sig');
    const share = game.master && game.master.shares ? game.master.shares[sig.key] : undefined;
    row.append(el('span', 'sig-name', sig.label));
    row.append(el('span', 'sig-val', sig.margin === null ? '—' : marginText(sig.margin, game)));
    row.append(el('span', 'sig-sd', sig.sigma === null ? '' : `±${sig.sigma.toFixed(2)} pts`));
    row.append(el('span', 'sig-w',
      share === undefined ? (sig.key === 'sp_plus' ? 'lens only' : 'unused')
                          : `${share}% of master`));
    wrap.append(row);
  }
  return wrap;
}

function divergenceRow(game) {
  const m = game.moneyline;
  const wrap = el('div', 'ml-grid');
  if (!m) {
    wrap.append(numBlock('Moneyline', '—', 'no matching market'));
    return wrap;
  }
  wrap.append(numBlock(`${game.home_team} — from spread`, pct(m.spread_win_prob),
                       `${fmtOdds(m.spread_odds)} equivalent`));
  wrap.append(numBlock(`${game.home_team} — moneyline`, pct(m.ml_win_prob),
                       `${fmtOdds(m.ml_odds)} quoted`));
  wrap.append(numBlock('Ladder says', marginText(m.spread_margin, game), 'from the strike ladder'));
  wrap.append(numBlock('Moneyline says', m.ml_margin === null ? '—' : marginText(m.ml_margin, game),
                       m.ml_band != null ? `±${(m.ml_band / 2).toFixed(2)} pts` : 'not comparable'));
  if (m.divergence_pts !== null) {
    wrap.append(numBlock('Disagreement', `${fmtSigned(m.divergence_pts)} pts`,
                         m.significant ? 'beyond both bands' : 'within both bands',
                         m.significant ? 'neg' : ''));
  } else if (m.divergence_prob !== null && m.divergence_prob !== undefined) {
    // The conversion to points failed, but the two markets still disagree.
    // Show it in the units that survive rather than dropping the comparison,
    // which is the whole point of this tab.
    wrap.append(numBlock('Disagreement', `${fmtSigned(m.divergence_prob * 100)}%`,
                         'win probability — no spread equivalent'));
  }
  // Why a number is missing is worth as much as the number would have been.
  if (m.ml_margin === null && m.note) wrap.append(el('p', 'ml-note', m.note));
  return wrap;
}

function spPlusRow(game) {
  const wrap = el('div', 'ml-grid');
  const sp = game.sp_plus;
  if (!sp) {
    wrap.append(numBlock('SP+', '—',
      state.data.sp_plus_available ? 'no rating for one of these teams' : 'ratings unavailable'));
    return wrap;
  }
  wrap.append(numBlock(`${game.home_team} SP+`, sp.home_rating.toFixed(1), 'points above average'));
  wrap.append(numBlock(`${game.away_team} SP+`, sp.away_rating.toFixed(1), 'points above average'));
  wrap.append(numBlock('Home field', sp.neutral_site ? 'neutral' : `+${sp.home_field.toFixed(1)}`,
                       sp.neutral_site ? 'no adjustment' : 'added to the home side'));
  wrap.append(numBlock('SP+ spread', marginText(sp.home_favored_by, game), 'rating gap + home field'));
  return wrap;
}

function manualRow(game) {
  const row = el('div', 'manual');
  row.append(el('span', '', 'Your line (home team gives):'));

  const input = el('input');
  input.type = 'number';
  input.step = '0.5';
  input.placeholder = game.vegas_home_favored_by !== null
    ? String(game.vegas_home_favored_by) : 'e.g. -7.5';
  input.value = state.manual[game.id] ?? '';
  input.setAttribute('aria-label', `Spread for ${game.title}`);
  if (input.value !== '') input.classList.add('set');

  input.addEventListener('input', () => {
    if (input.value === '') delete state.manual[game.id];
    else state.manual[game.id] = input.value;
    saveManual();
    const fresh = gameCard(game, state.tab);
    row.closest('.game').replaceWith(fresh);
    const nextInput = fresh.querySelector('.manual input');
    if (nextInput) {
      nextInput.focus();
      try { nextInput.setSelectionRange(nextInput.value.length, nextInput.value.length); }
      catch (e) { /* type="number" in Chrome and Safari */ }
    }
  });
  row.append(input);
  row.append(el('span', 'src',
    `positive = ${game.home_team} favoured, negative = ${game.away_team} favoured`));

  if (state.manual[game.id] !== undefined) {
    const clear = el('button', '', 'clear');
    clear.type = 'button';
    clear.addEventListener('click', () => {
      delete state.manual[game.id];
      saveManual();
      row.closest('.game').replaceWith(gameCard(game, state.tab));
    });
    row.append(clear);
  }
  return row;
}

/* ----------------------------------------------------------------- init */

/** Say so when the Vegas side of the data did not land.
 *
 * A whole slate failing to match used to be invisible: every card just read
 * "No Vegas line available", which looks identical to a quiet week. The count
 * sat in the JSON unread. */
function renderDataWarning() {
  const box = document.getElementById('datawarning');
  box.textContent = '';
  const d = state.data;
  if (!d.cfbd_available) return;

  const priced = d.matched_games;
  if (priced === undefined || priced === null) return;
  const total = d.slate_size || d.games.length;
  if (priced >= Math.max(1, total * 0.5)) return;

  const panel = el('div', 'panel warning');
  panel.append(el('strong', '', priced === 0
    ? 'No Vegas lines matched this week. '
    : `Only ${priced} of ${total} games matched a Vegas line. `));

  const bits = [];
  if (d.cfb_week) bits.push(`CFBD was asked for week ${d.cfb_week}`);
  if (d.slate_date) bits.push(`the slate is played ${d.slate_date}`);
  panel.append(document.createTextNode(
    (bits.length ? bits.join(' but ') + '. ' : '')
    + 'Lines, edges and grading are unreliable until this is resolved — '
    + 'the Kalshi numbers below are unaffected, and you can still enter '
    + 'spreads by hand.'));
  box.append(panel);
}

function renderHeader() {
  const d = state.data;
  const gen = new Date(d.generated_at);
  document.getElementById('generated').textContent =
    `${d.slate_size} games · updated ${isNaN(gen) ? d.generated_at : gen.toLocaleString()}`;

  const dl = new Date(d.decision_deadline);
  const node = document.getElementById('deadline');
  if (!isNaN(dl)) {
    const hours = (dl - Date.now()) / 36e5;
    node.textContent = hours > 0
      ? `picks due in ${hours < 48 ? `${Math.round(hours)}h` : `${Math.round(hours / 24)}d`}`
      : 'past this week’s lock';
  }
  if (!d.cfbd_available) {
    node.textContent += ' · no CFBD key: enter lines by hand';
  }
}

function bind() {
  document.getElementById('search').addEventListener('input', (e) => {
    state.search = e.target.value; render();
  });
  document.getElementById('tier').addEventListener('change', (e) => {
    state.tier = e.target.value; render();
  });
  document.getElementById('sort').addEventListener('change', (e) => {
    state.sort = e.target.value; render();
  });
  document.getElementById('picksonly').addEventListener('change', (e) => {
    state.picksOnly = e.target.checked; render();
  });
  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => {
      state.tab = tab.dataset.tab;
      for (const t of document.querySelectorAll('.tab')) {
        const on = t === tab;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', String(on));
      }
      render();
    });
  }
}

async function main() {
  try {
    const [cur, acc] = await Promise.all([
      fetch('data/current.json', { cache: 'no-store' }).then((r) => r.json()),
      fetch('data/accuracy.json', { cache: 'no-store' }).then((r) => r.json()).catch(() => null),
    ]);
    state.data = cur;
    state.accuracy = acc;
  } catch (e) {
    document.getElementById('generated').textContent = 'could not load data';
    document.getElementById('board').append(
      el('p', 'empty', 'Data failed to load. The updater may not have run yet.'));
    return;
  }
  renderHeader();
  renderDataWarning();
  renderAccuracy();
  bind();
  render();
}

main();
