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
const STORAGE_KEY = 'cfb-spreads:manual-lines:v1';

const state = {
  data: null,
  accuracy: null,
  tab: 'spreads',
  search: '',
  tier: 'C',
  sort: 'edge',
  picksOnly: false,
  manual: loadManual(),
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

/** S(x) = P(home margin > x), from the half-integer grid the pipeline stores. */
function survivalAt(game, x) {
  const s = game.survival;
  if (!s || !s.table.length) return null;
  const idx = Math.round((x - s.first) / s.step);
  if (idx < 0) return 1;
  if (idx >= s.table.length) return 0;
  return s.table[idx];
}

/** Mirrors margin_model.cover_probabilities. */
function coverProbabilities(game, line) {
  let home, away, push;
  if (Math.abs(line - Math.round(line)) < 1e-9) {
    // Margins are integers, so P(M > n) == P(M > n + 0.5); a whole number can push.
    const n = Math.round(line);
    const above = survivalAt(game, n + 0.5);
    const below = survivalAt(game, n - 0.5);
    if (above === null || below === null) return null;
    home = above;
    push = Math.max(0, below - above);
    away = Math.max(0, 1 - below);
  } else {
    home = survivalAt(game, line);
    if (home === null) return null;
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

/** Mirrors predict.evaluate. Returns the same shape the pipeline stores. */
function evaluate(game, line) {
  if (game.tier === 'D' || !game.survival || !game.survival.table.length) {
    const why = (game.tier_reasons && game.tier_reasons.join('; ')) || 'unreadable ladder';
    return { side: null, team: null, line: line || 0, edge: 0, p_cover: null, p_push: 0,
             confidence: 'no-signal', reason: `Insufficient market: ${why}.` };
  }
  if (line === null || line === undefined || Number.isNaN(line)) {
    return { side: null, team: null, line: 0, edge: 0, p_cover: null, p_push: 0,
             confidence: 'no-play',
             reason: 'No Vegas line available — enter one to grade this game.' };
  }

  const probs = coverProbabilities(game, line);
  if (!probs) {
    return { side: null, team: null, line, edge: 0, p_cover: null, p_push: 0,
             confidence: 'no-signal', reason: 'Insufficient market.' };
  }

  // Shrinkage: reproduce the precision weighting for whatever line is in play.
  const w = game.shrink_weight === null || game.shrink_weight === undefined
    ? 1 : game.shrink_weight;
  const blended = w * game.implied_margin + (1 - w) * line;
  const edge = blended - line;

  if (game.margin_low <= line && line <= game.margin_high) {
    return { side: null, team: null, line, edge, p_cover: Math.max(probs.home, probs.away),
             p_push: probs.push, confidence: 'no-play',
             reason: `Line sits inside the market's own ${game.band.toFixed(1)}pt uncertainty band `
                   + `(${fmtSigned(game.margin_low)} to ${fmtSigned(game.margin_high)}); `
                   + 'no edge worth acting on.' };
  }

  if (Math.abs(edge) < MIN_EDGE_POINTS) {
    return { side: null, team: null, line, edge, p_cover: Math.max(probs.home, probs.away),
             p_push: probs.push, confidence: 'no-play',
             reason: `Market and line agree to within ${Math.abs(edge).toFixed(1)} pts. `
                   + `Anything under ${MIN_EDGE_POINTS} pt is inside the vig, so there is `
                   + 'nothing to bet here.' };
  }

  const distance = strikeDistance(game, line);
  const unpinned = distance > MAX_STRIKE_DISTANCE;
  const side = edge > 0 ? 'home' : 'away';
  const team = side === 'home' ? game.home_team : game.away_team;
  const pCover = side === 'home' ? probs.home : probs.away;
  const magnitude = Math.abs(edge);

  let confidence = magnitude < EDGE_SOLID || unpinned ? 'lean' : (magnitude < EDGE_STRONG ? 'solid' : 'strong');
  if (game.tier === 'C') confidence = 'lean';

  let reason = `Market implies ${marginText(blended, game)}; line is ${marginText(line, game)}. `
             + `At that number the market gives ${team} a ${(pCover * 100).toFixed(0)}% chance to cover. `
             + `Edge ${magnitude.toFixed(1)} pts.`;
  if (unpinned) {
    reason += ` Nearest constraining strike is ${distance.toFixed(1)} pts away, so treat with care.`;
  }
  if (w < 0.995) {
    reason += ` Blend: ${(w * 100).toFixed(0)}% Kalshi / ${((1 - w) * 100).toFixed(0)}% line prior.`;
  }
  reason += ` Take ${team} ${fmtBet(line, side)}.`;

  return { side, team, line, edge, p_cover: pCover, p_push: probs.push, confidence, reason };
}

/* ------------------------------------------------------------ formatting */

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

function renderAccuracy() {
  const box = document.getElementById('accuracy');
  box.textContent = '';
  const a = state.accuracy;

  const head = el('div', 'acc-head');
  const graded = a && a.graded_games ? a.graded_games : 0;

  if (!graded) {
    head.append(metric('Record', '—', 'no graded games yet'));
    box.append(head);
    const note = el('p', 'acc-empty',
      'No games have been graded yet. Predictions lock at Thursday noon ET and are graded once '
      + 'finals land, so this fills in after the first completed week — deliberately empty rather '
      + 'than showing an untested number.');
    box.append(note);
    return;
  }

  const last = a.last_week;
  const season = a.season.decision;
  head.append(metric('Last week', last ? recordText(last.decision) : '—',
                     last ? last.week_key : ''));
  head.append(metric('Season ATS', recordText(season),
                     season.pct !== null ? `${(season.pct * 100).toFixed(0)}%` : ''));
  if (a.drift && a.drift.median !== null) {
    head.append(metric('Wed → close drift', `${a.drift.median.toFixed(1)} pts`,
                       `same pick ${a.drift.same_pick}/${a.drift.same_pick_total}`));
  }
  const lock = el('div', 'acc-lock');
  lock.append(el('div', '', 'Locked at Thursday noon ET'));
  lock.append(el('div', '', `${graded} graded games`));
  head.append(lock);
  box.append(head);

  const rows = el('div', 'acc-rows');
  rows.append(chipRow('By edge', a.by_edge));
  rows.append(chipRow('By tier', a.by_tier));
  box.append(rows);
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

const TIER_ORDER = { A: 0, B: 1, C: 2, D: 3 };

function visibleGames() {
  const q = state.search.trim().toLowerCase();
  let games = state.data.games.filter((g) => TIER_ORDER[g.tier] <= TIER_ORDER[state.tier]);

  if (q) {
    games = games.filter((g) => `${g.home_team} ${g.away_team} ${g.title}`.toLowerCase().includes(q));
  }
  if (state.picksOnly) {
    games = games.filter((g) => {
      if (state.tab === 'moneyline') return g.moneyline && g.moneyline.significant;
      return evaluate(g, lineFor(g)).side !== null;
    });
  }

  const sorters = {
    edge: (a, b) => Math.abs(evaluate(b, lineFor(b)).edge) - Math.abs(evaluate(a, lineFor(a)).edge),
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

  for (const g of games) {
    board.append(state.tab === 'moneyline' ? moneylineCard(g) : spreadCard(g));
  }
}

/** A board of thirty "No play" cards should say why, not leave you scrolling. */
function boardSummary() {
  const all = state.data.games;

  if (state.tab === 'moneyline') {
    const n = all.filter((g) => g.moneyline && g.moneyline.significant).length;
    if (n) return null;
    const box = el('div', 'panel summary');
    box.append(el('strong', '', 'No moneyline divergences this week. '));
    box.append(document.createTextNode(
      "Kalshi's spread ladders and its own moneyline markets agree on every game in the slate."));
    return box;
  }

  const priced = all.filter((g) => lineFor(g) !== null);
  const picks = priced.filter((g) => evaluate(g, lineFor(g)).side !== null);
  if (picks.length || !priced.length) return null;

  const edges = priced.map((g) => Math.abs(evaluate(g, lineFor(g)).edge));
  const worst = Math.max(...edges);
  const diverged = all.filter((g) => g.moneyline && g.moneyline.significant).length;

  const box = el('div', 'panel summary');
  box.append(el('strong', '', 'Nothing clears the threshold this week. '));
  box.append(document.createTextNode(
    `Across ${priced.length} priced games the market and the line never disagree by more than `
    + `${worst.toFixed(1)} pts, and anything under ${MIN_EDGE_POINTS} pt is inside the vig. `
    + 'Two efficient markets agreeing is the normal case, not a failure — '
    + 'a board full of thin "edges" would be the thing to distrust.'));
  if (diverged) {
    box.append(document.createTextNode(' '));
    const link = el('button', 'linky',
      `${diverged} moneyline divergence${diverged === 1 ? '' : 's'} did show up though →`);
    link.type = 'button';
    link.addEventListener('click', () => document.querySelector('.tab[data-tab=moneyline]').click());
    box.append(link);
  }
  return box;
}

function gameHead(game) {
  const head = el('div', 'g-head');
  head.append(el('span', 'g-title', game.title));
  const meta = el('div', 'g-meta');
  const tier = el('span', `tier ${game.tier}`, `${game.tier} · ${game.tier_label}`);
  meta.append(tier);
  meta.append(el('span', '', `${(game.open_interest / 1000).toFixed(0)}k OI`));
  const when = fmtKick(game);
  if (when) meta.append(el('span', '', when));
  head.append(meta);
  return head;
}

function numBlock(k, v, sub, cls) {
  const n = el('div', 'num');
  n.append(el('span', 'k', k));
  n.append(el('span', `v${cls ? ' ' + cls : ''}`, v));
  if (sub) n.append(el('span', 'sub', sub));
  return n;
}

function spreadCard(game) {
  const line = lineFor(game);
  const pick = evaluate(game, line);
  const card = el('div', `game${pick.side ? ' actionable' : ''}`);
  card.append(gameHead(game));

  const nums = el('div', 'g-nums');
  if (game.implied_margin !== null) {
    nums.append(numBlock('Kalshi implies', marginText(game.implied_margin, game),
                         `band ${fmtSigned(game.margin_low)} to ${fmtSigned(game.margin_high)}`));
  } else {
    nums.append(numBlock('Kalshi implies', '—', 'ladder unreadable'));
  }
  nums.append(numBlock('Line', line === null ? '—' : marginText(line, game),
                       game.vegas_home_favored_by !== null && line === game.vegas_home_favored_by
                         ? `CFBD · ${game.vegas_books} book${game.vegas_books === 1 ? '' : 's'}`
                         : (line === null ? 'none set' : 'entered by hand')));
  if (line !== null && game.implied_margin !== null) {
    nums.append(numBlock('Edge', `${pick.edge >= 0 ? '+' : ''}${pick.edge.toFixed(1)} pts`, '',
                         pick.side ? (pick.edge > 0 ? 'pos' : 'neg') : ''));
    nums.append(numBlock('Cover chance', pct(pick.p_cover),
                         pick.p_push > 0.001 ? `push ${pct(pick.p_push)}` : ''));
  }
  nums.append(numBlock('Ladder', `${game.usable_strikes}/${game.total_strikes}`,
                       `${(game.fraction_traded * 100).toFixed(0)}% traded`));
  card.append(nums);

  const cls = pick.confidence === 'no-signal' ? 'nosignal' : (pick.side ? 'play' : 'noplay');
  const verdict = el('div', `verdict ${cls}`);
  const call = pick.side
    ? `${pick.team} ${fmtBet(line, pick.side)} · ${pick.confidence}`
    : (pick.confidence === 'no-signal' ? 'No signal' : 'No play');
  verdict.append(el('span', 'call', call), document.createTextNode(' — ' + pick.reason));
  card.append(verdict);

  card.append(manualRow(game));
  return card;
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
    const fresh = spreadCard(game);
    row.closest('.game').replaceWith(fresh);
    const nextInput = fresh.querySelector('.manual input');
    if (nextInput) {
      nextInput.focus();
      // Number inputs reject setSelectionRange; the caret already lands at the
      // end on focus, so this is only a nicety where it is supported.
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
      row.closest('.game').replaceWith(spreadCard(game));
    });
    row.append(clear);
  }
  return row;
}

function moneylineCard(game) {
  const m = game.moneyline;
  const card = el('div', `game${m && m.significant ? ' diverged' : ''}`);
  card.append(gameHead(game));

  if (!m) {
    card.append(el('div', 'verdict noplay', 'No moneyline market matched for this game.'));
    return card;
  }

  const grid = el('div', 'ml-grid');
  grid.append(numBlock(`${game.home_team} — from spread`,
                       pct(m.spread_win_prob), `${fmtOdds(m.spread_odds)} equivalent`));
  grid.append(numBlock(`${game.home_team} — moneyline`,
                       pct(m.ml_win_prob), `${fmtOdds(m.ml_odds)} quoted`));
  grid.append(numBlock('Ladder says', marginText(m.spread_margin, game), 'from the strike ladder'));
  grid.append(numBlock('Moneyline says',
                       m.ml_margin === null ? '—' : marginText(m.ml_margin, game),
                       m.ml_margin === null ? 'not comparable' : 'same distribution, shifted'));
  if (m.divergence_pts !== null) {
    grid.append(numBlock('Disagreement', `${fmtSigned(m.divergence_pts)} pts`,
                         `${(m.ml_open_interest / 1000).toFixed(0)}k ML OI`,
                         m.significant ? 'neg' : ''));
  }
  card.append(grid);

  const verdict = el('div', `verdict ${m.significant ? 'play' : 'noplay'}`);
  if (m.significant) verdict.append(el('span', 'flag', 'DIVERGENCE'), document.createTextNode(' '));
  verdict.append(document.createTextNode(m.note));
  card.append(verdict);
  return card;
}

/* ----------------------------------------------------------------- init */

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
      if (state.tab === 'moneyline' && state.sort === 'edge') {
        state.sort = 'divergence';
        document.getElementById('sort').value = 'divergence';
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
  renderAccuracy();
  bind();
  render();
}

main();
