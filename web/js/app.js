/**
 * Dashboard controller: the single source of truth for what the page says.
 *
 *   LIVE        /api/live-carbon and /api/cluster-status, polled from the server
 *   LIVE RUN    /api/cycle: a real scheduling cycle started from this page
 *               (real Llama-3 proposal, gate v2.1, real Jobs on AKS)
 *   SIMULATION  the decision walkthrough and the gate sandbox
 *   RECORDED    web/data/evidence.json (rendered by metrics-view.js)
 *
 * The animation shows where the workload really is: the cluster running this
 * page's current cycle, else the cluster of the last real cycle.
 */

const SAMPLE_CARBON = { eastus2: 325, westus2: 148 };  // used only when live data are unavailable, and labelled so
const ACTIVE_PHASES = ['starting', 'capturing', 'deciding', 'resetting', 'benchmarking', 'stopping'];
const REGION_OF_CLUSTER = { 'aks-primary-eastus2': 'eastus2', 'aks-secondary-westus2': 'westus2' };

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? '—' : `${d.toISOString().substring(11, 16)} UTC`;
}

function fmtTimeSec(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? '—' : d.toISOString().substring(11, 19);
}

function fmtAge(seconds) {
  if (seconds == null) return '';
  if (seconds < 90) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${(seconds / 3600).toFixed(1)} h ago`;
}

class DashboardController {
  constructor(anim) {
    this.anim = anim;
    this.live = null;        // last /api/live-carbon response
    this.liveError = null;   // fetch failure (server unreachable)
    this.cluster = null;     // last /api/cluster-status response
    this.cycle = null;       // last /api/cycle response
    this.cycleError = null;
    this.listeners = [];
    this.controlBusy = false;
    this.actionError = null;
  }

  onChange(fn) { this.listeners.push(fn); }

  start() {
    this.pollLive();
    this.pollCluster();
    this.pollCycle();
    setInterval(() => this.pollCluster(), 60000);
    setInterval(() => this.render(), 30000); // keep "x min ago" honest
  }

  async pollLive() {
    clearTimeout(this._liveTimer);
    try {
      const resp = await fetch('/api/live-carbon', { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      this.live = await resp.json();
      this.liveError = null;
    } catch (err) {
      this.liveError = `dashboard server unreachable (${err.message})`;
    }
    this.render();
    // Until the server has completed its first Electricity Maps poll (e.g. right after a restart), retry quickly.
    const waiting = this.liveError || !this.live || !this.live.last_poll_at;
    this._liveTimer = setTimeout(() => this.pollLive(), waiting ? 5000 : 60000);
  }

  async pollCluster() {
    try {
      const resp = await fetch('/api/cluster-status', { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      this.cluster = await resp.json();
    } catch (err) {
      this.cluster = { error: err.message };
    }
    this.renderClusters();
  }

  async pollCycle() {
    clearTimeout(this._cycleTimer);
    try {
      const resp = await fetch('/api/cycle', { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      this.cycle = await resp.json();
      this.cycleError = null;
    } catch (err) {
      this.cycleError = err.message;
    }
    this.render();
    this._cycleTimer = setTimeout(() => this.pollCycle(), this.cycleActive() ? 3000 : 30000);
  }

  async runCycle(scenario = null) {
    if (this.controlBusy || this.cycleActive() || this.simulation?.enabled) return;
    this.controlBusy = true;
    this.actionError = null;
    this.render();
    const btn = document.getElementById('btnRunCycle');
    btn.disabled = true;
    document.getElementById('btnRunSpike').disabled = true;
    try {
      const resp = await fetch(`/api/cycle/run${scenario ? `?scenario=${scenario}` : ''}`, { method: 'POST' });
      const body = await resp.json();
      if (!resp.ok) {
        throw new Error(body.reason || `HTTP ${resp.status}`);
      }
      await this.pollCycle();
    } catch (err) {
      this.actionError = `Not started: ${err.message}`;
    } finally {
      this.controlBusy = false;
      this.render();
    }
  }

  async postControl(path) {
    const response = await fetch(path, { method: 'POST' });
    const body = await response.json();
    if (!response.ok) throw new Error(body.reason || `HTTP ${response.status}`);
    return body;
  }

  async stopCycle() {
    if (this.controlBusy) return;
    this.controlBusy = true;
    this.actionError = null;
    this.render();
    try {
      await this.postControl('/api/cycle/stop');
      await this.pollCycle();
    } catch (err) {
      this.actionError = `Stop failed: ${err.message}`;
    } finally {
      this.controlBusy = false;
      this.render();
    }
  }

  async resetPage() {
    if (this.controlBusy) return;
    this.controlBusy = true;
    this.actionError = null;
    this.simulation.exit();
    window.thinkingPanel?.close();
    this.render();
    try {
      // Reset also cancels an active live cycle. Wait for Job cleanup before
      // allowing reload to hide its state; never erase an incomplete stop.
      await this.pollCycle();
      if (this.cycleActive()) {
        await this.postControl('/api/cycle/stop');
        const deadline = Date.now() + 90000;
        do {
          await new Promise(resolve => setTimeout(resolve, 750));
          await this.pollCycle();
          if (this.cycleError) throw new Error(this.cycleError);
          if (Date.now() > deadline) throw new Error('Stop is still in progress. Wait and retry reset.');
        } while (this.cycleActive() || this.currentCycle()?.worker_active);
      }
      await this.postControl('/api/cycle/reset');
      window.location.reload();
    } catch (err) {
      this.actionError = `Reset failed: ${err.message}`;
      this.controlBusy = false;
      this.render();
    }
  }

  // ── Derived state ──────────────────────────────────────────────────────────

  liveStatus() {
    if (this.liveError || !this.live) return 'hold';
    return this.live.status;
  }

  holdReason() {
    if (this.liveError) return this.liveError;
    if (!this.live) return 'waiting for the first poll';
    return this.live.hold_reason;
  }

  /** Values the simulations should use, with their provenance. */
  carbonInputs() {
    const ok = this.liveStatus() !== 'hold';
    const live = ok ? { eastus2: this.live.primary.intensity, westus2: this.live.secondary.intensity } : null;
    const values = { ...(live || SAMPLE_CARBON) };
    const ageHours = ok ? Math.max(this.live.primary.age_seconds || 0, this.live.secondary.age_seconds || 0) / 3600 : null;
    return { values, live, ok, source: ok ? 'live' : 'sample', ageHours,
      returnedZones: ok ? { eastus2: this.live.primary.zone_returned, westus2: this.live.secondary.zone_returned } : null,
      observedAt: ok ? this.live.primary.observed_at : null, holdReason: ok ? null : this.holdReason() };
  }

  recommendation() {
    const c = this.carbonInputs();
    if (!c.ok) return null;
    return c.values.westus2 < c.values.eastus2 ? 'westus2' : 'eastus2';
  }

  currentCycle() { return this.cycle ? this.cycle.current : null; }

  cycleActive() {
    const cur = this.currentCycle();
    return !!cur && ACTIVE_PHASES.includes(cur.phase);
  }

  /** Last finished real cycle that placed a session on a cluster. */
  lastPlacement() {
    if (this.currentCycle()?.reset_at) return null;
    const hist = (this.cycle && this.cycle.history) || [];
    return hist.find(r => r.outcome === 'executed' && r.cluster) || null;
  }

  /** Where the workload really is: {region, cluster, kind, text, from}. */
  placement() {
    if (this.simulation?.enabled) return { region: this.simulation.region, kind: 'simulation' };
    const cur = this.currentCycle();
    if (this.cycleActive()) {
      if (cur.phase === 'stopping') return { region: null, kind: 'deciding', text: 'stopping the live cycle and cleaning up its Jobs' };
      if (cur.region) {
        const moved = cur.previous_cluster && cur.previous_cluster !== cur.cluster ? cur.previous_cluster : null;
        return { region: cur.region, cluster: cur.cluster, kind: 'running', from: moved,
          text: cur.phase === 'benchmarking' ? 'wrk2 benchmark running' : 'fixture reset running' };
      }
      return { region: null, kind: 'deciding', text: cur.phase === 'capturing' ? 'capturing carbon data' : 'scheduler deciding' };
    }
    const last = this.lastPlacement();
    if (last) {
      return { region: REGION_OF_CLUSTER[last.cluster] || last.final_region, cluster: last.cluster, kind: 'last', text: '', receipt: last };
    }
    return { region: null, kind: 'none' };
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  render() {
    if (this.cycleActive() && this.simulation?.enabled) {
      this.simulation.clearTimers();
      this.simulation.enabled = false;
      this.simulation.phase = 'idle';
      this.simulation.events = [];
    }
    this.simulation?.render();
    const c = this.carbonInputs();
    const status = this.liveStatus();
    const rec = this.recommendation();

    // HUD intensities (plus the scenario value while a spike cycle runs)
    const curSc = this.cycleActive() && this.currentCycle().scenario;
    for (const [region, id] of [['eastus2', 'liveEastIntensity'], ['westus2', 'liveWestIntensity']]) {
      const base = c.ok ? `${Math.round(c.live[region])} gCO₂/kWh` : 'unavailable';
      document.getElementById(id).textContent = curSc && curSc.region === region ? `${base} · scenario ${Math.round(curSc.scenario_value)}` : base;
    }
    const sim = this.simulation;
    this.anim.setCarbonLabels(sim?.enabled ? sim.values.eastus2 : c.ok ? c.values.eastus2 : null,
      sim?.enabled ? sim.values.westus2 : c.ok ? c.values.westus2 : null,
      sim?.enabled ? ' · SIM' : '', sim?.enabled ? ' · SIM' : '');
    // Centre HUD
    document.getElementById('hudRecommendation').textContent = sim?.enabled
      ? sim.values.eastus2 === sim.values.westus2 ? 'Simulation: equal carbon'
        : `Simulation: ${sim.values.eastus2 < sim.values.westus2 ? 'eastus2' : 'westus2'} is cleaner`
      : c.ok ? `Lower-carbon region now: ${rec}` : 'HOLD · carbon data failed checks';

    // Main banner (live carbon)
    const banner = document.getElementById('statusBannerMsg');
    if (status === 'hold') {
      banner.innerHTML = `<span class="live-indicator crimson">● CARBON DATA UNAVAILABLE</span> ${escapeHtml(c.holdReason || '')}. With such inputs the scheduler holds: no new placement is made.`;
    } else {
      const p = this.live.primary, s = this.live.secondary;
      const lead = status === 'degraded'
        ? '<span class="live-indicator amber">● LIVE (previous observation)</span>'
        : '<span class="live-indicator">● LIVE</span>';
      const pct = this.live.reduction_pct;
      banner.innerHTML = `${lead} Electricity Maps: PJM (eastus2) <strong>${Math.round(p.intensity)}</strong> vs BPAT (westus2) <strong>${Math.round(s.intensity)}</strong> gCO₂eq/kWh. Lower-carbon region now: <strong>${this.live.recommended_region}</strong>${pct > 0 ? ` (${pct}% lower intensity)` : ''}.${status === 'degraded' ? ` ${escapeHtml(this.live.hold_reason || '')}.` : ''}`;
    }

    // Placement banner and animation: where the workload really is
    const pl = this.placement();
    const pb = document.getElementById('previewBannerMsg');
    const note = document.getElementById('canvasNote');
    const cur = this.currentCycle();
    const sc = cur && this.cycleActive() ? cur.scenario : null;
    const scText = sc ? ` <span class="tag tag-sim">SCENARIO</span> ${sc.region} spiked ${sc.live_value} → ${sc.scenario_value} gCO₂/kWh in the scheduler's input.` : '';
    if (pl.kind === 'simulation') {
      pb.innerHTML = `<span class="tag tag-sim">SIMULATION</span> ${escapeHtml(sim.events[sim.events.length - 1] || 'Reading carbon meters')}`;
      note.innerHTML = `<span class="tag tag-sim">SIMULATION</span> Placement: ${pl.region} · no AKS execution`;
    } else if (pl.kind === 'running') {
      const what = pl.text.startsWith('wrk2')
        ? 'wrk2 is sending compose-post requests (10 req/s) to this cluster now'
        : 'the fixture reset is running on this cluster; the benchmark starts next';
      const switched = pl.from ? `<strong class="state-warn">Switched</strong> from ${pl.from} to <strong>${pl.cluster}</strong>. ` : `Placed on <strong>${pl.cluster}</strong> (${pl.region}). `;
      pb.innerHTML = `<span class="tag tag-live">LIVE RUN</span> ${switched}${what}.${scText}`;
      note.innerHTML = `<span class="tag tag-live">LIVE RUN</span> ${pl.from ? `switched ${pl.from} → ` : ''}${pl.cluster} · ${pl.text}`;
    } else if (pl.kind === 'deciding') {
      pb.innerHTML = `<span class="tag tag-live">LIVE RUN</span> ${pl.text}; no Job has been created yet.${scText}`;
      note.innerHTML = `<span class="tag tag-live">LIVE RUN</span> ${pl.text}…`;
    } else if (pl.kind === 'last') {
      const r = pl.receipt;
      const spikeNote = r.scenario ? ` <span class="tag tag-sim">SCENARIO</span> decided with ${r.scenario.region} spiked to ${r.scenario.scenario_value} gCO₂/kWh.` : '';
      pb.innerHTML = `<span class="tag tag-live">LAST REAL PLACEMENT</span> <strong>${r.cluster}</strong> at ${fmtTime(r.dispatched_at)} (run <code>${r.run_id}</code>, ${r.decision_source === 'llm' ? 'LLM proposal admitted' : 'exact program decided'}): P95 ${r.p95_ms} ms, SLO ${r.slo_passed ? 'passed' : 'failed'}.${spikeNote}`;
      note.innerHTML = `Flow to the last real placement · ${r.cluster}`;
    } else {
      pb.textContent = 'Ready for a new presentation run. Run a live cycle or use the adjustable carbon simulation.';
      note.textContent = 'Waiting for a run';
    }
    this.anim.holding = !pl.region;
    if (pl.region) this.anim.setRoute(pl.region === 'westus2' ? 'optimized' : 'baseline');
    for (const [region, id] of [['eastus2', 'primaryJobState'], ['westus2', 'secondaryJobState']]) {
      const el = document.getElementById(id);
      if (pl.region === region && pl.kind === 'simulation') el.innerHTML = '<span class="tag tag-sim">SIMULATION</span> Workload placement';
      else if (pl.region === region && pl.kind === 'running') el.innerHTML = `<span class="tag tag-live">LIVE RUN</span> ${pl.text}`;
      else if (pl.region === region && pl.kind === 'last') el.innerHTML = `Last real placement · ${fmtTime(pl.receipt.dispatched_at)}`;
      else el.innerHTML = '';
    }

    // Meta row
    document.getElementById('carbonObservedAt').textContent = c.ok
      ? `${fmtTime(this.live.primary.observed_at)} · ${fmtAge(this.live.primary.age_seconds)}`
      : 'no valid observation';
    document.getElementById('liveLastPoll').textContent = this.live && this.live.last_poll_at ? fmtTime(this.live.last_poll_at) : '—';
    document.getElementById('liveCarbonReduction').textContent = c.ok
      ? (this.live.reduction_pct > 0 ? `westus2 ${this.live.reduction_pct}% lower` : 'eastus2 equal or lower')
      : '—';

    // Carbon difference card
    const delta = document.getElementById('deltaPerKwh');
    if (c.ok) {
      const d = c.live.eastus2 - c.live.westus2;
      delta.textContent = d >= 0 ? `≈ ${Math.round(d)} g CO₂e less` : `≈ ${Math.round(-d)} g CO₂e more`;
    } else {
      delta.textContent = 'unavailable';
    }

    this.renderCycle();
    this.listeners.forEach(fn => fn(this));
  }

  renderCycle() {
    const btn = document.getElementById('btnRunCycle');
    const statusEl = document.getElementById('cycleStatus');
    const cur = this.currentCycle();
    const active = this.cycleActive();
    const clusters = this.clusterSummary();
    const ready = clusters.primary === 'ready' && clusters.secondary === 'ready';
    const spikeBtn = document.getElementById('btnRunSpike');
    btn.disabled = active || !ready || this.controlBusy || !!this.simulation?.enabled;
    spikeBtn.disabled = btn.disabled;
    const stop = document.getElementById('btnStopCycle');
    stop.disabled = this.controlBusy || ((!active || cur?.phase === 'stopping') && cur?.phase !== 'cancel_failed');
    stop.textContent = cur?.phase === 'cancel_failed' ? 'Retry force stop' : cur?.phase === 'stopping' ? 'Stopping live cycle…' : 'Force stop live cycle';
    document.getElementById('btnResetPage').disabled = this.controlBusy;
    btn.textContent = active ? '⏳ Live cycle running…' : '▶ Run live cycle';
    btn.title = ready ? 'Runs the real scheduler and real benchmark Jobs on AKS' : 'Both AKS clusters must be ready';

    // Stepper
    const phase = cur ? cur.phase : 'idle';
    const reached = new Set((cur && cur.events || []).map(e => e.phase));
    document.querySelectorAll('#cycleSteps li').forEach(li => {
      const step = li.dataset.step;
      li.className = phase === step && active ? 'active' : reached.has(step) ? 'done' : '';
    });

    if (this.actionError) {
      statusEl.innerHTML = `<span class="txt-hold">${escapeHtml(this.actionError)}</span>`;
    } else if (this.cycleError) {
      statusEl.innerHTML = `<span class="txt-hold">Cycle status unavailable:</span> ${escapeHtml(this.cycleError)}`;
    } else if (!cur || phase === 'idle') {
      statusEl.textContent = ready
        ? 'No cycle is running. Both clusters are ready.'
        : `Live cycles need both clusters ready (eastus2 ${clusters.primary}, westus2 ${clusters.secondary}).`;
    } else if (active) {
      const last = cur.events && cur.events.length ? cur.events[cur.events.length - 1].text : 'starting';
      statusEl.innerHTML = `<span class="live-dot"></span> <strong>${escapeHtml(cur.run_id)}</strong> · ${escapeHtml(last)}`;
    } else if (phase === 'failed' || phase === 'cancel_failed' || phase === 'cancelled') {
      statusEl.innerHTML = `<span class="txt-hold">Cycle ${escapeHtml(cur.run_id)} ${phase.replace('_', ' ')}:</span> ${escapeHtml(cur.events[cur.events.length - 1].text)}`;
    } else {
      statusEl.innerHTML = `Cycle <strong>${escapeHtml(cur.run_id)}</strong> finished at ${fmtTime(cur.finished_at)}.`;
    }

    document.getElementById('cycleEvents').innerHTML = (cur && cur.events || []).map(e =>
      `<li><span class="ev-time">${fmtTimeSec(e.at)}</span> ${escapeHtml(e.text)}</li>`).join('');

    // Receipt of the current (or most recent) finished cycle
    const hist = (this.cycle && this.cycle.history) || [];
    const receipt = (cur && cur.receipt) || (!active && !cur?.reset_at && !['cancelled', 'cancel_failed'].includes(phase) && hist[0]) || null;
    const box = document.getElementById('cycleReceipt');
    box.hidden = !(receipt && !active);
    if (!box.hidden) box.innerHTML = this.receiptHtml(receipt);

    // History
    const histEl = document.getElementById('cycleHistory');
    histEl.hidden = hist.length === 0;
    histEl.innerHTML = hist.length ? `
      <div class="cycle-history-title">Live cycles run from this page (receipts in <code>artifacts/web-cycles/</code>)</div>
      <div class="policy-table-wrapper"><table class="policy-table compact">
        <thead><tr><th>Dispatched (UTC)</th><th>Run</th><th>Carbon input</th><th>LLM proposed</th><th>Gate v2.1</th><th>Placed on</th><th>P95</th><th>Errors</th><th>SLO</th></tr></thead>
        <tbody>${hist.map(r => `<tr>
          <td>${r.dispatched_at ? r.dispatched_at.substring(0, 16).replace('T', ' ') : '—'}</td>
          <td><code>${escapeHtml(r.run_id)}</code></td>
          <td>${r.scenario ? `<span class="tag tag-sim">SPIKE</span> ${r.scenario.region} ×${r.scenario.factor}` : 'live'}</td>
          <td>${r.llm_proposed_region || '—'}</td>
          <td>${r.gate_passed ? 'admitted' : `rejected (${r.gate_failed_checks.length ? r.gate_failed_checks.join(', ') : 'evidence score'})`}</td>
          <td>${r.cluster || r.outcome}</td>
          <td>${r.p95_ms == null ? '—' : `${r.p95_ms} ms`}</td>
          <td>${r.error_rate == null ? '—' : `${(r.error_rate * 100).toFixed(2)}%`}</td>
          <td>${r.slo_passed == null ? '—' : r.slo_passed ? 'passed' : 'failed'}</td></tr>`).join('')}</tbody>
      </table></div>` : '';
  }

  receiptHtml(r) {
    const ci = (reg) => (r.carbon[reg] && r.carbon[reg].intensity != null ? `${Math.round(r.carbon[reg].intensity)} gCO₂/kWh` : '—');
    const regret = r.carbon_regret != null && r.gate_failed_checks.includes('carbon_regret')
      ? ` (regret +${(r.carbon_regret * 100).toFixed(0)}%, limit 5%)` : '';
    const scoreText = r.evidence_score != null
      ? `evidence score (F ${r.faithfulness.toFixed(2)} + CP ${r.context_precision.toFixed(2)})/2 = ${r.evidence_score.toFixed(2)}` : 'evidence score';
    let gate;
    if (r.gate_passed) gate = `<span class="state-ok">admitted</span> · all ${r.gate_checks_total} checks passed${r.evidence_score != null ? `, ${scoreText} ≥ 0.6` : ''}`;
    else if (r.gate_failed_checks.length) gate = `<span class="state-warn">rejected</span> by ${escapeHtml(r.gate_failed_checks.join(', '))}${regret}`;
    else if (r.llm_proposed_region) gate = `<span class="state-warn">rejected</span> by the ${scoreText} &lt; 0.6; all ${r.gate_checks_total} hard checks passed`;
    else gate = `<span class="state-warn">no admissible proposal</span>${r.gate_failure_reason ? ` (${escapeHtml(r.gate_failure_reason)})` : ''}`;
    const rows = [
      ...(r.scenario ? [['Scenario', `<span class="tag tag-sim">SPIKE</span> ${r.scenario.region} ${r.scenario.live_value} → ${r.scenario.scenario_value} gCO₂/kWh (×${r.scenario.factor}, input only)`]] : []),
      [r.scenario ? 'Carbon seen by the scheduler' : 'Carbon captured', `eastus2 ${ci('eastus2')} · westus2 ${ci('westus2')}`],
      ['Llama-3 8B proposed', r.llm_proposed_region || 'no valid proposal'],
      ['Gate v2.1', gate],
      ['Decision', r.decision_source === 'llm' ? 'LLM proposal' : r.decision_source === 'milp' ? 'exact program (fallback)' : escapeHtml(r.decision_source || '—')],
      ['Placed on', r.cluster ? `<strong>${r.cluster}</strong>` : escapeHtml(r.outcome)],
      ['Benchmark Job', r.job_name ? `<code>${escapeHtml(r.job_name)}</code>` : '—'],
      ['Dispatched → completed', `${fmtTimeSec(r.dispatched_at)} → ${fmtTimeSec(r.completed_at)} UTC`],
      ['Measured P95 · errors', r.p95_ms == null ? '—' : `${r.p95_ms} ms · ${(r.error_rate * 100).toFixed(2)}% (SLO 500 ms, 5%)`],
      ['SLO', r.slo_passed == null ? '—' : r.slo_passed ? '<span class="state-ok">passed</span>' : '<span class="txt-hold">failed</span>'],
    ];
    if (r.execution_failure_reason) rows.push(['Execution', `<span class="txt-hold">${escapeHtml(r.execution_failure_reason)}</span>`]);
    return `
      <div class="cycle-receipt-grid">
        <div>${rows.map(([k, v]) => `<div class="metadata-row"><span class="meta-key">${k}</span><span class="meta-val">${v}</span></div>`).join('')}</div>
        ${r.log_excerpt && r.log_excerpt.length ? `<pre class="cycle-log">${escapeHtml(r.log_excerpt.join('\n'))}</pre>` : ''}
      </div>`;
  }

  renderClusters() {
    const describe = (s) => {
      if (!s) return { text: 'checking…', cls: 'unknown', title: '' };
      if (s.state === 'ready') return { text: `ready · ${s.running}/${s.total} pods`, cls: 'ok', title: 'All pods running and ready' };
      if (s.state === 'degraded') return { text: `degraded · ${s.running}/${s.total} pods ready`, cls: 'warn', title: s.reason || '' };
      if (s.state === 'empty') return { text: 'no pods', cls: 'warn', title: s.reason || '' };
      if (s.state === 'unreachable') return { text: 'unreachable', cls: 'down', title: `${s.reason || ''}${s.detail ? `\n${s.detail}` : ''}` };
      return { text: 'unknown', cls: 'unknown', title: s.reason || '' };
    };
    const st = this.cluster || {};
    const failed = !!st.error;
    for (const [key, ctx, headerId, hudId, cardId, dotId] of [
      ['primary', 'aks-primary-eastus2', 'headerClusterPrimary', 'metaPrimaryPods', 'metaPrimaryPodsCard', 'primaryHudDot'],
      ['secondary', 'aks-secondary-westus2', 'headerClusterSecondary', 'metaSecondaryPods', 'metaSecondaryPodsCard', 'secondaryHudDot'],
    ]) {
      const d = failed ? { text: 'status check failed', cls: 'unknown', title: st.error } : describe(st[key]);
      const header = document.getElementById(headerId);
      header.innerHTML = `<span class="pulse-dot ${d.cls}"></span><span class="badge-region">${REGION_OF_CLUSTER[ctx]}</span><span class="badge-state"> · ${d.text}</span>`;
      header.title = `${ctx}: ${d.title || 'live kubectl check from the web host'}`;
      for (const id of [hudId, cardId]) {
        const el = document.getElementById(id);
        if (!el) continue;
        const reason = st[key] && st[key].reason && id === cardId ? ` — ${st[key].reason}` : '';
        el.textContent = `${d.text}${reason}`;
        el.className = `meta-val state-${d.cls}`;
        el.title = d.title || '';
      }
      const dot = document.getElementById(dotId);
      if (dot) dot.className = `pulse-dot ${d.cls}`;
    }
    const ts = document.getElementById('metaClusterTimestamp');
    if (ts) ts.textContent = st.checked_at ? fmtTime(st.checked_at) : '—';
    this.renderCycle();
    this.listeners.forEach(fn => fn(this));
  }

  clusterSummary() {
    const st = this.cluster || {};
    const one = (k) => (st[k] ? st[k].state : 'unknown');
    return { primary: one('primary'), secondary: one('secondary'),
      reason: (st.primary && st.primary.reason) || (st.secondary && st.secondary.reason) || st.error || null };
  }
}

function escapeHtml(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

document.addEventListener('DOMContentLoaded', () => {
  const anim = new TrafficDirectorAnimation('trafficCanvas');
  const dashboard = new DashboardController(anim);
  dashboard.simulation = new PlacementSimulation(dashboard);
  const walkthrough = new AIThinkingPanel(dashboard);
  window.dashboard = dashboard;
  window.thinkingPanel = walkthrough;

  new MetricsViewController(dashboard);
  new GraphTopologyViewer('dagSvg', walkthrough);

  const openFromHud = (hud, source) => {
    if (!hud) return;
    const open = () => walkthrough.trigger(source);
    hud.addEventListener('click', (e) => {
      // Card text opens the walkthrough. Sliders and the explicit child
      // button keep their own actions without also opening the card.
      if (e.target !== hud && e.target.closest('.carbon-meter, button, input, select, a')) return;
      open();
    });
    hud.addEventListener('keydown', (e) => {
      if (e.target === hud && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault(); open();
      }
    });
  };
  openFromHud(document.getElementById('primaryClusterHud'), 'primary');
  openFromHud(document.getElementById('secondaryClusterHud'), 'secondary');
  openFromHud(document.getElementById('primaryWalkthrough'), 'primary');
  openFromHud(document.getElementById('secondaryWalkthrough'), 'secondary');

  document.getElementById('btnRunCycle').addEventListener('click', () => dashboard.runCycle());
  document.getElementById('btnRunSpike').addEventListener('click', () => dashboard.runCycle('spike'));
  document.getElementById('btnStopCycle').addEventListener('click', () => dashboard.stopCycle());
  document.getElementById('btnResetPage').addEventListener('click', () => dashboard.resetPage());
  document.getElementById('flowSpeedSelect').addEventListener('change', (e) => {
    anim.flowSpeed = parseFloat(e.target.value);
  });

  dashboard.start();
});
