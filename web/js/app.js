/**
 * Main Application Controller for Carbon-Aware Cloud Showcase
 * v2.0 — live carbon data pipeline from /api/live-carbon
 */

// ---------------------------------------------------------------------------
// Live Carbon Poller — fetches /api/live-carbon every 60 seconds
// and drives the traffic animation from real Electricity Maps data.
// ---------------------------------------------------------------------------
class LiveCarbonPoller {
  /**
   * @param {TrafficDirectorAnimation} anim - The traffic animation instance
   * @param {number} intervalMs - Polling interval in milliseconds (default 60s)
   */
  constructor(anim, intervalMs = 60000) {
    this.anim = anim;
    this.intervalMs = intervalMs;
    this._timer = null;
    this._lastDecidedRegion = null;

    // Track cumulative carbon saved — seeded from 0, grows from real difference
    this.cumulativeCarbonSavedGCO2 = 0;
    this._savingsTimer = null;
  }

  start() {
    // Fetch immediately on page load, then on interval
    this._fetchAndApply();
    this._timer = setInterval(() => this._fetchAndApply(), this.intervalMs);
    // Update cumulative savings counter every second
    this._savingsTimer = setInterval(() => this._tickSavings(), 1000);
  }

  stop() {
    clearInterval(this._timer);
    clearInterval(this._savingsTimer);
  }

  async _fetchAndApply() {
    try {
      const resp = await fetch('/api/live-carbon');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();

      const eastIntensity = data.eastus?.intensity ?? null;
      const westIntensity = data.westus2?.intensity ?? null;
      const updatedAt = data.eastus?.updated_at ?? data.last_poll_at ?? null;
      const source = data.eastus?.source ?? 'live';

      // Push live values into the animation engine (updates node labels, HUD)
      this.anim.setLiveCarbonValues(eastIntensity, westIntensity, updatedAt, source);

      // Auto-reroute decision driven by real carbon data:
      // If westus2 intensity is meaningfully lower (>10% reduction), optimise.
      // If they're similar (within 10%), fall back to baseline (eastus primary).
      const decidedRegion = data.decided_region;
      if (decidedRegion && decidedRegion !== this._lastDecidedRegion) {
        this._lastDecidedRegion = decidedRegion;
        const targetState = decidedRegion === 'westus2' ? 'optimized' : 'baseline';
        this.anim.setRoute(targetState);

        // Announce the live decision in the status banner
        this._announceLiveDecision(decidedRegion, eastIntensity, westIntensity, data.carbon_reduction_pct);
      }

      // Sync the carbon-diff metric on the HUD
      this.anim.updateHudState();

      // Update the live carbon stats panel if present
      this._updateLiveStatsPanel(data);

    } catch (err) {
      console.warn('[LiveCarbonPoller] Failed to fetch /api/live-carbon:', err);
      // Mark animation as degraded but don't crash
      this.anim.liveCarbon.source = 'error';
    }
  }

  _announceLiveDecision(region, eastInt, westInt, reductionPct) {
    const banner = document.getElementById('statusBannerMsg');
    if (!banner) return;

    if (region === 'westus2') {
      const saving = (eastInt && westInt) ? `${Math.round(eastInt - westInt)} gCO₂/kWh saved` : '';
      banner.innerHTML = `<span class="live-indicator">● LIVE DECISION</span> Electricity Maps data: PJM=${Math.round(eastInt || 0)} vs BPA=${Math.round(westInt || 0)} gCO₂/kWh. Scheduler routing to <strong>aks-secondary-westus2</strong>${saving ? ` — ${saving}` : ''}.`;
    } else {
      banner.innerHTML = `<span class="live-indicator crimson">● MONITORING</span> Carbon parity detected. Holding workload on <strong>aks-primary-eastus</strong>. PJM=${Math.round(eastInt || 0)} gCO₂/kWh.`;
    }
  }

  _updateLiveStatsPanel(data) {
    // Update east intensity display
    const eastEl = document.getElementById('liveEastIntensity');
    if (eastEl && data.eastus?.intensity !== null) {
      eastEl.textContent = `${Math.round(data.eastus.intensity)} gCO₂/kWh`;
    }
    // Update west intensity display
    const westEl = document.getElementById('liveWestIntensity');
    if (westEl && data.westus2?.intensity !== null) {
      westEl.textContent = `${Math.round(data.westus2.intensity)} gCO₂/kWh`;
    }
    // Update decided region indicator
    const decisionEl = document.getElementById('liveDecisionRegion');
    if (decisionEl && data.decided_region) {
      decisionEl.textContent = data.decided_region === 'westus2' ? 'westus2 ✓' : 'eastus (baseline)';
      decisionEl.style.color = data.decided_region === 'westus2' ? '#a8c0dd' : '#ff6b6b';
    }
    // Update carbon reduction badge
    const reductionEl = document.getElementById('liveCarbonReduction');
    if (reductionEl && data.carbon_reduction_pct !== null) {
      reductionEl.textContent = data.carbon_reduction_pct > 0
        ? `-${data.carbon_reduction_pct}%`
        : '0% (parity)';
    }
    // Update last-poll timestamp
    const pollEl = document.getElementById('liveLastPoll');
    if (pollEl && data.last_poll_at) {
      const d = new Date(data.last_poll_at);
      pollEl.textContent = d.toLocaleTimeString();
    }
  }

  _tickSavings() {
    // Accumulate carbon savings every second based on real intensity difference
    const eastInt = this.anim.liveCarbon.eastus;
    const westInt = this.anim.liveCarbon.westus2;

    if (this.anim.state === 'optimized' && eastInt !== null && westInt !== null) {
      // Assume ~0.5 kW workload; savings per second = (east-west) * 0.5 kW / 3600 s
      const gPerSecond = ((eastInt - westInt) * 0.5) / 3600;
      if (gPerSecond > 0) {
        this.cumulativeCarbonSavedGCO2 += gPerSecond;
      }
    }

    const countEl = document.getElementById('carbonSavedCounter');
    if (countEl) {
      const kg = this.cumulativeCarbonSavedGCO2 / 1000;
      countEl.textContent = kg > 0.001 ? `${kg.toFixed(3)} kg CO₂` : '< 0.001 kg CO₂';
    }

    const evMilesEl = document.getElementById('evMilesEquivalent');
    if (evMilesEl) {
      // ~0.25 kg CO₂ per EV mile (EPA estimate)
      const miles = this.cumulativeCarbonSavedGCO2 / 250;
      evMilesEl.textContent = miles > 0 ? `${miles.toFixed(3)} mi` : '< 0.001 mi';
    }
  }
}


// ---------------------------------------------------------------------------
// DOMContentLoaded — wire everything together
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  // 1. Initialize Traffic Particle Animation Engine
  const anim = new TrafficDirectorAnimation('trafficCanvas');

  // 2. Initialize Metrics and Trust Gate Controller
  const metrics = new MetricsViewController();

  // 3. Initialize 28-Microservice Dependency DAG
  const dag = new GraphTopologyViewer('dagSvg');

  // 4. Start Live Carbon Poller — this drives rerouting from real Electricity Maps data
  const carbonPoller = new LiveCarbonPoller(anim, 60000);
  carbonPoller.start();

  // 5. Wire Interactive Toolbar Controls (manual overrides)
  const btnReroute = document.getElementById('btnTriggerReroute');
  const btnBaseline = document.getElementById('btnResetBaseline');
  const btnGridSurge = document.getElementById('btnSimulateGridSurge');
  const flowSpeedSelect = document.getElementById('flowSpeedSelect');
  const autoDemoToggle = document.getElementById('autoDemoToggle');

  if (btnReroute) {
    btnReroute.addEventListener('click', () => {
      anim.setRoute('optimized');
      pulseButton(btnReroute);
    });
  }

  if (btnBaseline) {
    btnBaseline.addEventListener('click', () => {
      anim.setRoute('baseline');
      pulseButton(btnBaseline);
    });
  }

  if (btnGridSurge) {
    btnGridSurge.addEventListener('click', async () => {
      // Show simulated grid surge then let live data decide route
      const banner = document.getElementById('statusBannerMsg');
      if (banner) {
        const eastInt = anim.liveCarbon.eastus;
        const spiked = eastInt ? Math.round(eastInt * 1.22) : 685;
        banner.innerHTML = `<span class="live-indicator crimson">⚡ GRID SURGE SIMULATED!</span> PJM East US spiked to <strong>${spiked} gCO₂eq/kWh</strong>. Trust Gate initiating automated cross-region migration…`;
      }
      setTimeout(() => {
        anim.setRoute('optimized');
      }, 1200);
    });
  }

  if (flowSpeedSelect) {
    flowSpeedSelect.addEventListener('change', (e) => {
      anim.flowSpeed = parseFloat(e.target.value);
    });
  }

  // Auto-demo toggle: when checked, allow natural live-data-driven switching.
  // When unchecked, stop the auto interval (manual control only).
  let autoTimer = null;
  function startAutoDemo() {
    // In live mode, auto-demo just reinforces the live decision every 20s
    autoTimer = setInterval(() => {
      const eastInt = anim.liveCarbon.eastus;
      const westInt = anim.liveCarbon.westus2;
      if (eastInt !== null && westInt !== null) {
        // Let live data drive
        if (westInt < eastInt * 0.9) {
          anim.setRoute('optimized');
        } else {
          anim.setRoute('baseline');
        }
      } else {
        // If live data not yet loaded, toggle for demo purposes
        if (anim.state === 'baseline') {
          anim.setRoute('optimized');
        } else {
          anim.setRoute('baseline');
        }
      }
    }, 20000);
  }

  if (autoDemoToggle) {
    autoDemoToggle.addEventListener('change', (e) => {
      if (e.target.checked) {
        startAutoDemo();
      } else {
        clearInterval(autoTimer);
      }
    });
    // Start auto demo by default for interactive presentation
    startAutoDemo();
  }

  function pulseButton(btn) {
    btn.style.transform = 'scale(0.96)';
    setTimeout(() => (btn.style.transform = ''), 150);
  }
});
