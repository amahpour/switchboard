// Attach a two-point bound arrow to the actual targets, without converting targets as skeletons.
// Derived view geometry only: no version bump, mutation of a target, or echo back to the broker.
export function attachArrows(elements) {
  const byId = new Map(elements.map(e => [e.id, e]));
  function edge(shape, other, gap) {
    const center = [shape.x + shape.width / 2, shape.y + shape.height / 2];
    const dx = other.x + other.width / 2 - center[0], dy = other.y + other.height / 2 - center[1];
    const angle = shape.angle || 0, co = Math.cos(angle), si = Math.sin(angle);
    const lx = dx * co + dy * si, ly = -dx * si + dy * co;
    const w = Math.max(1, shape.width / 2), h = Math.max(1, shape.height / 2);
    let scale;
    if (shape.type === 'ellipse') scale = 1 / Math.sqrt((lx / w) ** 2 + (ly / h) ** 2);
    else if (shape.type === 'diamond') scale = 1 / (Math.abs(lx) / w + Math.abs(ly) / h);
    else scale = Math.min(w / Math.max(1e-9, Math.abs(lx)), h / Math.max(1e-9, Math.abs(ly)));
    const length = Math.hypot(dx, dy);
    if (!length) return center;
    return [center[0] + dx * scale + dx / length * gap, center[1] + dy * scale + dy / length * gap];
  }
  return elements.map(e => {
    if (e.type !== 'arrow' || e.elbowed || e.points.length !== 2) return e;
    const a = byId.get(e.startBinding?.elementId), b = byId.get(e.endBinding?.elementId);
    if (!a || !b || a.isDeleted || b.isDeleted) return e;
    const start = edge(a, b, e.startBinding.gap || 0), end = edge(b, a, e.endBinding.gap || 0);
    return { ...e, x: start[0], y: start[1], width: Math.abs(end[0] - start[0]), height: Math.abs(end[1] - start[1]), points: [[0, 0], [end[0] - start[0], end[1] - start[1]]] };
  });
}

// Bound labels from the broker carry a rough one-line origin. Recenter their measured box,
// without resizing a target or changing its stored version. Left/top person layouts stay as drawn.
export function centerLabels(elements) {
  const byId = new Map(elements.map(e => [e.id, e]));
  return elements.map(e => {
    const c = byId.get(e.containerId);
    if (e.type !== 'text' || !c || !['rectangle','ellipse','diamond'].includes(c.type) || e.textAlign !== 'center' || e.verticalAlign !== 'middle') return e;
    return { ...e, x: c.x + (c.width - e.width) / 2, y: c.y + (c.height - e.height) / 2, angle: c.angle || 0 };
  });
}
