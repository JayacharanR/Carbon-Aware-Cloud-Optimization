/** Adjustable, browser-only placement demonstration. Never calls a run API. */
class PlacementSimulation {
  constructor(dashboard) {
    this.dashboard = dashboard;
    this.enabled = false;
    this.region = null;
    this.phase = 'idle';
    this.events = [];
    this.timers = [];
    this.seeded = false;
    this.edited = false;
    this.values = { eastus2: 400, westus2: 150 };
    this.inputs = { eastus2: document.getElementById('simEastCarbon'), westus2: document.getElementById('simWestCarbon') };
    for (const [region, input] of Object.entries(this.inputs)) {
      const update = value => {
        if (input.disabled) return;
        this.edited = true;
        this.values[region] = Math.max(0, Math.min(1200, Math.round(value)));
        if (this.enabled) this.schedule();
        else this.dashboard.render();
      };
      input.addEventListener('input', () => update(Number(input.value)));
      // Keep range interaction consistent in embedded browsers as well as
      // Chrome. Preserve native range semantics for assistive technology.
      input.addEventListener('keydown', event => {
        const current = this.values[region];
        const targets = { Home: 0, End: 1200, ArrowLeft: current - 1, ArrowDown: current - 1,
          ArrowRight: current + 1, ArrowUp: current + 1, PageDown: current - 100, PageUp: current + 100 };
        if (Object.hasOwn(targets, event.key)) {
          event.preventDefault();
          update(targets[event.key]);
        }
      });
      const pointValue = event => {
        const rect = input.getBoundingClientRect();
        return (event.clientX - rect.left) / rect.width * 1200;
      };
      input.addEventListener('pointerdown', event => {
        if (input.disabled) return;
        event.preventDefault();
        input.focus();
        input.setPointerCapture(event.pointerId);
        update(pointValue(event));
      });
      input.addEventListener('pointermove', event => {
        if (input.hasPointerCapture(event.pointerId)) update(pointValue(event));
      });
      input.addEventListener('pointerup', event => {
        if (input.hasPointerCapture(event.pointerId)) input.releasePointerCapture(event.pointerId);
      });
    }
    document.getElementById('btnRunSimulation').addEventListener('click', () => this.start());
    document.getElementById('btnExitSimulation').addEventListener('click', () => this.exit());
    document.getElementById('btnSwapCarbon').addEventListener('click', () => {
      [this.values.eastus2, this.values.westus2] = [this.values.westus2, this.values.eastus2];
      this.edited = true;
      this.schedule();
    });
  }

  start() {
    if (this.dashboard.cycleActive() || this.dashboard.controlBusy) return;
    const previous = this.dashboard.placement().region;
    this.region = previous || (this.values.eastus2 >= this.values.westus2 ? 'eastus2' : 'westus2');
    this.enabled = true;
    this.events = [];
    window.thinkingPanel?.close();
    this.schedule();
  }

  exit() {
    this.clearTimers();
    this.enabled = false;
    this.phase = 'idle';
    this.events = [];
    this.dashboard.render();
  }

  clearTimers() {
    this.timers.forEach(clearTimeout);
    this.timers = [];
  }

  schedule() {
    if (!this.enabled) return;
    this.clearTimers();
    const { eastus2: east, westus2: west } = this.values;
    const from = this.region;
    const target = east === west ? from : east < west ? 'eastus2' : 'westus2';
    const baseline = this.values[from];
    const best = this.values[target];
    this.phase = 'inputs';
    this.events = [`Simulation inputs: eastus2 ${east}, westus2 ${west} gCO₂eq/kWh.`];
    this.dashboard.render();
    const later = (ms, phase, text, move = false) => this.timers.push(setTimeout(() => {
      if (!this.enabled) return;
      this.phase = phase;
      if (move) this.region = target;
      this.events.push(text);
      this.dashboard.render();
    }, ms));
    later(650, 'compare', east === west ? 'Equal intensities. Keep the current simulated placement.' : `${target} is the lower-carbon choice. Both regions are assumed feasible.`);
    later(1400, 'switch', from === target ? `Keep the simulated workload on ${target}.` : `Switch simulated placement from ${from} to ${target}.`, true);
    const saving = baseline > 0 ? ((baseline - best) / baseline * 100).toFixed(1) : '0.0';
    later(2150, 'complete', `Simulated placement: ${target}. Intensity ${best} gCO₂eq/kWh; ${saving}% lower than the previous placement at the new inputs. No AKS execution or measured emissions saving.`);
  }

  render() {
    // Seed once from valid live readings; polling must never overwrite edits.
    if (!this.seeded && !this.edited && !this.enabled && this.dashboard.liveStatus() === 'ok') {
      this.values = { eastus2: Math.min(1200, Math.round(this.dashboard.live.primary.intensity)), westus2: Math.min(1200, Math.round(this.dashboard.live.secondary.intensity)) };
      this.seeded = true;
    }
    const busy = this.dashboard.cycleActive() || this.dashboard.controlBusy;
    for (const [region, prefix] of [['eastus2', 'simEast'], ['westus2', 'simWest']]) {
      this.inputs[region].value = this.values[region];
      this.inputs[region].disabled = busy;
      document.getElementById(`${prefix}Value`).value = this.values[region];
      document.getElementById(`${prefix}Meter`).value = this.values[region];
    }
    document.getElementById('btnRunSimulation').disabled = busy || this.enabled;
    document.getElementById('btnSwapCarbon').disabled = busy || !this.enabled;
    document.getElementById('btnExitSimulation').disabled = busy || !this.enabled;
    const stages = ['inputs', 'compare', 'switch', 'complete'];
    document.querySelectorAll('[data-sim-step]').forEach(el => {
      const i = stages.indexOf(el.dataset.simStep), current = stages.indexOf(this.phase);
      el.className = this.enabled && i === current ? 'active' : this.enabled && i < current ? 'done' : '';
    });
    document.getElementById('simulationStatus').textContent = this.enabled
      ? `SIMULATION · ${this.events[this.events.length - 1]}`
      : 'Set the meters, then run the simulation. No model is called and no AKS workload is started.';
    const log = document.getElementById('simulationEvents');
    log.replaceChildren(...this.events.map(text => {
      const item = document.createElement('li'); item.textContent = text; return item;
    }));
  }
}
