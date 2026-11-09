// The bounded conversion runs only in the opaque board document (§56.9).
import { parseMermaidToExcalidraw } from '@excalidraw/mermaid-to-excalidraw';
import { convertToExcalidrawElements } from '@excalidraw/excalidraw';
import rules from '../../src/switchboard/web/static/board-rules.js';
export async function convert(source) {
  if (typeof source !== 'string' || source.length > 8000 || /%%\{|\bclick\b/i.test(source) || !/^(flowchart|graph)\s+(TD|TB|BT|RL|LR)\b/.test(source)) throw new Error('invalid flow');
  if (/^\s*(style|classDef|linkStyle)\b|@\s*\{|!\[/im.test(source)) throw new Error('plain flows only');
  source = source.replace(/&(?=[#a-z0-9])/gi, '＆').replace(/#(?=(?:[a-z]+|[0-9]+);)/gi, '＃').replace(/<(?=[a-z/!])/gi, '＜');
  const result = await parseMermaidToExcalidraw(source, { securityLevel: 'strict', startOnLoad: false, suppressErrorRendering: true, maxEdges: 40, maxTextSize: 8000, flowchart: { htmlLabels: false }, themeVariables: { fontSize: '20px', fontFamily: 'sans-serif' } });
  const elements = rules.batch(convertToExcalidrawElements(result.elements, { regenerateIds: true }));
  if (!elements || !elements.length || elements.length > 40 || elements.some(e => !['rectangle','ellipse','diamond','arrow','text','frame'].includes(e.type))) throw new Error('diagram exceeds limits');
  return elements;
}
