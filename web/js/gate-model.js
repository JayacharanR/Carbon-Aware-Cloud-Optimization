/**
 * Browser model of the scheduler's gate v2.1 decision rule (simulation only).
 *
 * Mirrors the order in src/carbon_scheduler/workflow.py and trust.py:
 *   1. input integrity (data age against the clock, returned zone) -> hold
 *   2. exact program: argmin of intensity over model-feasible regions
 *   3. proposal checks, carbon regret (<= 5% of the program's optimum) and
 *      model feasibility (catalog P95 within the SLO) -> any failure falls back
 *   4. optional evidence score (rule R2: (F + CP)/2 >= 0.6; rule R0: skipped)
 *
 * Simplification, stated wherever results are shown: one decision slot with
 * the current intensities, whereas the scheduler compares every 5-minute slot
 * of a forecast window.
 */

const GATE_V21 = {
  sloP95Ms: 500,
  regretTolerance: 0.05,
  scoreThreshold: 0.6,
  maxDataAgeHours: 2,
  expectedZones: { eastus2: 'US-MIDA-PJM', westus2: 'US-NW-BPAT' },
};

/**
 * @param {object} inp
 *   carbon: {eastus2: number, westus2: number}
 *   dataAgeHours: number
 *   returnedZones: {eastus2: string, westus2: string}
 *   catalogP95: {eastus2: number, westus2: number}
 *   available: {eastus2: bool, westus2: bool}
 *   proposal: {region, declaredP95, service, inWindow, evidenceOk} or null
 *   faithfulness, contextPrecision: numbers in [0, 1]
 *   rule: 'R2' | 'R0'
 */
function evaluateGateV21(inp) {
  const checks = [];
  const add = (group, name, passed, detail) => checks.push({ group, name, passed, detail });
  const regions = ['eastus2', 'westus2'];

  // 1. Input integrity
  const ageOk = inp.dataAgeHours <= GATE_V21.maxDataAgeHours;
  add('input integrity', 'data age', ageOk,
    ageOk ? `${inp.dataAgeHours.toFixed(1)} h ≤ ${GATE_V21.maxDataAgeHours} h` : `${inp.dataAgeHours.toFixed(0)} h old > ${GATE_V21.maxDataAgeHours} h limit`);
  const zoneProblems = regions.filter(r => inp.returnedZones[r] !== GATE_V21.expectedZones[r])
    .map(r => `${r}: returned ${inp.returnedZones[r]}, expected ${GATE_V21.expectedZones[r]}`);
  const sameZone = inp.returnedZones.eastus2 === inp.returnedZones.westus2;
  const identical = inp.carbon.eastus2 === inp.carbon.westus2;
  const zoneOk = zoneProblems.length === 0 && !sameZone && !identical;
  add('input integrity', 'zone identity', zoneOk,
    zoneOk ? 'returned zones match the expected grid zones' :
      [...zoneProblems, sameZone ? 'both regions served the same zone' : null, identical ? 'identical values for both regions' : null]
        .filter(Boolean).join('; '));
  if (!ageOk || !zoneOk) {
    return { outcome: 'hold', deciding: !ageOk ? 'data age' : 'zone identity', checks,
      milpRegion: null, regretPct: null, score: null,
      summary: 'Data-integrity hold: no new placement is made on these inputs; a critical alert is raised.' };
  }

  // 2. Exact program over model-feasible regions
  const feasible = regions.filter(r => inp.available[r] && inp.catalogP95[r] <= GATE_V21.sloP95Ms);
  if (!feasible.length) {
    return { outcome: 'none', deciding: 'no feasible region', checks, milpRegion: null, regretPct: null, score: null,
      summary: 'No region meets the SLO and availability constraints, so the program is infeasible and no action is taken.' };
  }
  const milpRegion = feasible.reduce((a, b) => (inp.carbon[b] < inp.carbon[a] ? b : a));
  const optimum = inp.carbon[milpRegion];

  const p = inp.proposal;
  if (!p) {
    return { outcome: 'fallback', deciding: 'no proposal', checks, milpRegion, regretPct: null, score: null,
      summary: `No valid proposal; the exact program places the workload in ${milpRegion}.` };
  }

  // 3. Proposal checks, regret and model feasibility
  add('proposal feasibility', 'region configured', regions.includes(p.region), p.region);
  add('proposal feasibility', 'service known', p.service !== false, p.service === false ? 'service not in the inventory' : 'service in the inventory');
  add('proposal feasibility', 'start inside window', p.inWindow !== false, p.inWindow === false ? 'start outside [E, D]' : 'inside the decision window');
  add('proposal feasibility', 'cluster available', !!inp.available[p.region], inp.available[p.region] ? 'marked available' : 'marked unavailable');
  add('proposal feasibility', 'declared P95 ≤ SLO', p.declaredP95 <= GATE_V21.sloP95Ms, `declared ${p.declaredP95} ms`);
  add('proposal feasibility', 'evidence IDs', p.evidenceOk !== false, p.evidenceOk === false ? 'cites an ID it was not given' : 'cites only supplied IDs');
  const propCi = inp.carbon[p.region];
  const regret = optimum > 0 ? (propCi - optimum) / optimum : 0;
  const regretOk = regret <= GATE_V21.regretTolerance + 1e-12;
  add('optimality', 'carbon regret ≤ 5%', regretOk,
    `${Math.round(propCi)} vs optimum ${Math.round(optimum)} gCO₂/kWh (${regret >= 0 ? '+' : ''}${(regret * 100).toFixed(1)}%)`);
  const mfOk = feasible.includes(p.region);
  add('proposal feasibility', 'model feasibility', mfOk,
    mfOk ? `catalog P95 ${inp.catalogP95[p.region]} ms ≤ ${GATE_V21.sloP95Ms} ms` : `catalog P95 ${inp.catalogP95[p.region]} ms > ${GATE_V21.sloP95Ms} ms SLO`);

  const failed = checks.filter(c => !c.passed);
  if (failed.length) {
    return { outcome: 'fallback', deciding: failed[0].name, checks, milpRegion, regretPct: regret * 100, score: null,
      summary: `Rejected by “${failed[0].name}”. The exact program decides: ${milpRegion}.` };
  }

  // 4. Optional evidence score
  let score = null;
  if (inp.rule === 'R2') {
    score = (inp.faithfulness + inp.contextPrecision) / 2;
    const scoreOk = score >= GATE_V21.scoreThreshold;
    add('evidence score (R2)', '(F + CP)/2 ≥ 0.6', scoreOk, `${score.toFixed(3)}`);
    if (!scoreOk) {
      return { outcome: 'fallback', deciding: 'evidence score', checks, milpRegion, regretPct: regret * 100, score,
        summary: `Rejected by the evidence score. The exact program decides: ${milpRegion}.` };
    }
  } else {
    add('evidence score', 'not used (rule R0)', true, 'hard checks decide alone');
  }
  return { outcome: 'admit', deciding: null, checks, milpRegion, regretPct: regret * 100, score,
    summary: `Admitted: the proposal (${p.region}) is feasible and within 5% of the program's optimum.` };
}

window.GATE_V21 = GATE_V21;
window.evaluateGateV21 = evaluateGateV21;
