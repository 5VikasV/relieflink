import { useLayoutEffect, useRef, useState } from 'react';
import { Maximize2, Minus, Plus, MapPin } from 'lucide-react';
import type { Snapshot, Route } from './types';

const COLORS = ['#167b72', '#6366a8', '#d08b37', '#428cbb'];

type LabelSize = { width: number; height: number };
type MapLabel = { id: string; name: string; x: number; y: number; warehouse: boolean };
type LabelBox = { x: number; y: number; width: number; height: number };

function intersection(a: LabelBox, b: LabelBox) {
  return Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x))
    * Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));
}

function placeLabels(nodes: MapLabel[], sizes: Record<string, LabelSize>, viewport: LabelSize, zoom: number) {
  const scale = Math.max(.01, Math.min(viewport.width / 900, viewport.height / 475));
  const unit = 1 / scale;
  const offsetX = (viewport.width - 900 * scale) / 2;
  const offsetY = (viewport.height - 475 * scale) / 2;
  const bounds = { left: (8 - offsetX) * unit, right: (viewport.width - 8 - offsetX) * unit,
    top: (52 - offsetY) * unit, bottom: (viewport.height - (viewport.width < 480 ? 110 : 82) - offsetY) * unit };
  const visible = nodes.map(node => ({ ...node, x: 450 + (node.x - 450) * zoom, y: 237 + (node.y - 237) * zoom }))
    .filter(node => node.x >= -offsetX * unit && node.x <= (viewport.width - offsetX) * unit
      && node.y >= -offsetY * unit && node.y <= (viewport.height - offsetY) * unit);
  const markers = visible.map(node => {
    const radius = (node.warehouse ? 17 : 10) * zoom + 3 * unit;
    return { x: node.x - radius, y: node.y - radius, width: radius * 2, height: radius * 2 };
  });
  const occupied: LabelBox[] = [{ x: (viewport.width - 60 - offsetX) * unit, y: (16 - offsetY) * unit, width: 44 * unit, height: 132 * unit }];
  return { unit, labels: visible.map(node => {
    const size = sizes[node.id] || { width: node.name.length * 7, height: 16 };
    const width = (size.width + 16) * unit, height = (size.height + 10) * unit;
    const gap = (node.warehouse ? 17 : 10) * zoom + 6 * unit;
    const candidates = [[node.x, node.y + gap + height / 2], [node.x, node.y - gap - height / 2],
      [node.x + gap + width / 2, node.y], [node.x - gap - width / 2, node.y]];
    for (let step = 1; step <= 5; step++) {
      for (const direction of [-1, 1]) {
        for (const shift of [0, -width / 2, width / 2]) candidates.push([node.x + shift, node.y + direction * step * (height + 5 * unit)]);
      }
    }
    let chosen = { x: node.x, y: node.y, width, height }, best = Infinity, bestOverlap = Infinity;
    const consider = (x: number, y: number) => {
      const box = { x: Math.max(bounds.left, Math.min(x - width / 2, bounds.right - width)),
        y: Math.max(bounds.top, Math.min(y - height / 2, bounds.bottom - height)), width, height };
      const overlap = occupied.reduce((sum, other) => sum + intersection(box, other), 0)
        + markers.reduce((sum, marker) => sum + intersection(box, marker), 0);
      const distance = (box.x + width / 2 - node.x) ** 2 + (box.y + height / 2 - node.y) ** 2;
      const score = overlap * 1e8 + distance;
      if (score < best) { chosen = box; best = score; bestOverlap = overlap; }
    };
    for (const [x, y] of candidates) consider(x, y);
    // Dense phone maps may need free space farther from the marker.
    if (bestOverlap > 0) {
      for (let y = bounds.top + height / 2; y <= bounds.bottom - height / 2; y += 8 * unit) {
        for (let x = bounds.left + width / 2; x <= bounds.right - width / 2; x += 12 * unit) consider(x, y);
      }
    }
    occupied.push({ x: chosen.x - 2 * unit, y: chosen.y - 2 * unit, width: width + 4 * unit, height: height + 4 * unit });
    return { ...node, size, centerX: chosen.x + width / 2, centerY: chosen.y + height / 2 };
  }) };
}

export function NetworkMap({ data, routes = [], large = false }: { data: Snapshot; routes?: Route[]; large?: boolean }) {
  const [zoom, setZoom] = useState(1);
  const [selected, setSelected] = useState<string | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [viewport, setViewport] = useState<LabelSize>({ width: 900, height: 475 });
  const [labelSizes, setLabelSizes] = useState<Record<string, LabelSize>>({});
  const nodes = [...data.warehouses.map(n => ({ ...n, kind: 'warehouse', severity: '' })), ...data.locations.map(n => ({ ...n, kind: 'camp' }))];
  const labelKey = JSON.stringify(nodes.map(node => [node.id, node.name]));
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    let active = true;
    const measure = () => {
      if (!active) return;
      const { width, height } = svg.getBoundingClientRect();
      if (!width || !height) return;
      setViewport(previous => Math.abs(previous.width - width) < .1 && Math.abs(previous.height - height) < .1 ? previous : { width, height });
      const measured: Record<string, LabelSize> = {};
      svg.querySelectorAll<SVGTextElement>('[data-map-label]').forEach(text => {
        const box = text.getBBox();
        measured[text.dataset.mapLabel!] = { width: box.width, height: box.height };
      });
      setLabelSizes(previous => Object.entries(measured).every(([id, size]) => previous[id]
        && Math.abs(previous[id].width - size.width) < .1 && Math.abs(previous[id].height - size.height) < .1)
        ? previous : { ...previous, ...measured });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(svg);
    window.addEventListener('resize', measure);
    document.fonts.addEventListener('loadingdone', measure);
    void document.fonts.ready.then(measure);
    return () => { active = false; observer.disconnect(); window.removeEventListener('resize', measure); document.fonts.removeEventListener('loadingdone', measure); };
  }, [labelKey, zoom]);
  const all = [...nodes.map(n => [n.lat, n.lng]), ...routes.flatMap(r => r.path)];
  const minLat = all.length ? Math.min(...all.map(n => n[0])) : 12.9;
  const maxLat = all.length ? Math.max(...all.map(n => n[0])) : 13.1;
  const minLng = all.length ? Math.min(...all.map(n => n[1])) : 77.5;
  const maxLng = all.length ? Math.max(...all.map(n => n[1])) : 77.7;
  const project = (lat: number, lng: number) => [90 + (lng - minLng) / Math.max(maxLng - minLng, .015) * 720, 382 - (lat - minLat) / Math.max(maxLat - minLat, .015) * 285];
  const chosen = nodes.find(n => n.id === selected);
  const labels = placeLabels(nodes.map((node, i) => {
    const [x, y] = project(node.lat, node.lng);
    return { id: node.id, name: node.name.replace(/(Relief |Warehouse |Community |Shelter |Camp |Depot )/gi, '').slice(0, 26) || `Location ${i + 1}`, x, y, warehouse: node.kind === 'warehouse' };
  }), labelSizes, viewport, zoom);
  return <div className={`network-map ${large ? 'map-large' : ''}`}>
    <div className="map-caption"><span className="map-caption-dot" /> BENGALURU REGION <span className="map-caption-divider">/</span> SAMPLE NETWORK</div>
    <svg ref={svgRef} viewBox="0 0 900 475" role="img" aria-label="Modelled road network connecting relief locations and warehouses">
      <defs>
        <pattern id="map-grid" width="35" height="35" patternUnits="userSpaceOnUse"><path d="M 35 0 L 0 0 0 35" fill="none" stroke="#ccd9cf" strokeWidth=".6" opacity=".5" /></pattern>
        <pattern id="map-blocks" width="123" height="91" patternUnits="userSpaceOnUse"><rect x="8" y="9" width="40" height="27" rx="3" fill="#e8ece4" /><rect x="55" y="45" width="48" height="30" rx="3" fill="#e5eadd" /></pattern>
        <filter id="marker-shadow" x="-100%" y="-100%" width="300%" height="300%"><feDropShadow dx="0" dy="3" stdDeviation="4" floodColor="#203c30" floodOpacity=".16" /></filter>
      </defs>
      <rect width="900" height="475" fill="#f2f3e9" />
      <path d="M-40 80 Q170 130 180 260 T440 430 L350 540 L-30 520 Z" fill="#e3ebdc" />
      <path d="M640 -40 Q560 120 720 230 T850 520 L980 520 L1000 -60Z" fill="#e2e9d9" />
      <path d="M360 -40 Q310 130 410 200 T450 500" fill="none" stroke="#d5e7e7" strokeWidth="25" />
      <path d="M360 -40 Q310 130 410 200 T450 500" fill="none" stroke="#e1eff0" strokeWidth="15" />
      <rect width="900" height="475" fill="url(#map-blocks)" opacity=".64" /><rect width="900" height="475" fill="url(#map-grid)" />
      <g transform={`translate(${450 - 450 * zoom} ${237 - 237 * zoom}) scale(${zoom})`}>
        {data.roads.map(road => {
          const a = nodes.find(n => n.id === road.from_id), b = nodes.find(n => n.id === road.to_id);
          if (!a || !b) return null;
          const [x1, y1] = project(a.lat, a.lng), [x2, y2] = project(b.lat, b.lng);
          return <g key={road.id}><line x1={x1} y1={y1} x2={x2} y2={y2} stroke="white" strokeWidth="10" /><line x1={x1} y1={y1} x2={x2} y2={y2} stroke={road.blocked ? '#d95a52' : '#c8d3c5'} strokeWidth={road.blocked ? 3 : 3.5} strokeDasharray={road.blocked ? '7 5' : undefined} />{road.blocked && <g transform={`translate(${(x1 + x2) / 2} ${(y1 + y2) / 2})`}><circle r="11" fill="#fff" stroke="#d95a52" /><path d="M-5 0h10" stroke="#d95a52" strokeWidth="3" /></g>}</g>;
        })}
        {routes.map((route, i) => <polyline key={route.vehicle_id} points={route.path.map(([lat, lng]) => project(lat, lng).join(',')).join(' ')} stroke={COLORS[i % COLORS.length]} strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" fill="none" opacity=".85" />)}
        {nodes.map(node => {
          const [x, y] = project(node.lat, node.lng);
          const warehouse = node.kind === 'warehouse';
          const color = warehouse ? '#214c49' : node.severity === 'critical' ? '#db6d53' : node.severity === 'high' ? '#d9a245' : '#448a80';
          return <g className="map-marker" key={node.id} onClick={() => setSelected(selected === node.id ? null : node.id)} onKeyDown={e => { if (e.key === 'Enter') setSelected(node.id); }} tabIndex={0} role="button" aria-label={node.name}>
            <title>{node.name}</title>
            {warehouse ? <g filter="url(#marker-shadow)"><rect x={x - 17} y={y - 17} width="34" height="34" rx="9" fill={color} stroke="white" strokeWidth="3" /><path d={`M${x - 8} ${y - 3}l8 -6 8 6v11h-16z M${x - 3} ${y + 8}v-7h6v7`} stroke="white" strokeWidth="1.5" fill="none" /></g> : <><circle cx={x} cy={y} r={selected === node.id ? 23 : 19} fill={color} opacity=".13" /><circle cx={x} cy={y} r="10" fill={color} stroke="white" strokeWidth="3" filter="url(#marker-shadow)" /></>}
          </g>;
        })}
      </g>
      {/* Keep names at a readable screen size as the SVG and road graph scale. */}
      {labels.labels.map(label => <g key={label.id} aria-hidden="true">
        {Math.hypot(label.centerX - label.x, label.centerY - label.y) / labels.unit > 48 && <line
          x1={label.x} y1={label.y} x2={label.centerX} y2={label.centerY}
          stroke="#597053" strokeOpacity=".55" strokeWidth=".75" strokeDasharray="2 3" vectorEffect="non-scaling-stroke" pointerEvents="none" />}
        <g className="map-label" onClick={() => setSelected(selected === label.id ? null : label.id)}
          transform={`translate(${label.centerX} ${label.centerY}) scale(${labels.unit})`}>
          <rect x={-label.size.width / 2 - 8} y={-label.size.height / 2 - 5} width={label.size.width + 16} height={label.size.height + 10} rx="4" fill="white" fillOpacity=".95" />
          <text data-map-label={label.id} textAnchor="middle" dominantBaseline="central" className="map-node-label">{label.name}</text>
        </g>
      </g>)}
      {nodes.length === 0 && <g transform={`translate(450 237) scale(${labels.unit})`}><text textAnchor="middle" className="map-empty">Add locations and warehouses to build your network.</text></g>}
    </svg>
    <div className="map-controls"><button title="Zoom in" aria-label="Zoom in" onClick={() => setZoom(z => Math.min(1.8, z + .2))}><Plus size={16} /></button><button title="Zoom out" aria-label="Zoom out" onClick={() => setZoom(z => Math.max(.8, z - .2))}><Minus size={16} /></button><button title="Fit network" aria-label="Fit network" onClick={() => setZoom(1)}><Maximize2 size={15} /></button></div>
    {chosen && <div className="map-selected"><MapPin size={16} /><div><strong>{chosen.name}</strong><span>{chosen.lat.toFixed(4)}° N, {chosen.lng.toFixed(4)}° E</span></div><button onClick={() => setSelected(null)} aria-label="Close location">×</button></div>}
    <div className="map-legend"><span><i className="legend-warehouse" />Warehouse</span><span><i className="legend-camp" />Affected location</span><span><i className="legend-closure" />Road closure</span></div>
    <span className="map-disclaimer">Illustrative geography · modelled road links</span>
  </div>;
}
