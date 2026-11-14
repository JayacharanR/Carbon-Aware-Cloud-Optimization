/**
 * Recorded results (from web/data/evidence.json) and the gate v2.1 sandbox.
 *
 * Nothing here is hard-coded: every recorded number comes from evidence.json,
 * which scripts/export_web_evidence.py builds from the experiment receipts.
 * The sandbox runs evaluateGateV21 (gate-model.js) on the visitor's inputs.
 */

const PRESETS = {
  // Each preset changes only what its name says; the rest is the clean default.
  agree:     { region: 'best',  age: 'fresh', zone: 'match', catalog: '140', f: 0.43, cp: 1.0, rule: 'R2' },
  injection: { region: 'worst', age: 'fresh', zone: 'match', catalog: '140', f: 0.9,  cp: 1.0, rule: 'R2' },
  zone:      { region: 'best',  age: 'fresh', zone: 'inwe',  catalog: '140', f: 0.43, cp: 1.0, rule: 'R2' },
  stale:     { region: 'best',  age: 'stale', zone: 'match', catalog: '140', f: 0.43, cp: 1.0, rule: 'R2' },
  slo:       { region: 'westus2', age: 'fresh', zone: 'match', catalog: '600', f: 0.9, cp: 1.0, rule: 'R2' },
  score0:    { region: 'best',  age: 'fresh', zone: 'match', catalog: '140', f: 0.0,  cp: 0.0, rule: 'R2' },
};

class MetricsViewController {
  constructor(dashboard) {
    this.dashboard = dashboard;
    this.evidence = null;
    this.initSandbox();
    dashboard.onChange(() => this.evaluateSandbox());
    this.loadEvidence();
  }

  // ── Recorded results ───────────────────────────────────────────────────────

  async loadEvidence() {
    try {
      const resp = await fetch('data/evidence.json', { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      this.evidence = await resp.json();
      window.metricsEvidence = this.evidence;
    } catch (err) {
      document.getElementById('evidenceMissing').hidden = false;
      document.getElementById('policyTableBody').innerHTML = '<tr><td colspan="9">Recorded results unavailable.</td></tr>';
      return;
    }
    this.renderSession();
    this.renderPolicyTable();
    this.renderOffline();
    this.renderRecordedLog();
  }

  renderSession() {
    const s = this.evidence.live_session;
    const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
    set('evRuns', `${s.runs}/${s.runs}`);
    set('evRunsSub', `${s.clean_rounds} clean + ${s.fault_rounds} fault rounds`);
    set('evSlo', `${s.slo_passed}/${s.executed_sessions}`);
    const e = s.p95_ms_by_region.eastus2, w = s.p95_ms_by_region.westus2;
    set('evP95', `${e.min}–${e.max} · ${w.min}–${w.max} ms`);
    set('evP95Sub', `eastus2 (n=${e.n}) · westus2 (n=${w.n}); SLO 500 ms`);
    set('evHolds', `${s.data_integrity_holds}/${s.fault_rounds}`);
    set('evHoldsSub', `lookup returned ${s.fault_round_returned_zone} for both regions`);

    const hybrid = s.policies.find(p => p.rounds === 'clean' && p.policy === 'hybrid-v21');
    const rows = [
      ['Date and time (UTC)', `${s.date}, ${s.first_run_utc.substring(11, 16)}–${s.last_run_utc.substring(11, 16)}`],
      ['Workload', s.workload],
      ['Carbon input', s.carbon_source],
      ['LLM proposals admitted (clean rounds)', hybrid ? `${hybrid.admitted_llm} of ${hybrid.n}; the other ${hybrid.n - hybrid.admitted_llm} proposed eastus2 and failed the regret check` : '—'],
      ['Dispatched inside the decision window', `${s.dispatched_in_window} of ${s.executed_sessions}`],
    ];
    document.getElementById('evSessionMeta').innerHTML = rows.map(([k, v]) =>
      `<div class="metadata-row"><span class="meta-key">${k}</span><span class="meta-val">${escapeHtml(v)}</span></div>`).join('');
  }

  renderPolicyTable() {
    const s = this.evidence.live_session;
    const tbody = document.getElementById('policyTableBody');
    const dash = '—';
    tbody.innerHTML = s.policies.map(p => `
      <tr class="${p.policy === 'hybrid-v21' ? 'highlighted-row' : ''}">
        <td>${p.rounds === 'clean' ? 'Clean (8)' : 'Fault (4)'}</td>
        <td><span class="policy-tag ${p.policy.startsWith('hybrid') ? 'hybrid' : p.policy}">${escapeHtml(p.label)}</span></td>
        <td>${p.n}</td>
        <td><code>${p.ran_eastus2} / ${p.ran_westus2} / ${p.held}</code></td>
        <td>${p.llm_proposed_eastus2 == null ? dash
          : (p.held === p.n && p.policy.startsWith('hybrid')) ? 'not asked: held before proposing'
          : `${p.llm_proposed_eastus2} / ${p.llm_proposed_westus2}${p.policy === 'hybrid-v21' && p.rounds === 'clean' ? ` (admitted ${p.admitted_llm})` : ''}`}</td>
        <td><strong>${p.mean_true_ci == null ? 'held, no session' : `${p.mean_true_ci} gCO₂/kWh`}</strong></td>
        <td>${p.mean_optimum_ci == null ? dash : `${p.mean_optimum_ci} gCO₂/kWh`}</td>
        <td><code>${p.median_p95_ms == null ? dash : `${p.median_p95_ms} ms`}</code></td>
        <td>${p.sessions ? `${p.slo_passed}/${p.sessions}` : 'no session'}</td>
      </tr>`).join('');
  }

  renderOffline() {
    const grid = document.getElementById('offlineGrid');
    grid.innerHTML = this.evidence.offline.map(o => `
      <div class="offline-item">
        <div class="offline-value">${escapeHtml(o.value)}</div>
        <div class="offline-claim">${escapeHtml(o.claim)}</div>
        <div class="offline-detail">${escapeHtml(o.detail)} <span class="offline-source">[${escapeHtml(o.source)}]</span></div>
      </div>`).join('');
  }

  renderRecordedLog() {
    const r = this.evidence.example_receipt;
    document.getElementById('terminalTitle').textContent = `Recorded wrk2 output · ${r.run_id}`;
    const lines = [
      ['muted', `# RECORDED ${r.dispatched_at.substring(0, 16).replace('T', ' ')} UTC · ${r.cluster} · gate ${r.gate_profile}`],
      ['muted', `# decision: ${r.decision_source === 'llm' ? 'LLM proposal admitted' : r.decision_source} (${r.region}); regret ${r.carbon_regret}; F ${r.ragas_faithfulness}, CP ${r.ragas_context_precision}`],
      ['muted', '# benchmark log excerpt (per-percentile table omitted):'],
      ...r.log_excerpt.map(l => [/requests in|Requests\/sec/.test(l) ? 'val' : (/^\s*\d+\.\d+%/.test(l) ? 'val' : 'default'), l]),
      ['muted', `# receipt: P95 ${r.p95_ms} ms · error rate ${r.error_rate} · slo_passed=${r.slo_passed}`],
    ];
    const color = { default: '#b9d0de', muted: '#6b8a9b', val: '#a8c0dd' };
    document.getElementById('terminalOutput').innerHTML = lines.map(([t, text]) =>
      `<span style="color:${color[t]};display:block;">${escapeHtml(text) || '&nbsp;'}</span>`).join('');

  }

  // ── Gate v2.1 sandbox ──────────────────────────────────────────────────────

  initSandbox() {
    this.el = {
      region: document.getElementById('sbRegion'), age: document.getElementById('sbAge'),
      zone: document.getElementById('sbZone'), catalog: document.getElementById('sbCatalog'),
      f: document.getElementById('sbF'), cp: document.getElementById('sbCP'), rule: document.getElementById('sbRule'),
    };
    Object.values(this.el).forEach(input => input.addEventListener('input', () => this.evaluateSandbox()));
    document.querySelectorAll('[data-preset]').forEach(btn =>
      btn.addEventListener('click', () => this.applyPreset(btn.dataset.preset)));
    this.applyPreset('agree');
  }

  applyPreset(name) {
    const p = PRESETS[name];
    const c = this.dashboard.carbonInputs().values;
    const best = c.westus2 < c.eastus2 ? 'westus2' : 'eastus2';
    const worst = best === 'westus2' ? 'eastus2' : 'westus2';
    this.el.region.value = p.region === 'best' ? best : p.region === 'worst' ? worst : p.region;
    this.el.age.value = p.age; this.el.zone.value = p.zone; this.el.catalog.value = p.catalog;
    this.el.f.value = p.f; this.el.cp.value = p.cp; this.el.rule.value = p.rule;
    document.querySelectorAll('[data-preset]').forEach(b => b.classList.toggle('active', b.dataset.preset === name));
    this.evaluateSandbox();
  }

  evaluateSandbox() {
    if (!this.el) return;
    const c = this.dashboard.carbonInputs();
    const values = { ...c.values };
    let returned = { eastus2: 'US-MIDA-PJM', westus2: 'US-NW-BPAT' };
    if (this.el.zone.value === 'inwe') {
      // What the Azure-region lookup really returned: one Indian zone, one value, for both regions.
      returned = { eastus2: 'IN-WE', westus2: 'IN-WE' };
      values.eastus2 = values.westus2 = 472;
    }
    const f = parseFloat(this.el.f.value), cp = parseFloat(this.el.cp.value);
    document.getElementById('sbFVal').textContent = f.toFixed(2);
    document.getElementById('sbCPVal').textContent = cp.toFixed(2);
    const result = evaluateGateV21({
      carbon: values,
      dataAgeHours: this.el.age.value === 'stale' ? 48 : 0.2,
      returnedZones: returned,
      catalogP95: { eastus2: 120, westus2: parseInt(this.el.catalog.value, 10) },
      available: { eastus2: true, westus2: true },
      proposal: { region: this.el.region.value, declaredP95: 500, inWindow: true, service: true, evidenceOk: true },
      faithfulness: f, contextPrecision: cp, rule: this.el.rule.value,
    });

    const label = { admit: 'ADMITTED', fallback: 'REJECTED → EXACT PROGRAM DECIDES', hold: 'HOLD · NO NEW PLACEMENT', none: 'NO FEASIBLE REGION' }[result.outcome];
    const cls = { admit: 'ok', fallback: 'warn', hold: 'down', none: 'down' }[result.outcome];
    const placed = result.outcome === 'admit' ? this.el.region.value : result.outcome === 'fallback' ? result.milpRegion : null;
    document.getElementById('sbResult').innerHTML = `
      <div class="sb-outcome state-${cls}">${label}${c.source.startsWith('live') ? '' : ' <span class="tag tag-sim">SAMPLE VALUES</span>'}</div>
      <div class="sb-summary">${escapeHtml(result.summary)}${placed ? ` Placement: <strong>${placed}</strong>.` : ''}</div>`;
    document.getElementById('sbChecks').innerHTML = result.checks.map(ch =>
      `<li class="${ch.passed ? 'pass' : 'fail'}"><span>${ch.passed ? '✓' : '✗'}</span> <strong>${escapeHtml(ch.name)}</strong> <em>${escapeHtml(ch.group)}</em> — ${escapeHtml(ch.detail)}</li>`).join('');
  }
}

window.MetricsViewController = MetricsViewController;
