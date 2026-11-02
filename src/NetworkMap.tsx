import { useState } from 'react';
import { Maximize2, Minus, Plus, MapPin } from 'lucide-react';
import type { Snapshot, Route } from './types';

const COLORS = ['#167b72', '#6366a8', '#d08b37', '#428cbb'];

export function NetworkMap({ data, routes = [], large = false }: { data: Snapshot; routes?: Route[]; large?: boolean }) {
  const [zoom, setZoom] = useState(1);
  const [selected, setSelected] = useState<string | null>(null);
  const nodes = [...data.warehouses.map(n => ({ ...n, kind: 'warehouse', severity: '' })), ...data.locations.map(n => ({ ...n, kind: 'camp' }))];
  const all = [...nodes.map(n => [n.lat, n.lng]), ...routes.flatMap(r => r.path)];
  const minLat = all.length ? Math.min(...all.map(n => n[0])) : 12.9;
  const maxLat = all.length ? Math.max(...all.map(n => n[0])) : 13.1;
  const minLng = all.length ? Math.min(...all.map(n => n[1])) : 77.5;
  const maxLng = all.length ? Math.max(...all.map(n => n[1])) : 77.7;
  const project = (lat: number, lng: number) => [90 + (lng - minLng) / Math.max(maxLng - minLng, .015) * 720, 382 - (lat - minLat) / Math.max(maxLat - minLat, .015) * 285];
  const chosen = nodes.find(n => n.id === selected);
  return <div className={`network-map ${large ? 'map-large' : ''}`}>
    <div className="map-caption"><span className="map-caption-dot" /> BENGALURU REGION <span className="map-caption-divider">/</span> SAMPLE NETWORK</div>
    <svg viewBox="0 0 900 475" role="img" aria-label="Modelled road network connecting relief locations and warehouses">
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
        {nodes.map((node, i) => {
          const [x, y] = project(node.lat, node.lng);
          const warehouse = node.kind === 'warehouse';
          const color = warehouse ? '#214c49' : node.severity === 'critical' ? '#db6d53' : node.severity === 'high' ? '#d9a245' : '#448a80';
          const shortName = node.name.replace(/(Relief |Warehouse |Community |Shelter |Camp |Depot )/gi, '').slice(0, 26);
          return <g className="map-marker" key={node.id} onClick={() => setSelected(selected === node.id ? null : node.id)} onKeyDown={e => { if (e.key === 'Enter') setSelected(node.id); }} tabIndex={0} role="button" aria-label={node.name}>
            {warehouse ? <g filter="url(#marker-shadow)"><rect x={x - 17} y={y - 17} width="34" height="34" rx="9" fill={color} stroke="white" strokeWidth="3" /><path d={`M${x - 8} ${y - 3}l8 -6 8 6v11h-16z M${x - 3} ${y + 8}v-7h6v7`} stroke="white" strokeWidth="1.5" fill="none" /></g> : <><circle cx={x} cy={y} r={selected === node.id ? 23 : 19} fill={color} opacity=".13" /><circle cx={x} cy={y} r="10" fill={color} stroke="white" strokeWidth="3" filter="url(#marker-shadow)" /></>}
            <g transform={`translate(${x} ${y + (warehouse ? 30 : 27)})`}><rect x={-shortName.length * 3.15 - 8} y="-11" rx="4" width={shortName.length * 6.3 + 16} height="22" fill="white" fillOpacity=".95" /><text textAnchor="middle" y="4" className="map-node-label">{shortName || `Location ${i + 1}`}</text></g>
          </g>;
        })}
        {nodes.length === 0 && <text x="450" y="245" textAnchor="middle" className="map-empty">Add locations and warehouses to build your network.</text>}
      </g>
    </svg>
    <div className="map-controls"><button title="Zoom in" aria-label="Zoom in" onClick={() => setZoom(z => Math.min(1.8, z + .2))}><Plus size={16} /></button><button title="Zoom out" aria-label="Zoom out" onClick={() => setZoom(z => Math.max(.8, z - .2))}><Minus size={16} /></button><button title="Fit network" aria-label="Fit network" onClick={() => setZoom(1)}><Maximize2 size={15} /></button></div>
    {chosen && <div className="map-selected"><MapPin size={16} /><div><strong>{chosen.name}</strong><span>{chosen.lat.toFixed(4)}° N, {chosen.lng.toFixed(4)}° E</span></div><button onClick={() => setSelected(null)} aria-label="Close location">×</button></div>}
    <div className="map-legend"><span><i className="legend-warehouse" />Warehouse</span><span><i className="legend-camp" />Affected location</span><span><i className="legend-closure" />Road closure</span></div>
    <span className="map-disclaimer">Illustrative geography · modelled road links</span>
  </div>;
}
