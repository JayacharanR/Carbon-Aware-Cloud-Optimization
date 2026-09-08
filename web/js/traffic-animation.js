/**
 * 60FPS Fluid Particle Traffic Animation Engine
 * Simulates real-time microservice workload routing across Azure AKS clusters
 * (aks-primary-eastus vs aks-secondary-westus2) with dynamic cubic bezier transitions.
 *
 * v2.0 — live carbon values from /api/live-carbon update node labels in real-time.
 */

class TrafficDirectorAnimation {
  constructor(canvasId) {
    this.canvas = document.getElementById(canvasId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext('2d');

    // State
    this.state = 'baseline'; // 'baseline' (eastus) or 'optimized' (westus2)
    this.transitionProgress = 0.0; // 0 = 100% east, 1 = 100% west
    this.isTransitioning = false;
    this.flowSpeed = 1.0;
    this.isPaused = false;
    this.lastFrameTime = performance.now();

    // Live carbon data (updated externally via setLiveCarbonValues)
    this.liveCarbon = {
      eastus: null,      // gCO₂/kWh
      westus2: null,
      updatedAt: null,   // ISO string
      source: 'loading', // 'live', 'loading', 'error'
    };

    // Particle pool
    this.particles = [];
    this.maxParticles = 140;
    this.spawnTimer = 0;
    this.spawnInterval = 30; // ms

    // Pulses & ripples
    this.ripples = [];

    // Colors matching palette
    this.colors = {
      bg: '#061826',
      slate: '#627C85',
      crimson: '#D62828',
      crimsonGlow: 'rgba(214, 40, 40, 0.45)',
      crisp: '#F3F7F0',
      steel: '#778DA9',
      steelGlow: 'rgba(119, 141, 169, 0.55)',
      cyanAccent: '#a8c0dd',
      liveGreen: '#4caf82',
    };

    // Node locations (proportional) — sub labels updated dynamically
    this.nodes = {
      ingress: { x: 0.5, y: 0.12, label: 'Client Ingress (wrk2)', sub: '10 RPS / 2 Threads' },
      orchestrator: { x: 0.5, y: 0.48, label: 'Hybrid AI & Trust Gate', sub: 'Trust: 0.97 (PASS)' },
      primary: {
        x: 0.20, y: 0.74,
        label: 'aks-primary-eastus',
        sub: 'PJM Grid — loading…',
        region: 'eastus',
      },
      secondary: {
        x: 0.80, y: 0.74,
        label: 'aks-secondary-westus2',
        sub: 'BPA Hydro — loading…',
        region: 'westus2',
      },
    };

    this.initCanvasSize();
    window.addEventListener('resize', () => this.initCanvasSize());
    this.animate = this.animate.bind(this);
    requestAnimationFrame(this.animate);
  }

  // -------------------------------------------------------------------------
  // Live data injection (called by app.js on every /api/live-carbon response)
  // -------------------------------------------------------------------------
  setLiveCarbonValues(eastIntensity, westIntensity, updatedAt, source) {
    this.liveCarbon = {
      eastus: eastIntensity,
      westus2: westIntensity,
      updatedAt: updatedAt,
      source: source || 'live',
    };

    // Update node sub-labels with real gCO₂/kWh values
    if (eastIntensity !== null && eastIntensity !== undefined) {
      this.nodes.primary.sub = `PJM Grid — ${Math.round(eastIntensity)} gCO₂/kWh`;
    }
    if (westIntensity !== null && westIntensity !== undefined) {
      this.nodes.secondary.sub = `BPA Hydro — ${Math.round(westIntensity)} gCO₂/kWh`;
    }

    // Update the data-freshness timestamp display element
    this._updateTimestampDisplay(updatedAt);
  }

  _updateTimestampDisplay(updatedAt) {
    const el = document.getElementById('carbonDataTimestamp');
    if (!el || !updatedAt) return;
    try {
      const d = new Date(updatedAt);
      const diffMs = Date.now() - d.getTime();
      const diffMin = Math.floor(diffMs / 60000);
      if (diffMin < 1) {
        el.textContent = 'Updated just now';
      } else if (diffMin < 60) {
        el.textContent = `Updated ${diffMin}m ago`;
      } else {
        el.textContent = `Updated ${Math.floor(diffMin / 60)}h ago`;
      }
      el.title = `Data from Electricity Maps as of ${d.toLocaleTimeString()}`;
    } catch (_) {
      el.textContent = 'Live';
    }
  }

  // -------------------------------------------------------------------------
  initCanvasSize() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.width = rect.width;
    this.height = rect.height;
    this.canvas.width = this.width * dpr;
    this.canvas.height = this.height * dpr;
    this.ctx.scale(dpr, dpr);
  }

  setRoute(targetState) {
    if (this.state === targetState) return;
    this.state = targetState;
    this.isTransitioning = true;

    // Add ripple effect at destination
    const destNode = targetState === 'optimized' ? this.getNodeCoords('secondary') : this.getNodeCoords('primary');
    this.addRipple(destNode.x, destNode.y, targetState === 'optimized' ? this.colors.steel : this.colors.crimson);

    // Update UI HUD
    this.updateHudState();
  }

  addRipple(x, y, color) {
    this.ripples.push({
      x, y,
      radius: 10,
      maxRadius: 75,
      alpha: 0.9,
      color: color,
    });
  }

  getNodeCoords(nodeKey) {
    const n = this.nodes[nodeKey];
    return {
      x: n.x * this.width,
      y: n.y * this.height,
    };
  }

  spawnParticle() {
    const ingress = this.getNodeCoords('ingress');
    const startX = ingress.x + (Math.random() - 0.5) * 20;
    const startY = ingress.y;

    const targetRegion = this.state === 'optimized' ? 'secondary' : 'primary';

    this.particles.push({
      x: startX,
      y: startY,
      t: 0,
      speed: (0.005 + Math.random() * 0.003) * this.flowSpeed,
      size: 2.5 + Math.random() * 2.0,
      targetRegion: targetRegion,
      color: targetRegion === 'secondary' ? this.colors.cyanAccent : this.colors.crimson,
      trail: [],
    });
  }

  getBezierPoint(p0, p1, p2, p3, t) {
    const cx = 3 * (p1.x - p0.x);
    const bx = 3 * (p2.x - p1.x) - cx;
    const ax = p3.x - p0.x - cx - bx;

    const cy = 3 * (p1.y - p0.y);
    const by = 3 * (p2.y - p1.y) - cy;
    const ay = p3.y - p0.y - cy - by;

    const tSquared = t * t;
    const tCubed = tSquared * t;

    return {
      x: (ax * tCubed) + (bx * tSquared) + (cx * t) + p0.x,
      y: (ay * tCubed) + (by * tSquared) + (cy * t) + p0.y,
    };
  }

  calculateParticlePos(p) {
    const ingress = this.getNodeCoords('ingress');
    const orch = this.getNodeCoords('orchestrator');
    const dest = this.getNodeCoords(p.targetRegion);

    if (p.t < 0.4) {
      const localT = p.t / 0.4;
      const cp1 = { x: ingress.x, y: ingress.y + (orch.y - ingress.y) * 0.5 };
      const cp2 = { x: orch.x, y: orch.y - (orch.y - ingress.y) * 0.2 };
      return this.getBezierPoint(ingress, cp1, cp2, orch, localT);
    } else {
      const localT = (p.t - 0.4) / 0.6;
      const isWest = p.targetRegion === 'secondary';

      const cp1 = {
        x: isWest ? orch.x + (dest.x - orch.x) * 0.3 : orch.x - (orch.x - dest.x) * 0.3,
        y: orch.y + 20,
      };
      const cp2 = {
        x: dest.x,
        y: dest.y - (dest.y - orch.y) * 0.4,
      };
      return this.getBezierPoint(orch, cp1, cp2, dest, localT);
    }
  }

  update(dt) {
    if (this.isTransitioning) {
      const step = 0.035 * this.flowSpeed;
      if (this.state === 'optimized') {
        this.transitionProgress = Math.min(1.0, this.transitionProgress + step);
        if (this.transitionProgress >= 1.0) this.isTransitioning = false;
      } else {
        this.transitionProgress = Math.max(0.0, this.transitionProgress - step);
        if (this.transitionProgress <= 0.0) this.isTransitioning = false;
      }
    }

    this.spawnTimer += dt;
    if (this.spawnTimer >= this.spawnInterval && this.particles.length < this.maxParticles) {
      this.spawnParticle();
      this.spawnTimer = 0;
    }

    for (let i = this.particles.length - 1; i >= 0; i--) {
      const p = this.particles[i];
      p.t += p.speed;

      const pos = this.calculateParticlePos(p);
      p.x = pos.x;
      p.y = pos.y;

      p.trail.unshift({ x: p.x, y: p.y });
      if (p.trail.length > 8) p.trail.pop();

      if (p.t >= 1.0) {
        const dest = this.getNodeCoords(p.targetRegion);
        this.addRipple(dest.x, dest.y, p.targetRegion === 'secondary' ? this.colors.steel : this.colors.crimson);
        this.particles.splice(i, 1);
      }
    }

    for (let i = this.ripples.length - 1; i >= 0; i--) {
      const r = this.ripples[i];
      r.radius += 1.8 * this.flowSpeed;
      r.alpha -= 0.025;
      if (r.alpha <= 0 || r.radius >= r.maxRadius) {
        this.ripples.splice(i, 1);
      }
    }
  }

  drawBackgroundGrid() {
    const ctx = this.ctx;
    ctx.strokeStyle = 'rgba(98, 124, 133, 0.08)';
    ctx.lineWidth = 1;

    const gridSize = 44;
    for (let x = 0; x < this.width; x += gridSize) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, this.height);
      ctx.stroke();
    }
    for (let y = 0; y < this.height; y += gridSize) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(this.width, y);
      ctx.stroke();
    }
  }

  drawStaticConnections() {
    const ctx = this.ctx;
    const ingress = this.getNodeCoords('ingress');
    const orch = this.getNodeCoords('orchestrator');
    const primary = this.getNodeCoords('primary');
    const secondary = this.getNodeCoords('secondary');

    ctx.beginPath();
    ctx.moveTo(ingress.x, ingress.y);
    ctx.lineTo(orch.x, orch.y);
    ctx.strokeStyle = 'rgba(119, 141, 169, 0.25)';
    ctx.lineWidth = 2.5;
    ctx.setLineDash([4, 6]);
    ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(orch.x, orch.y);
    ctx.bezierCurveTo(
      orch.x - (orch.x - primary.x) * 0.3, orch.y + 20,
      primary.x, primary.y - (primary.y - orch.y) * 0.4,
      primary.x, primary.y,
    );
    const eastActive = this.state === 'baseline' || this.transitionProgress < 0.5;
    ctx.strokeStyle = eastActive ? 'rgba(214, 40, 40, 0.4)' : 'rgba(214, 40, 40, 0.12)';
    ctx.lineWidth = eastActive ? 3.5 : 1.5;
    ctx.setLineDash(eastActive ? [] : [6, 6]);
    ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(orch.x, orch.y);
    ctx.bezierCurveTo(
      orch.x + (secondary.x - orch.x) * 0.3, orch.y + 20,
      secondary.x, secondary.y - (secondary.y - orch.y) * 0.4,
      secondary.x, secondary.y,
    );
    const westActive = this.state === 'optimized' || this.transitionProgress > 0.5;
    ctx.strokeStyle = westActive ? 'rgba(119, 141, 169, 0.65)' : 'rgba(119, 141, 169, 0.15)';
    ctx.lineWidth = westActive ? 3.5 : 1.5;
    ctx.setLineDash(westActive ? [] : [6, 6]);
    ctx.stroke();

    ctx.setLineDash([]);
  }

  drawNodes() {
    const ctx = this.ctx;
    const ingress = this.getNodeCoords('ingress');
    const orch = this.getNodeCoords('orchestrator');
    const primary = this.getNodeCoords('primary');
    const secondary = this.getNodeCoords('secondary');

    this.drawNodeCircle(ingress.x, ingress.y, 14, this.colors.slate, 'Ingress');

    const orchGlow = this.state === 'optimized' ? this.colors.steelGlow : this.colors.crimsonGlow;
    ctx.save();
    ctx.shadowColor = orchGlow;
    ctx.shadowBlur = 18;
    this.drawNodeCircle(orch.x, orch.y, 22, this.colors.steel, 'AI Gate');
    ctx.restore();

    const isPrimaryActive = this.state === 'baseline';
    ctx.save();
    ctx.shadowColor = isPrimaryActive ? this.colors.crimsonGlow : 'transparent';
    ctx.shadowBlur = 25;
    this.drawNodeCircle(primary.x, primary.y, 28, isPrimaryActive ? this.colors.crimson : this.colors.slate, 'East US');
    ctx.restore();

    const isSecondaryActive = this.state === 'optimized';
    ctx.save();
    ctx.shadowColor = isSecondaryActive ? this.colors.steelGlow : 'transparent';
    ctx.shadowBlur = 25;
    this.drawNodeCircle(secondary.x, secondary.y, 28, isSecondaryActive ? this.colors.steel : this.colors.slate, 'West US 2');
    ctx.restore();

    // Draw node labels and live sub-labels below each node
    this._drawNodeLabels(ingress, orch, primary, secondary);
  }

  _drawNodeLabels(ingress, orch, primary, secondary) {
    const ctx = this.ctx;
    const labelOffset = 42;
    const subOffset = 58;

    const entries = [
      { coords: primary, node: this.nodes.primary },
      { coords: secondary, node: this.nodes.secondary },
      { coords: orch, node: this.nodes.orchestrator },
      { coords: ingress, node: this.nodes.ingress },
    ];

    ctx.textAlign = 'center';
    for (const { coords, node } of entries) {
      // Main label
      ctx.font = '600 11px "Inter", "Segoe UI", sans-serif';
      ctx.fillStyle = this.colors.crisp;
      ctx.fillText(node.label, coords.x, coords.y + labelOffset);

      // Sub-label (live carbon or status)
      ctx.font = '10px "Inter", "Segoe UI", sans-serif';
      // Colour live intensity readings based on value
      if (node === this.nodes.primary && this.liveCarbon.eastus !== null) {
        ctx.fillStyle = this.liveCarbon.eastus > 300 ? '#ff8a80' : '#a8c0dd';
      } else if (node === this.nodes.secondary && this.liveCarbon.westus2 !== null) {
        ctx.fillStyle = this.liveCarbon.westus2 < 200 ? this.colors.liveGreen : '#a8c0dd';
      } else {
        ctx.fillStyle = this.colors.slate;
      }
      ctx.fillText(node.sub, coords.x, coords.y + subOffset);
    }

    // Live data badge in top-right of canvas
    if (this.liveCarbon.source === 'live') {
      ctx.font = 'bold 9px "Inter", monospace';
      ctx.fillStyle = this.colors.liveGreen;
      ctx.textAlign = 'right';
      ctx.fillText('● LIVE', this.width - 10, 18);
    } else if (this.liveCarbon.source === 'loading') {
      ctx.font = '9px "Inter", monospace';
      ctx.fillStyle = this.colors.slate;
      ctx.textAlign = 'right';
      ctx.fillText('⟳ Fetching live data…', this.width - 10, 18);
    }
    ctx.textAlign = 'center'; // reset
  }

  drawNodeCircle(x, y, radius, color, label) {
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = this.colors.bg;
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = color;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(x, y, radius * 0.45, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  }

  drawParticles() {
    const ctx = this.ctx;
    for (const p of this.particles) {
      if (p.trail.length > 1) {
        ctx.beginPath();
        ctx.moveTo(p.trail[0].x, p.trail[0].y);
        for (let j = 1; j < p.trail.length; j++) {
          ctx.lineTo(p.trail[j].x, p.trail[j].y);
        }
        ctx.strokeStyle = p.color === this.colors.crimson
          ? 'rgba(214, 40, 40, 0.28)'
          : 'rgba(168, 192, 221, 0.35)';
        ctx.lineWidth = p.size * 0.8;
        ctx.stroke();
      }

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      ctx.fillStyle = p.color;
      ctx.shadowColor = p.color;
      ctx.shadowBlur = 8;
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  }

  drawRipples() {
    const ctx = this.ctx;
    for (const r of this.ripples) {
      ctx.beginPath();
      ctx.arc(r.x, r.y, r.radius, 0, Math.PI * 2);
      ctx.strokeStyle = r.color;
      ctx.globalAlpha = r.alpha;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.globalAlpha = 1.0;
    }
  }

  animate(currentTime) {
    if (!this.isPaused) {
      const dt = Math.min(currentTime - this.lastFrameTime, 100);
      this.lastFrameTime = currentTime;

      this.ctx.clearRect(0, 0, this.width, this.height);
      this.drawBackgroundGrid();
      this.drawStaticConnections();
      this.update(dt);
      this.drawRipples();
      this.drawParticles();
      this.drawNodes();
    }
    requestAnimationFrame(this.animate);
  }

  updateHudState() {
    const primaryHud = document.getElementById('primaryClusterHud');
    const secondaryHud = document.getElementById('secondaryClusterHud');
    const statusMsg = document.getElementById('statusBannerMsg');
    const metricCarbonDiff = document.getElementById('liveCarbonDiff');

    const eastVal = this.liveCarbon.eastus !== null
      ? `${Math.round(this.liveCarbon.eastus)} gCO₂eq/kWh`
      : '— gCO₂eq/kWh';
    const westVal = this.liveCarbon.westus2 !== null
      ? `${Math.round(this.liveCarbon.westus2)} gCO₂eq/kWh`
      : '— gCO₂eq/kWh';

    let reductionText = '';
    if (this.liveCarbon.eastus !== null && this.liveCarbon.westus2 !== null) {
      const pct = ((this.liveCarbon.eastus - this.liveCarbon.westus2) / this.liveCarbon.eastus * 100);
      reductionText = pct > 0 ? `${pct.toFixed(1)}% cleaner` : 'similar intensity';
    }

    if (this.state === 'optimized') {
      if (primaryHud) primaryHud.classList.remove('active');
      if (secondaryHud) secondaryHud.classList.add('active');
      if (statusMsg) {
        statusMsg.innerHTML = `<span class="live-indicator">● OPTIMIZED</span> Workload rerouted to <strong>aks-secondary-westus2</strong>. BPA Hydro: <strong>${westVal}</strong>${reductionText ? ` — ${reductionText}` : ''}. Latency p95: <strong>0.33 ms</strong>.`;
      }
      if (metricCarbonDiff) {
        const diffVal = (this.liveCarbon.eastus && this.liveCarbon.westus2)
          ? `-${Math.round(this.liveCarbon.eastus - this.liveCarbon.westus2)} gCO₂eq/kWh (${reductionText})`
          : '-263 gCO₂eq/kWh (BPA Hydro)';
        metricCarbonDiff.textContent = diffVal;
        metricCarbonDiff.style.color = '#a8c0dd';
      }
    } else {
      if (primaryHud) primaryHud.classList.add('active');
      if (secondaryHud) secondaryHud.classList.remove('active');
      if (statusMsg) {
        statusMsg.innerHTML = `<span class="live-indicator crimson">● HIGH CARBON ALERT</span> Running on <strong>aks-primary-eastus</strong> (PJM Grid: <strong>${eastVal}</strong>). Carbon-aware dispatch pending.`;
      }
      if (metricCarbonDiff) {
        metricCarbonDiff.textContent = `Baseline — PJM Grid (${eastVal})`;
        metricCarbonDiff.style.color = '#ff6b6b';
      }
    }
  }
}

// Attach globally
window.TrafficDirectorAnimation = TrafficDirectorAnimation;
