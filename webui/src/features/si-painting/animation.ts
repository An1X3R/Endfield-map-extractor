import { clamp, damp, phase } from './artwork'

function element(root: HTMLElement, id: string): HTMLElement | SVGElement {
  const node = root.querySelector(`#si-${id}`)
  if (!(node instanceof HTMLElement || node instanceof SVGElement)) throw new Error(`Painting animation element missing: ${id}`)
  return node
}
function settle(node: HTMLElement | SVGElement, value: number, distance: number): void {
  node.style.opacity = String(clamp(value))
  node.style.transform = `translateY(${(1 - damp(value)) * distance}px)`
}
export function playPaintingCompletion(root: HTMLElement, terminal: string, reducedMotion: boolean): () => void {
  let completionFrame = 0
  const start = performance.now();
  const branches = Array.from(root.querySelectorAll('.si-tree-branch'));
  const crowns = Array.from(root.querySelectorAll('[data-crown]'));
  const textNodes = Array.from(root.querySelectorAll('.si-result-kicker, #si-result-title, .si-result-sub, .si-report-record, .si-report-bottom, .si-art-caption, .si-result-stamp'));
  const tick = (now: number): void => {
    const time = reducedMotion ? 1800 : Math.min(1800, now - start);
    const open = damp(phase(time, 0, 550));
    element(root, 'scroll-shell').style.setProperty('--reveal', `${48 * (1 - open)}%`);
    const paint = damp(phase(time, 110, 920));
    element(root, 'paint-aperture-rect').setAttribute('x', String(590 * (1 - paint)));
    element(root, 'paint-aperture-rect').setAttribute('width', String(1180 * paint));
    const mountain = damp(phase(time, 130, 1030));
    settle(element(root, 'mountain-scene'), mountain, 14);
    settle(element(root, 'rock-scene'), phase(time, 140, 360), 6);
    root.querySelectorAll('.si-rock-line').forEach((node) => { node.setAttribute('stroke-dasharray', '1'); node.setAttribute('stroke-dashoffset', String(1 - damp(phase(time, 120, 430)))); });
    element(root, 'trunk-mask-path').setAttribute('stroke-dasharray', '1');
    element(root, 'trunk-mask-path').setAttribute('stroke-dashoffset', String(1 - damp(phase(time, 310, 580))));
    branches.forEach((node) => {
      const branchStart = Number(node.getAttribute('data-start'));
      node.setAttribute('stroke-dasharray', '1');
      node.setAttribute('stroke-dashoffset', String(1 - damp(phase(time, branchStart, 380))));
      node.setAttribute('opacity', String(clamp(phase(time, branchStart, 70))));
    });
    crowns.forEach((node, index) => {
      const p = phase(time, 700 + index * 27, 300);
      node.setAttribute('opacity', String(damp(p)));
    });
    element(root, 'canopy-grain').style.opacity = String(.2 * phase(time, 750, 600));
    settle(element(root, 'falling-leaves'), phase(time, 950, 600), -19);
    element(root, 'brush-traces').style.opacity = String(.17 * phase(time, 780, 570));
    textNodes.forEach((node, index) => {
      if (!(node instanceof HTMLElement)) throw new Error('Report item must be HTML.');
      settle(node, phase(time, 260 + index * 54, 650), index === 1 ? 20 : 12);
    });
    element(root, 'report-progress').style.transform = `scaleX(${damp(phase(time, 410, 710))})`;
    element(root, 'terminal-record').textContent = terminal.slice(0, Math.floor(terminal.length * phase(time, 590, 650)));
    if (time < 1800) completionFrame = requestAnimationFrame(tick); else completionFrame = 0;
  };
  tick(start);
  return () => cancelAnimationFrame(completionFrame);
}
