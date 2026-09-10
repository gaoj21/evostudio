// The rendered ports and React Flow's initial geometry share one definition.
// Derived nodes can be replaced while their first DOM measurement is pending.
export const MEMORY_HANDLES = ['top', 'right', 'bottom', 'left'].flatMap((position, index) => [
  { type: 'target', id: index === 0 ? 's-in' : `s-in-${position}`, position, fraction: .38 },
  { type: 'source', id: index === 0 ? 's-out' : `s-out-${position}`, position, fraction: .62 },
]);
export const CHAT_HANDLES = [
  { type: 'target', id: 'in', position: 'left', fraction: .5 },
  { type: 'target', id: 't-in', position: 'top', fraction: .5 },
  { type: 'source', id: 'b-out', position: 'bottom', fraction: .38 },
  { type: 'target', id: 'b-in', position: 'bottom', fraction: .62 },
  { type: 'source', id: 'out', position: 'right', fraction: .5 },
];
export const handleStyle = port => ({ [port.position === 'top' || port.position === 'bottom' ? 'left' : 'top']: `${port.fraction * 100}%` });
export function resourceGeometry(node, measured) {
  const width = measured?.width || node.width || node.initialWidth;
  const height = measured?.height || node.height || node.initialHeight;
  const ports = node.type === 'memory' ? MEMORY_HANDLES : CHAT_HANDLES;
  return { ...node, measured, handles: ports.map(port => ({
    id: port.id, type: port.type, position: port.position, width: 6, height: 6,
    x: (port.position === 'left' ? 0 : port.position === 'right' ? width : width * port.fraction) - 3,
    y: (port.position === 'top' ? 0 : port.position === 'bottom' ? height : height * port.fraction) - 3,
  })) };
}
