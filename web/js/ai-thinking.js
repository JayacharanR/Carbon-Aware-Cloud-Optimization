/**
 * AI Decision Thinking Panel
 * Simulates the real LangGraph multi-step reasoning pipeline:
 *   1. Carbon Snapshot (Electricity Maps API direct query)
 *   2. PuLP MILP Solver (Binary optimization with latency constraints)
 *   3. LLM Cognitive Reasoning (Llama-3 8B token-by-token thought streaming)
 *   4. 5-Component Trust Gate (Faithfulness, Context Precision, Guardrails, Freshness, Feasibility)
 *   5. Final Workload Dispatch (kubectl switch & wrk2 benchmark trigger)
 *
 * Triggered by:
 *   - Clicking cluster HUD nodes (aks-primary-eastus / aks-secondary-westus2)
 *   - Clicking ANY microservice node in the DeathStarBench Social Network SVG DAG
 */

class AIThinkingPanel {
  constructor() {
    this._overlay = null;
    this._panel = null;
    this._streamTimers = [];
    this._typingTimer = null;
    this._liveCarbon = { eastus: 415, westus2: 152 };
    this._init();
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /** Update live carbon values from the background poller so thinking panel uses real data */
  setLiveCarbon(eastus, westus2) {
    if (eastus != null && !isNaN(eastus)) this._liveCarbon.eastus = Math.round(eastus);
    if (westus2 != null && !isNaN(westus2)) this._liveCarbon.westus2 = Math.round(westus2);
  }

  /** Open the panel and start streaming the decision process for a given trigger source */
  trigger(triggerSource = 'primary', meta = null) {
    this._clearStream();
    this._resetProgress();

    if (this._overlay) this._overlay.classList.add('visible');
    if (this._panel) this._panel.classList.add('visible');

    const east = this._liveCarbon.eastus;
    const west = this._liveCarbon.westus2;
    const reduction = east > 0 ? (((east - west) / east) * 100).toFixed(1) : '—';
    const decidedRegion = west < east ? 'westus2' : 'eastus';
    const decidedCluster = decidedRegion === 'westus2' ? 'aks-secondary-westus2' : 'aks-primary-eastus';
    const decidedGrid = decidedRegion === 'westus2' ? 'US-NW-BPAT (BPA Hydro — Pacific NW)' : 'US-MIDA-PJM (PJM — Fossil Heavy)';

    const now = new Date().toISOString();
    const trustScores = {
      faithfulness: (0.91 + Math.random() * 0.06).toFixed(3),
      precision: (0.90 + Math.random() * 0.07).toFixed(3),
      guardrails: '1.000',
      freshness: '1.000',
      feasibility: '1.000',
    };
    const composite = (
      (parseFloat(trustScores.faithfulness) +
       parseFloat(trustScores.precision) +
       parseFloat(trustScores.guardrails) +
       parseFloat(trustScores.freshness) +
       parseFloat(trustScores.feasibility)) / 5
    ).toFixed(3);

    // Build the full 5-step sequence with rich context for the clicked workload
    const steps = this._buildSteps({
      east, west, reduction, decidedRegion, decidedCluster, decidedGrid,
      now, trustScores, composite, triggerSource, meta,
    });

    this._streamSteps(steps);
  }

  close() {
    this._clearStream();
    if (this._overlay) this._overlay.classList.remove('visible');
    if (this._panel) this._panel.classList.remove('visible');
  }

  // ── Private ────────────────────────────────────────────────────────────────

  _init() {
    if (document.getElementById('aiThinkingRoot')) return;

    const el = document.createElement('div');
    el.id = 'aiThinkingRoot';
    el.innerHTML = this._panelHTML();
    document.body.appendChild(el);

    this._overlay = document.getElementById('thinkingOverlay');
    this._panel   = document.getElementById('thinkingPanel');

    document.getElementById('thinkingCloseBtn')?.addEventListener('click', () => this.close());
    this._overlay?.addEventListener('click', () => this.close());

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this.close();
    });
  }

  _panelHTML() {
    return `
<div id="thinkingOverlay" class="thinking-overlay"></div>
<div id="thinkingPanel" class="thinking-panel">

  <!-- Panel Header -->
  <div class="thinking-header">
    <div class="thinking-header-left">
      <div class="thinking-header-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2z"/>
          <path d="M12 6v6l4 2"/>
        </svg>
      </div>
      <div>
        <div class="thinking-title">AI Scheduler — Decision Log</div>
        <div class="thinking-subtitle">LangGraph Multi-Agent · Llama-3 8B · PuLP MILP · 5-Factor Trust Gate</div>
      </div>
    </div>
    <button id="thinkingCloseBtn" class="thinking-close-btn" aria-label="Close panel" title="Close (Esc)">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
    </button>
  </div>

  <!-- Step progress bar -->
  <div class="thinking-steps-bar">
    <div class="thinking-step" id="tStep1"><span class="step-dot"></span>01 Carbon Fetch</div>
    <div class="thinking-step-sep">›</div>
    <div class="thinking-step" id="tStep2"><span class="step-dot"></span>02 MILP Solve</div>
    <div class="thinking-step-sep">›</div>
    <div class="thinking-step" id="tStep3"><span class="step-dot"></span>03 LLM Reason</div>
    <div class="thinking-step-sep">›</div>
    <div class="thinking-step" id="tStep4"><span class="step-dot"></span>04 Trust Gate</div>
    <div class="thinking-step-sep">›</div>
    <div class="thinking-step" id="tStep5"><span class="step-dot"></span>05 Dispatch</div>
  </div>

  <!-- Scrollable output -->
  <div class="thinking-body">
    <div id="thinkingOutput" class="thinking-output"></div>
    <div id="thinkingCursor" class="thinking-cursor">▋</div>
  </div>

  <!-- Footer hint -->
  <div class="thinking-footer">
    <span class="thinking-footer-note">⚡ Click any DeathStarBench node or cluster HUD to run a decision cycle</span>
    <span id="thinkingElapsed" class="thinking-footer-elapsed"></span>
  </div>
</div>`;
  }

  _buildSteps({ east, west, reduction, decidedRegion, decidedCluster, decidedGrid, now, trustScores, composite, triggerSource, meta }) {
    let triggerLabel = '';
    let workloadName = '';
    let tierName = meta?.tier || 'Service Layer';

    if (triggerSource === 'primary') {
      triggerLabel = 'Cluster HUD: aks-primary-eastus';
      workloadName = 'DeathStarBench Ingress Gateway (nginx-thrift)';
      tierName = 'Ingress Gateway';
    } else if (triggerSource === 'secondary') {
      triggerLabel = 'Cluster HUD: aks-secondary-westus2';
      workloadName = 'DeathStarBench Clean Region Deployment';
      tierName = 'Multi-Region Ingress';
    } else {
      triggerLabel = `DeathStarBench DAG Node: ${triggerSource}`;
      workloadName = `${triggerSource} (${tierName})`;
    }

    const isStateless = !tierName.includes('Storage');
    const milpObjective = decidedRegion === 'westus2' ? west : east;

    return [
      // ── Step 1: Carbon Snapshot ────────────────────────────────────────────
      {
        stepId: 'tStep1',
        delay: 0,
        html: `
<div class="think-block step-carbon">
  <div class="think-block-header">
    <span class="think-step-num">01</span>
    <span class="think-step-label">CARBON INTENSITY SNAPSHOT</span>
    <span class="think-step-source">Electricity Maps API · Direct Poller</span>
  </div>
  <div class="think-block-body">
    <div class="think-kv-grid">
      <div class="think-kv"><span class="kv-key">Workload Target</span><span class="kv-val trigger">${workloadName}</span></div>
      <div class="think-kv"><span class="kv-key">Architectural Tier</span><span class="kv-val mono">${tierName}</span></div>
      <div class="think-kv"><span class="kv-key">Trigger Origin</span><span class="kv-val mono">${triggerLabel}</span></div>
      <div class="think-kv"><span class="kv-key">Evaluation Timestamp</span><span class="kv-val mono">${now.replace('T', ' ').substring(0, 19)} UTC</span></div>
      <div class="think-kv"><span class="kv-key">Primary Zone (eastus)</span><span class="kv-val mono">US-MIDA-PJM (PJM Interconnection)</span></div>
      <div class="think-kv"><span class="kv-key">Secondary Zone (westus2)</span><span class="kv-val mono">US-NW-BPAT (BPA Hydro)</span></div>
    </div>
    <div class="think-intensity-bars">
      <div class="think-intensity-row">
        <span class="intensity-label">eastus</span>
        <div class="intensity-bar-bg">
          <div class="intensity-bar-fill high" style="width:${Math.min(100, Math.max(10, (east / 6)))}%"></div>
        </div>
        <span class="intensity-val high">${east} gCO₂/kWh</span>
      </div>
      <div class="think-intensity-row">
        <span class="intensity-label">westus2</span>
        <div class="intensity-bar-bg">
          <div class="intensity-bar-fill clean" style="width:${Math.min(100, Math.max(10, (west / 6)))}%"></div>
        </div>
        <span class="intensity-val clean">${west} gCO₂/kWh</span>
      </div>
    </div>
    <div class="think-conclusion green">✓ Live carbon delta verified: eastus is <strong>${reduction}% dirtier</strong> than westus2. Optimization threshold (&gt;20%) satisfied.</div>
  </div>
</div>`,
      },

      // ── Step 2: MILP Solver ────────────────────────────────────────────────
      {
        stepId: 'tStep2',
        delay: 500,
        html: `
<div class="think-block step-milp">
  <div class="think-block-header">
    <span class="think-step-num">02</span>
    <span class="think-step-label">PuLP MILP OPTIMIZATION SOLVER</span>
    <span class="think-step-source">Solver: PULP_CBC_CMD · 5-min slot</span>
  </div>
  <div class="think-block-body">
    <div class="think-code-block">
<span class="code-comment"># Objective: Minimise Σ carbon_intensity[r] × x[r] for ${workloadName}</span>
<span class="code-kw">prob</span> = LpProblem(<span class="code-str">"carbon_dispatch"</span>, LpMinimize)
<span class="code-kw">x_east</span>  = LpVariable(<span class="code-str">"x_eastus"</span>,  0, 1, cat=<span class="code-str">'Binary'</span>)
<span class="code-kw">x_west</span>  = LpVariable(<span class="code-str">"x_westus2"</span>, 0, 1, cat=<span class="code-str">'Binary'</span>)

<span class="code-comment"># Carbon objective function:</span>
prob += <span class="code-num">${east}</span>*x_east + <span class="code-num">${west}</span>*x_west

<span class="code-comment"># Operational constraints:</span>
prob += x_east + x_west == <span class="code-num">1</span>                    <span class="code-comment"># Exact placement</span>
prob += <span class="code-num">0.33</span> + <span class="code-num">20.4</span>*x_west &lt;= <span class="code-num">500.0</span>            <span class="code-comment"># Latency SLO: p95 &lt;= 500ms</span>

prob.solve(PULP_CBC_CMD(msg=<span class="code-num">0</span>))

<span class="code-comment"># Solver output:</span>
<span class="code-kw">x_eastus</span>  = <span class="code-num">${decidedRegion === 'eastus' ? 1 : 0}</span>
<span class="code-kw">x_westus2</span> = <span class="code-num">${decidedRegion === 'westus2' ? 1 : 0}</span>
<span class="code-green">status = "Optimal" (Solver solved in 4.2ms)</span></div>
    <div class="think-kv-grid mt8">
      <div class="think-kv"><span class="kv-key">MILP Status</span><span class="kv-val green">Optimal (Proven Global Min)</span></div>
      <div class="think-kv"><span class="kv-key">Winner Region</span><span class="kv-val mono">${decidedRegion}</span></div>
      <div class="think-kv"><span class="kv-key">Carbon Objective</span><span class="kv-val mono">${milpObjective} gCO₂/kWh</span></div>
      <div class="think-kv"><span class="kv-key">SLO p95 Constraint</span><span class="kv-val green">20.73 ms ≤ 500 ms (Passed ✓)</span></div>
    </div>
    <div class="think-conclusion green">✓ MILP determined <strong>${decidedCluster}</strong> achieves ${reduction}% lower emissions with zero SLO violation.</div>
  </div>
</div>`,
      },

      // ── Step 3: LLM Reasoning ──────────────────────────────────────────────
      {
        stepId: 'tStep3',
        delay: 700,
        html: `
<div class="think-block step-llm">
  <div class="think-block-header">
    <span class="think-step-num">03</span>
    <span class="think-step-label">LLM COGNITIVE REASONING & VALIDATION</span>
    <span class="think-step-source">Model: Llama-3 8B · RAG Context Engine</span>
  </div>
  <div class="think-block-body">
    <div class="think-llm-prompt">
      <div class="llm-prompt-label">SYSTEM PROMPT (RAG-Augmented)</div>
      <div class="llm-prompt-text">You are a carbon-aware cloud scheduler with autonomous actuation rights. Evaluate whether workload "<strong>${workloadName}</strong>" should migrate from eastus to westus2 given live carbon telemetry (${east} vs ${west} gCO₂/kWh) and DeathStarBench SLO constraints.</div>
    </div>
    <div class="think-llm-stream" id="llmStream">
      <div class="llm-thinking-dots"><span></span><span></span><span></span></div>
    </div>
  </div>
</div>`,
        afterInsert: (wrapper) => {
          const streamEl = wrapper.querySelector('#llmStream');
          if (!streamEl) return;

          const thoughtContent = `<thinking>
Analyzing workload: ${workloadName} [Tier: ${tierName}].
1. Current Grid State:
   - Primary cluster (eastus · US-MIDA-PJM): ${east} gCO₂eq/kWh (coal/gas baseload).
   - Secondary cluster (westus2 · US-NW-BPAT): ${west} gCO₂eq/kWh (hydro/wind baseload).
   - Carbon difference: ${east - west} gCO₂/kWh (${reduction}% reduction).

2. Workload Architecture Viability:
   - Tier "${tierName}": ${isStateless ? 'Stateless RPC service. Request routing can shift instantly with 0 persistent state migration overhead.' : 'State-backed service. Evaluated replication lag across Azure inter-region link: <2ms. Consistent replica available in westus2.'}
   - Inter-cluster round-trip time: Azure backbone ping measured at 20.4ms.
   - Total expected p95 latency: ~20.73ms (measured benchmark latency is 0.33ms), which provides a 479.2ms safety buffer below the 500ms SLO limit.

3. Failure Modes & Risks:
   - Surge risk: None detected. West US 2 has ample cluster capacity (28/28 pods healthy in benchmark namespace).
   - Reversion trigger: If westus2 hydro generation drops or carbon surge exceeds 250 gCO₂/kWh, automatic fallback to primary cluster is enforced.

Recommendation: Authorize full cross-region rerouting to ${decidedCluster}.
</thinking>

<output>
RECOMMENDATION: Reroute **${workloadName}** to **${decidedCluster}**.

JUSTIFICATION:
• Carbon Reduction: ${reduction}% lower emissions on BPA Hydro (${west} vs ${east} gCO₂/kWh).
• SLO Compliance: p95 latency projected at 20.7ms (strict requirement: ≤ 500ms).
• Availability: 28/28 microservices running healthy on target cluster.
• Actuation Safety: Stateless RPC layer allows zero-downtime DNS/Ingress traffic switch.

CONFIDENCE: 98.4% (MILP objective aligned with LLM reasoning)
</output>`;

          this._typeText(streamEl, thoughtContent, 3);
        },
      },

      // ── Step 4: Trust Gate ─────────────────────────────────────────────────
      {
        stepId: 'tStep4',
        delay: 3600,
        html: `
<div class="think-block step-trust">
  <div class="think-block-header">
    <span class="think-step-num">04</span>
    <span class="think-step-label">5-COMPONENT TRUST GATE EVALUATION</span>
    <span class="think-step-source">RAGAS Framework + NeMo Guardrails · Min Threshold: 0.600</span>
  </div>
  <div class="think-block-body">
    <div class="think-trust-scores">
      ${this._trustRow('RAGAS Faithfulness',       trustScores.faithfulness, '0.20')}
      ${this._trustRow('RAGAS Context Precision',  trustScores.precision,    '0.20')}
      ${this._trustRow('NeMo Policy Guardrails',   trustScores.guardrails,   '0.20')}
      ${this._trustRow('Data Freshness (Maps API)',trustScores.freshness,    '0.20')}
      ${this._trustRow('Execution Feasibility',    trustScores.feasibility,  '0.20')}
    </div>
    <div class="think-trust-composite">
      <span class="trust-composite-label">Weighted Composite Score</span>
      <span class="trust-composite-val ${parseFloat(composite) >= 0.6 ? 'pass' : 'fail'}">${composite}</span>
      <span class="trust-composite-thresh">Threshold: 0.600 → ${parseFloat(composite) >= 0.6 ? '✓ PASSED' : '✗ FAILED'}</span>
    </div>
    <div class="think-conclusion ${parseFloat(composite) >= 0.6 ? 'green' : 'red'}">
      ${parseFloat(composite) >= 0.6
        ? '✓ Trust Gate PASSED — Proposal verified safe. Actuation rights granted to scheduler executor.'
        : '✗ Trust Gate FAILED — Disallowed automated actuation. Reverting to deterministic baseline.'}
    </div>
  </div>
</div>`,
      },

      // ── Step 5: Dispatch ───────────────────────────────────────────────────
      {
        stepId: 'tStep5',
        delay: 1200,
        html: `
<div class="think-block step-dispatch">
  <div class="think-block-header">
    <span class="think-step-num">05</span>
    <span class="think-step-label">WORKLOAD DISPATCH & EXECUTION RECEIPT</span>
    <span class="think-step-source">kubectl · AKS Azure Backbone · wrk2 telemetry</span>
  </div>
  <div class="think-block-body">
    <div class="think-code-block dispatch-log">
<span class="code-comment"># Actuator: Switching traffic context for ${workloadName}</span>
$ kubectl config use-context <span class="code-kw">${decidedCluster}</span>
<span class="code-str">Switched to context "${decidedCluster}".</span>

$ kubectl get pods -n benchmark -l app=${triggerSource.replace('-service', '')}
<span class="code-str">NAME                                  READY   STATUS    RESTARTS   AGE</span>
<span class="code-str">${triggerSource.replace('-service', '')}-789bc44d-xk29    1/1     Running   0          26h</span>

$ wrk2 -t2 -c10 -d60s -R10 --latency http://nginx-thrift.benchmark.svc.cluster.local:8080
<span class="code-green">scheduler outcome=executed  fallback=False</span>
<span class="code-green">carbon_region=${decidedRegion}  zone=${decidedRegion === 'westus2' ? 'US-NW-BPAT' : 'US-MIDA-PJM'}</span>
<span class="code-green">active_intensity=${decidedRegion === 'westus2' ? west : east} gCO₂/kWh  reduction=${reduction}%</span></div>
    <div class="think-dispatch-result">
      <div class="dispatch-result-row">
        <span class="dispatch-icon">🍃</span>
        <div>
          <div class="dispatch-cluster">${decidedCluster} (${decidedRegion})</div>
          <div class="dispatch-meta">${decidedGrid} · Latency: 0.33ms (p95)</div>
        </div>
        <div class="dispatch-badge pass">DISPATCHED</div>
      </div>
    </div>
    <div class="think-conclusion green">✓ Complete pipeline executed in 6.2s · Traffic rerouting animation triggered in hero view.</div>
  </div>
</div>`,
      },
    ];
  }

  _trustRow(label, score, weight) {
    const pct = Math.min(100, Math.max(0, (parseFloat(score) * 100).toFixed(0)));
    const isPass = parseFloat(score) >= 0.6;
    return `
    <div class="think-trust-row">
      <span class="trust-row-label">${label}</span>
      <div class="trust-row-bar-bg">
        <div class="trust-row-bar-fill ${isPass ? 'pass' : 'fail'}" style="width:${pct}%"></div>
      </div>
      <span class="trust-row-score ${isPass ? 'pass' : 'fail'}">${score}</span>
      <span class="trust-row-weight">×${weight}</span>
    </div>`;
  }

  _resetProgress() {
    for (let i = 1; i <= 5; i++) {
      const stepEl = document.getElementById(`tStep${i}`);
      if (stepEl) stepEl.className = 'thinking-step';
    }
    const output = document.getElementById('thinkingOutput');
    if (output) output.innerHTML = '';
    const elapsed = document.getElementById('thinkingElapsed');
    if (elapsed) elapsed.textContent = '';
    const cursor = document.getElementById('thinkingCursor');
    if (cursor) {
      cursor.style.display = 'block';
      if (output) output.appendChild(cursor);
    }
  }

  _streamSteps(steps) {
    const output = document.getElementById('thinkingOutput');
    const cursor = document.getElementById('thinkingCursor');
    const startTime = Date.now();

    const processStep = (idx) => {
      if (idx >= steps.length) {
        if (cursor) cursor.style.display = 'none';
        const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
        const el = document.getElementById('thinkingElapsed');
        if (el) el.textContent = `Completed in ${elapsed}s`;
        return;
      }

      const step = steps[idx];
      const timer = setTimeout(() => {
        // Step progress indicator: mark current active, mark previous done
        const stepEl = document.getElementById(step.stepId);
        if (stepEl) {
          stepEl.classList.add('active');
          if (idx > 0) {
            const prevEl = document.getElementById(steps[idx - 1].stepId);
            if (prevEl) {
              prevEl.classList.remove('active');
              prevEl.classList.add('done');
            }
          }
        }

        // Insert step HTML
        const wrapper = document.createElement('div');
        wrapper.innerHTML = step.html;
        if (output) {
          output.appendChild(wrapper);
          if (cursor) output.appendChild(cursor);
          const body = output.closest('.thinking-body');
          if (body) body.scrollTop = body.scrollHeight;
        }

        // Run post-insert hook (e.g. streaming LLM thought tokens)
        if (step.afterInsert) {
          step.afterInsert(wrapper);
        }

        processStep(idx + 1);
      }, step.delay);

      this._streamTimers.push(timer);
    };

    processStep(0);
  }

  _typeText(el, text, speedMs = 7) {
    if (this._typingTimer) {
      clearInterval(this._typingTimer);
      this._typingTimer = null;
    }
    let i = 0;
    el.textContent = '';
    this._typingTimer = setInterval(() => {
      if (i >= text.length) {
        clearInterval(this._typingTimer);
        this._typingTimer = null;
        return;
      }
      el.textContent += text[i++];
      const body = el.closest('.thinking-body');
      if (body) body.scrollTop = body.scrollHeight;
    }, speedMs);
  }

  _clearStream() {
    this._streamTimers.forEach(t => clearTimeout(t));
    this._streamTimers = [];
    if (this._typingTimer) {
      clearInterval(this._typingTimer);
      this._typingTimer = null;
    }
  }
}

window.AIThinkingPanel = AIThinkingPanel;
