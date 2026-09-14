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
 *   node tools/conformance.js [path/to/current.json]
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

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
  const node = () => ({ style: {}, append() {}, addEventListener() {}, classList: { add() {} } });
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

  console.log(`conformance: ${compared} picks across ${games.length} games, page and pipeline agree`);
}

main();
