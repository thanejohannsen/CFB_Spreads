#!/usr/bin/env node
/* Pins the INVARIANT declared at the top of docs/app.js and pipeline/predict.py:
 * the page and the pipeline judge a spread by the same rules, so a pick shown on
 * the board is the pick the accuracy record grades.
 *
 * Nothing checked that claim, and it quietly stopped being true. The pipeline
 * decided from full-precision floats and published rounded ones, so wherever a
 * number landed within half a unit of the last published decimal of a threshold
 * the two disagreed -- the board reading "No play" on a game the record counted
 * as a pick. A comment asserting two implementations agree is worth nothing
 * unless something fails when they stop.
 *
 * This loads the real docs/app.js -- not a copy of its logic, which would just
 * be a third implementation to drift -- and replays its own evaluate() over
 * every game in the committed current.json, for all four lenses, against the
 * pick the pipeline stored. Any disagreement exits non-zero.
 *
 * The same argument covers the execution box, which mirrors pipeline/execution.py
 * so it can price a hand-typed line in the browser. There the pipeline stores no
 * answer to compare against, so this asks it for one: `python -m pipeline.execution`
 * emits the venue rows for every stored pick and app.js's own venues() is replayed
 * against them. A tenth of a cent of drift there picks the wrong venue.
 *
 *   node tools/conformance.js [path/to/current.json]
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const APP = path.join(ROOT, 'docs', 'app.js');
const DATA = process.argv[2] || path.join(ROOT, 'docs', 'data', 'current.json');
const LENSES = ['master', 'kalshi_spread', 'kalshi_ml', 'sp_plus'];

/** Load app.js as a library: strip its browser entry point and stub the DOM.
 *  The stubs only need to survive definition -- nothing here renders. */
function loadApp() {
  const src = fs.readFileSync(APP, 'utf8');
  const headless = src.replace(/\bmain\(\);\s*$/, '');
  if (headless === src) {
    throw new Error('app.js no longer ends in main(); update this loader');
  }
  const node = () => ({ style: {}, append() {}, addEventListener() {},
                        classList: { add() {} }, setAttribute() {} });
  const context = vm.createContext({
    console,
    document: {
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: node,
      addEventListener() {},
    },
    window: {},
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: () => Promise.reject(new Error('conformance run does not fetch')),
  });
  vm.runInContext(headless, context, { filename: 'docs/app.js' });
  return context;
}

function main() {
  if (!fs.existsSync(DATA)) {
    console.error(`no data at ${DATA} -- nothing to check`);
    process.exit(1);
  }
  const context = loadApp();
  const evaluate = vm.runInContext('evaluate', context);
  const lineFor = vm.runInContext('lineFor', context);
  if (typeof evaluate !== 'function' || typeof lineFor !== 'function') {
    console.error('could not reach evaluate()/lineFor() in app.js');
    process.exit(1);
  }

  const data = JSON.parse(fs.readFileSync(DATA, 'utf8'));
  const games = data.games || [];
  const problems = [];
  let compared = 0;

  for (const game of games) {
    const line = lineFor(game);
    for (const lens of LENSES) {
      const stored = (game.picks || {})[lens];
      if (!stored) continue;
      compared++;
      const page = evaluate(game, line, lens);
      const a = `${stored.confidence}/${stored.side || 'none'}`;
      const b = `${page.confidence}/${page.side || 'none'}`;
      if (a !== b) {
        problems.push({ title: game.title, lens, pipeline: a, page: b,
                        line, band: [game.margin_low, game.margin_high] });
      }
    }
  }

  if (!compared) {
    console.error(`${path.relative(ROOT, DATA)} holds no picks to compare`);
    process.exit(1);
  }

  if (problems.length) {
    console.error(`\nThe board and the record disagree on ${problems.length} of ${compared} picks:\n`);
    for (const p of problems) {
      console.error(`  ${p.title} [${p.lens}]`);
      console.error(`      pipeline: ${p.pipeline}`);
      console.error(`      page:     ${p.page}`);
      console.error(`      line ${p.line}, published band [${p.band[0]}, ${p.band[1]}]`);
    }
    console.error('\npipeline/predict.py and docs/app.js must decide from the same published');
    console.error('numbers. See config.publish and the quantisation in build_predictions.py.\n');
    process.exit(1);
  }

  const execChecked = checkExecution(context, data, games);
  console.log(`conformance: ${compared} picks across ${games.length} games, `
              + `page and pipeline agree (plus ${execChecked} execution rows)`);
}

/** The execution box: app.js:venues() against pipeline/execution.py:venues(). */
function checkExecution(context, data, games) {
  const venues = vm.runInContext('venues', context);
  const quoteAt = vm.runInContext('quoteAt', context);
  if (typeof venues !== 'function' || typeof quoteAt !== 'function') {
    console.error('could not reach venues()/quoteAt() in app.js');
    process.exit(1);
  }

  const run = spawnSync('python3', ['-m', 'pipeline.execution', DATA],
                        { cwd: ROOT, encoding: 'utf8' });
  if (run.status !== 0) {
    console.error('could not run pipeline.execution:\n' + (run.stderr || run.error));
    process.exit(1);
  }
  const expected = JSON.parse(run.stdout);
  const byId = new Map(games.map((g) => [g.id, g]));

  // Both sides run the same arithmetic on the same published doubles, so they
  // should agree exactly; the tolerance is here to name a real divergence
  // rather than to tolerate one.
  const EPS = 1e-9;
  const problems = [];
  let rows = 0;

  for (const want of expected) {
    const game = byId.get(want.id);
    const pick = ((game || {}).picks || {})[want.lens] || {};
    const got = venues(quoteAt(game, game.vegas_home_favored_by), pick.side,
                       pick.p_cover, undefined);
    if (got.rows.length !== want.venues.rows.length) {
      problems.push(`${game.title} [${want.lens}]: ${want.venues.rows.length} rows in the `
                    + `pipeline, ${got.rows.length} on the page`);
      continue;
    }
    for (let i = 0; i < got.rows.length; i++) {
      const a = want.venues.rows[i];
      const b = got.rows[i];
      rows++;
      for (const key of ['venue', 'cost', 'break_even', 'dealing', 'edge', 'fillable', 'best', 'level']) {
        const x = a[key];
        const y = b[key];
        const same = (typeof x === 'number' && typeof y === 'number')
          ? Math.abs(x - y) < EPS : x === y;
        if (!same) {
          problems.push(`${game.title} [${want.lens}] ${a.venue}.${key}: `
                        + `pipeline ${x}, page ${y}`);
        }
      }
    }
    for (const key of ['margin', 'tied']) {
      const x = want.venues[key];
      const y = got[key];
      const same = (typeof x === 'number' && typeof y === 'number')
        ? Math.abs(x - y) < EPS : x === y;
      if (!same) problems.push(`${game.title} [${want.lens}] ${key}: pipeline ${x}, page ${y}`);
    }
    if (want.venues.best !== got.best) {
      problems.push(`${game.title} [${want.lens}]: pipeline would place it on `
                    + `${want.venues.best}, the page says ${got.best}`);
    }
  }

  if (problems.length) {
    console.error(`\nThe execution box and the pipeline disagree on ${problems.length} value(s):\n`);
    for (const p of problems) console.error('  ' + p);
    console.error('\ndocs/app.js mirrors pipeline/execution.py. Both must decide from the same');
    console.error('published numbers, or the box recommends the wrong venue.\n');
    process.exit(1);
  }
  return rows;
}

main();
