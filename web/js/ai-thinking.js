/**
 * Decision walkthrough (simulation).
 *
 * Shows, step by step, what gate v2.1 would decide for the current live carbon
 * values and a scripted example proposal. It calls no model, runs no solver on
 * the server and deploys nothing; the outcome comes from evaluateGateV21 in
 * gate-model.js. Recorded behaviour of the real scheduler is quoted from
 * web/data/evidence.json where it helps.
 */

class AIThinkingPanel {
  constructor(dashboard) {
    this.dashboard = dashboard;
    this._timers = [];
    this._init();
  }

  trigger(source = 'primary', meta = null) {
    this._clear();
    this._resetProgress();
    this._overlay.classList.add('visible');
    this._panel.classList.add('visible');
    this._panel.focus();
    this._stream(this._buildSteps(source, meta));
  }

  close() {
    this._clear();
    this._overlay.classList.remove('visible');
    this._panel.classList.remove('visible');
  }

  _init() {
    const root = document.createElement('div');
    root.id = 'aiThinkingRoot';
    root.innerHTML = `
<div id="thinkingOverlay" class="thinking-overlay"></div>
<div id="thinkingPanel" class="thinking-panel" role="dialog" aria-modal="true" aria-labelledby="thinkingTitle" tabindex="-1">
  <div class="thinking-header">
    <div class="thinking-header-left">
      <div class="thinking-header-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2z"/><path d="M12 6v6l4 2"/></svg>
      </div>
      <div>
        <div class="thinking-title" id="thinkingTitle">Decision Walkthrough · Simulation</div>
        <div class="thinking-subtitle">Gate v2.1 rule on live carbon values · no model is called · nothing is deployed</div>
      </div>
    </div>
    <button id="thinkingCloseBtn" class="thinking-close-btn" aria-label="Close panel" title="Close (Esc)">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="sim-ribbon"><span class="tag tag-sim">SIMULATION</span> Every step below is computed in your browser. Recorded results of the real scheduler are marked <span class="tag tag-recorded">RECORDED</span>.</div>
  <div class="thinking-steps-bar">
    <div class="thinking-step" id="tStep1"><span class="step-dot"></span>01 Carbon inputs</div><div class="thinking-step-sep">›</div>
    <div class="thinking-step" id="tStep2"><span class="step-dot"></span>02 Data checks</div><div class="thinking-step-sep">›</div>
    <div class="thinking-step" id="tStep3"><span class="step-dot"></span>03 Exact program</div><div class="thinking-step-sep">›</div>
    <div class="thinking-step" id="tStep4"><span class="step-dot"></span>04 Proposal checks</div><div class="thinking-step-sep">›</div>
    <div class="thinking-step" id="tStep5"><span class="step-dot"></span>05 Outcome</div>
  </div>
  <div class="thinking-body"><div id="thinkingOutput" class="thinking-output"></div></div>
  <div class="thinking-footer">
    <span class="thinking-footer-note">Open from a cluster card or any service in the topology</span>
    <span id="thinkingElapsed" class="thinking-footer-elapsed"></span>
  </div>
</div>`;
    document.body.appendChild(root);
    this._overlay = document.getElementById('thinkingOverlay');
    this._panel = document.getElementById('thinkingPanel');
    document.getElementById('thinkingCloseBtn').addEventListener('click', () => this.close());
    this._overlay.addEventListener('click', () => this.close());
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this._panel.classList.contains('visible')) this.close(); });
  }

  _block(num, label, source, body, conclusion, tone = 'green') {
    return `
<div class="think-block">
  <div class="think-block-header">
    <span class="think-step-num">${num}</span><span class="think-step-label">${label}</span><span class="think-step-source">${source}</span>
  </div>
  <div class="think-block-body">${body}${conclusion ? `<div class="think-conclusion ${tone}">${conclusion}</div>` : ''}</div>
</div>`;
  }

  _kv(rows) {
    return `<div class="think-kv-grid">${rows.map(([k, v, cls = 'mono']) =>
      `<div class="think-kv"><span class="kv-key">${k}</span><span class="kv-val ${cls}">${v}</span></div>`).join('')}</div>`;
  }

  _checkList(checks) {
    return `<ul class="sandbox-checks in-panel">${checks.map(ch =>
      `<li class="${ch.passed ? 'pass' : 'fail'}"><span>${ch.passed ? '✓' : '✗'}</span> <strong>${escapeHtml(ch.name)}</strong> — ${escapeHtml(ch.detail)}</li>`).join('')}</ul>`;
  }

  _buildSteps(source, meta) {
    const d = this.dashboard;
    const c = d.carbonInputs();
    const ev = window.metricsEvidence || null;
    const recorded = ev && ev.example_receipt;

    let target, proposalRegion, origin;
    if (source === 'primary') {
      target = 'the DeathStarBench stamp'; proposalRegion = 'eastus2'; origin = 'cluster card aks-primary-eastus2';
    } else if (source === 'secondary') {
      target = 'the DeathStarBench stamp'; proposalRegion = 'westus2'; origin = 'cluster card aks-secondary-westus2';
    } else {
      target = `${source} (${meta && meta.tier ? meta.tier : 'service'}) and the rest of its stamp`;
      proposalRegion = 'eastus2'; origin = `topology node ${source}`;
    }

    const returned = c.returnedZones || { eastus2: 'none', westus2: 'none' };
    const inputs = {
      carbon: c.values,
      dataAgeHours: c.ok ? c.ageHours : 999,
      returnedZones: returned,
      catalogP95: { eastus2: 120, westus2: 140 },
      available: { eastus2: true, westus2: true },
      proposal: { region: proposalRegion, declaredP95: 500, inWindow: true, service: true, evidenceOk: true },
      faithfulness: recorded ? recorded.ragas_faithfulness : 0.43,
      contextPrecision: recorded ? recorded.ragas_context_precision : 1.0,
      rule: 'R2',
    };
    // Without a valid observation there is nothing to check; say so rather than invent an age or a zone.
    const result = c.ok ? evaluateGateV21(inputs) : {
      outcome: 'hold', milpRegion: null,
      summary: 'Data-integrity hold: no new placement is made without valid carbon data; a critical alert is raised.',
      checks: [
        { group: 'input integrity', name: 'data age', passed: false, detail: 'no valid observation to date' },
        { group: 'input integrity', name: 'zone identity', passed: false, detail: 'no zone returned for either region' },
      ],
    };
    const integrity = result.checks.filter(ch => ch.group === 'input integrity');
    const later = result.checks.filter(ch => ch.group !== 'input integrity');
    const steps = [];

    // 01 Carbon inputs
    const carbonRows = c.ok ? [
      ['Selected by', escapeHtml(origin)],
      ['Workload', escapeHtml(target)],
      ['eastus2 · US-MIDA-PJM', `${Math.round(c.values.eastus2)} gCO₂eq/kWh`],
      ['westus2 · US-NW-BPAT', `${Math.round(c.values.westus2)} gCO₂eq/kWh`],
      ['Data hour', fmtTime(c.observedAt)],
      ['Zones returned', `${escapeHtml(returned.eastus2)}, ${escapeHtml(returned.westus2)}`],
    ] : [
      ['Selected by', escapeHtml(origin)],
      ['Live carbon data', `<span class="txt-hold">unavailable</span>`, ''],
      ['Reason', escapeHtml(c.holdReason || 'unknown'), ''],
    ];
    steps.push({ id: 'tStep1', html: this._block('01', 'CARBON INPUTS', c.ok ? '<span class="tag tag-live">LIVE</span> Electricity Maps latest' : 'Electricity Maps',
      this._kv(carbonRows),
      c.ok ? null : 'No valid observation is available, so the data checks below cannot pass.', c.ok ? 'green' : 'red') });

    // 02 Data checks
    const held = result.outcome === 'hold';
    steps.push({ id: 'tStep2', html: this._block('02', 'INPUT INTEGRITY CHECKS', 'data age against the clock · returned zone identity',
      this._checkList(integrity),
      held ? `✗ ${escapeHtml(result.summary)}` : '✓ The carbon data pass the age and identity checks.', held ? 'red' : 'green') });

    if (!held) {
      // 03 Exact program
      steps.push({ id: 'tStep3', html: this._block('03', 'EXACT PROGRAM (REFERENCE)', 'PuLP/CBC in the real scheduler · argmin here',
        this._kv([
          ['Feasible regions', 'eastus2 (catalog P95 120 ms), westus2 (140 ms); SLO 500 ms'],
          ['Program choice', `<strong>${result.milpRegion}</strong>`, 'mono'],
          ['Its intensity', `${Math.round(c.values[result.milpRegion])} gCO₂eq/kWh`],
        ]),
        'Simplified to one slot with the current values; the real program compares every 5-minute slot of a forecast window and may delay the start.', 'muted') });

      // 04 Proposal and checks
      const proposalNote = recorded
        ? `<div class="think-note"><span class="tag tag-recorded">RECORDED</span> On 12 Oct 2026 Llama-3 8B proposed eastus2 in 7 of 8 live rounds and declared a P95 of exactly 500 ms each time; offline it proposed the dirtier region in 72 of 72 clean cycles.</div>`
        : '';
      const scoreNote = result.checks.some(ch => ch.group.startsWith('evidence'))
        ? `<div class="think-note">The evidence score uses recorded RAGAS scores of run ${recorded ? recorded.run_id : 'lvd-clean08-hybrid-v21'} (F ${inputs.faithfulness}, CP ${inputs.contextPrecision}); the judge is not run here.</div>`
        : '';
      steps.push({ id: 'tStep4', html: this._block('04', 'PROPOSAL AND GATE CHECKS', 'scripted example proposal',
        this._kv([['Example proposal', `run in <strong>${proposalRegion}</strong>, declared P95 500 ms`, 'mono']]) + proposalNote +
        this._checkList(later) + scoreNote,
        result.outcome === 'admit' ? `✓ ${escapeHtml(result.summary)}` : `✗ ${escapeHtml(result.summary)}`,
        result.outcome === 'admit' ? 'green' : 'amber') });
    }

    // 05 Outcome
    const cl = d.clusterSummary();
    const clusterLine = cl.primary === 'ready' && cl.secondary === 'ready'
      ? 'Both clusters are reachable now, but this page never deploys anything.'
      : `Clusters now: eastus2 ${cl.primary}, westus2 ${cl.secondary}${cl.reason ? ` (${escapeHtml(cl.reason)})` : ''}.`;
    const placed = result.outcome === 'admit' ? proposalRegion : result.outcome === 'fallback' ? result.milpRegion : null;
    const recordedLine = recorded && result.outcome === 'admit'
      ? `<div class="think-note"><span class="tag tag-recorded">RECORDED</span> A real run with this outcome: <code>${recorded.run_id}</code>, admitted ${recorded.region} on ${recorded.dispatched_at.substring(0, 10)}, P95 ${recorded.p95_ms} ms, SLO passed.</div>`
      : result.outcome === 'hold' && ev
        ? `<div class="think-note"><span class="tag tag-recorded">RECORDED</span> In all ${ev.live_session.fault_rounds} live fault rounds of 12 Oct 2026, gate v2.1 held while the ungated program and LLM ran in eastus2 on corrupted data.</div>`
        : '';
    steps.push({ id: 'tStep5', html: this._block('05', 'OUTCOME', 'what the scheduler would do',
      this._kv([
        ['Decision', placed ? `${result.outcome === 'admit' ? 'proposal admitted' : 'exact program decides'} → <strong>${placed}</strong>` : '<span class="txt-hold">hold, no new placement</span>', 'mono'],
        ['Executed', 'no (simulation)', 'mono'],
      ]) + recordedLine,
      escapeHtml(clusterLine), 'muted') });

    return steps;
  }

  _resetProgress() {
    for (let i = 1; i <= 5; i++) document.getElementById(`tStep${i}`).className = 'thinking-step';
    document.getElementById('thinkingOutput').innerHTML = '';
    document.getElementById('thinkingElapsed').textContent = '';
  }

  _stream(steps) {
    const out = document.getElementById('thinkingOutput');
    const body = out.closest('.thinking-body');
    steps.forEach((step, idx) => {
      this._timers.push(setTimeout(() => {
        const prev = idx > 0 && document.getElementById(steps[idx - 1].id);
        if (prev) { prev.classList.remove('active'); prev.classList.add('done'); }
        document.getElementById(step.id).classList.add('active');
        const wrap = document.createElement('div');
        wrap.innerHTML = step.html;
        out.appendChild(wrap);
        body.scrollTop = body.scrollHeight;
        if (idx === steps.length - 1) {
          document.getElementById(step.id).classList.add('done');
          for (let i = 1; i <= 5; i++) {
            const el = document.getElementById(`tStep${i}`);
            if (!steps.some(s => s.id === `tStep${i}`)) el.classList.add('skipped');
          }
          document.getElementById('thinkingElapsed').textContent = 'Simulation complete';
        }
      }, idx * 450));
    });
  }

  _clear() {
    this._timers.forEach(t => clearTimeout(t));
    this._timers = [];
  }
}

window.AIThinkingPanel = AIThinkingPanel;
