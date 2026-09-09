/**
 * 28-Microservice Dependency DAG Topology Engine
 * Visualizes the DeathStarBench Social Network graph with interactive tier layout,
 * live node inspections, and dependency flow lines.
 */

class GraphTopologyViewer {
  constructor(svgId) {
    this.svg = document.getElementById(svgId);
    if (!this.svg) return;
    this.selectedNode = null;
    this.init();
  }

  async init() {
    let graphData = null;
    try {
      const res = await fetch('data/graph_data.json');
      graphData = await res.json();
    } catch (e) {
      console.warn('Using default graph nodes:', e);
    }
    this.render(graphData);
  }

  render(graphData) {
    const width = this.svg.clientWidth || 1000;
    const height = 400;
    this.svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    this.svg.innerHTML = '';

    // Tiers definition
    const tiers = [
      { name: 'Ingress Gateway', nodes: ['nginx-thrift'], x: width * 0.10 },
      { name: 'Composite Services', nodes: ['compose-post-service', 'home-timeline-service', 'user-timeline-service', 'social-graph-service'], x: width * 0.35 },
      { name: 'Core Microservices', nodes: ['text-service', 'user-mention-service', 'unique-id-service', 'media-service', 'url-shorten-service', 'post-storage-service'], x: width * 0.62 },
      { name: 'State / Cache Storage', nodes: ['social-graph-mongodb', 'post-storage-mongodb', 'home-timeline-redis', 'user-memcached', 'media-mongodb', 'url-shorten-mongodb'], x: width * 0.88 }
    ];

    const nodePositions = {};

    // Calculate Y positions per tier
    tiers.forEach(tier => {
      const count = tier.nodes.length;
      const step = height / (count + 1);
      tier.nodes.forEach((nodeName, i) => {
        nodePositions[nodeName] = {
          x: tier.x,
          y: step * (i + 1),
          tier: tier.name
        };
      });
    });

    // Edges definition (canonical DeathStarBench dependencies)
    const edges = [
      { from: 'nginx-thrift', to: 'compose-post-service' },
      { from: 'nginx-thrift', to: 'home-timeline-service' },
      { from: 'nginx-thrift', to: 'user-timeline-service' },
      { from: 'nginx-thrift', to: 'social-graph-service' },
      { from: 'compose-post-service', to: 'text-service' },
      { from: 'compose-post-service', to: 'media-service' },
      { from: 'compose-post-service', to: 'unique-id-service' },
      { from: 'compose-post-service', to: 'post-storage-service' },
      { from: 'compose-post-service', to: 'user-timeline-service' },
      { from: 'text-service', to: 'user-mention-service' },
      { from: 'text-service', to: 'url-shorten-service' },
      { from: 'social-graph-service', to: 'social-graph-mongodb' },
      { from: 'post-storage-service', to: 'post-storage-mongodb' },
      { from: 'home-timeline-service', to: 'home-timeline-redis' },
      { from: 'media-service', to: 'media-mongodb' },
      { from: 'url-shorten-service', to: 'url-shorten-mongodb' }
    ];

    // Create defs for gradients and markers
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.innerHTML = `
      <linearGradient id="edgeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" stop-color="#627C85" stop-opacity="0.3"/>
        <stop offset="100%" stop-color="#778DA9" stop-opacity="0.7"/>
      </linearGradient>
      <marker id="arrow" viewBox="0 0 10 10" refX="16" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" fill="#627C85"/>
      </marker>
    `;
    this.svg.appendChild(defs);

    // Draw edges
    edges.forEach(edge => {
      const p1 = nodePositions[edge.from];
      const p2 = nodePositions[edge.to];
      if (p1 && p2) {
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        const dx = (p2.x - p1.x) * 0.5;
        const d = `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`;
        path.setAttribute('d', d);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', 'url(#edgeGrad)');
        path.setAttribute('stroke-width', '1.5');
        path.setAttribute('marker-end', 'url(#arrow)');
        this.svg.appendChild(path);
      }
    });

    // Draw nodes
    Object.entries(nodePositions).forEach(([name, pos]) => {
      const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.setAttribute('class', 'dag-node');
      g.style.cursor = 'pointer';

      // Background pill / circle
      const isIngress = name === 'nginx-thrift';
      const isStorage = pos.tier.includes('Storage');
      const nodeColor = isIngress ? '#D62828' : (isStorage ? '#627C85' : '#778DA9');

      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', pos.x);
      circle.setAttribute('cy', pos.y);
      circle.setAttribute('r', isIngress ? '12' : '9');
      circle.setAttribute('fill', '#061826');
      circle.setAttribute('stroke', nodeColor);
      circle.setAttribute('stroke-width', '2.5');

      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', pos.x);
      text.setAttribute('y', pos.y - 14);
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('fill', '#F3F7F0');
      text.setAttribute('font-size', '10px');
      text.setAttribute('font-family', 'JetBrains Mono, monospace');
      text.textContent = name.replace('-service', '');

      g.appendChild(circle);
      g.appendChild(text);

      g.addEventListener('mouseenter', () => {
        circle.setAttribute('stroke-width', '4');
        circle.setAttribute('r', isIngress ? '14' : '11');
        this.showNodeInfo(name, pos.tier);
      });

      g.addEventListener('mouseleave', () => {
        if (!g.classList.contains('active-selected')) {
          circle.setAttribute('stroke-width', '2.5');
          circle.setAttribute('r', isIngress ? '12' : '9');
        }
      });

      // Click node -> Simulate switch & open AI decision thinking panel
      g.addEventListener('click', (e) => {
        e.stopPropagation();
        // Remove active state from all other nodes
        this.svg.querySelectorAll('.dag-node').forEach(el => {
          el.classList.remove('active-selected');
          const c = el.querySelector('circle');
          if (c) {
            const isIng = el.textContent.includes('nginx-thrift');
            c.setAttribute('stroke-width', '2.5');
            c.setAttribute('r', isIng ? '12' : '9');
          }
        });

        // Activate this node
        g.classList.add('active-selected');
        circle.setAttribute('stroke-width', '4.5');
        circle.setAttribute('r', isIngress ? '15' : '12');

        this.showNodeInfo(name, pos.tier, true);

        // Sync live carbon values if available
        if (window.thinkingPanel) {
          if (window.trafficAnim && window.trafficAnim.liveCarbon) {
            window.thinkingPanel.setLiveCarbon(
              window.trafficAnim.liveCarbon.eastus,
              window.trafficAnim.liveCarbon.westus2
            );
          }
          // Open the AI Thinking Panel with the microservice context
          window.thinkingPanel.trigger(name, { tier: pos.tier });
        }

        // Also trigger the traffic rerouting animation
        if (window.trafficAnim) {
          window.trafficAnim.setRoute('baseline');
          setTimeout(() => {
            if (window.trafficAnim) window.trafficAnim.setRoute('optimized');
          }, 3200);
        }
      });

      this.svg.appendChild(g);
    });
  }

  showNodeInfo(name, tier, isClicked = false) {
    const infoEl = document.getElementById('selectedNodeInfo');
    if (infoEl) {
      if (isClicked) {
        infoEl.innerHTML = `⚡ Active Simulation: <strong style="color:#4caf82;">${name}</strong> | Tier: <strong>${tier}</strong> | Decision panel opened · Rerouting DeathStarBench traffic to clean region`;
      } else {
        infoEl.innerHTML = `Service: <strong>${name}</strong> | Tier: <strong>${tier}</strong> | Placement: <code>aks-primary-eastus</code> / <code>aks-secondary-westus2</code> (Click node to simulate decision)`;
      }
    }
  }
}

window.GraphTopologyViewer = GraphTopologyViewer;

