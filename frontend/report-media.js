import CodeBlock from '@tiptap/extension-code-block';
import katex from 'katex';

function svgElement(tag, attributes, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (text != null) node.textContent = text;
  return node;
}
function chart(source, host) {
  const data = JSON.parse(source);
  if (!data || typeof data.title !== 'string' || !Array.isArray(data.values) || !data.values.length || data.values.length > 30) throw new Error('Use a title and 1–30 labeled numeric values.');
  if (data.values.some(item => typeof item.label !== 'string' || typeof item.value !== 'number' || !Number.isFinite(item.value) || item.value < 0)) throw new Error('Chart values must be finite, nonnegative numbers.');
  const height = data.values.length * 34 + 55;
  const svg = svgElement('svg', { viewBox: `0 0 640 ${height}`, role: 'img', 'aria-label': data.title });
  svg.append(svgElement('title', {}, data.title));
  svg.append(svgElement('text', { x: 0, y: 22, class: 'chart-title' }, data.title.slice(0, 100)));
  const maximum = Math.max(...data.values.map(item => item.value), 1);
  data.values.forEach((item, index) => {
    const y = 45 + index * 34;
    svg.append(svgElement('text', { x: 0, y: y + 16 }, item.label.slice(0, 24)));
    svg.append(svgElement('rect', { x: 180, y, width: 350 * item.value / maximum, height: 22, rx: 3 }));
    svg.append(svgElement('text', { x: 545, y: y + 16 }, String(item.value)));
  });
  host.append(svg);
}

// Code remains a standard ProseMirror code block. Media is a deterministic
// preview of its editable source and round-trips through fenced Markdown.
export const ReportCodeBlock = CodeBlock.extend({
  addNodeView() {
    return ({ node }) => {
      const dom = document.createElement('div');
      const preview = document.createElement('div');
      preview.contentEditable = 'false';
      preview.className = 'media-preview';
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      pre.append(code);
      dom.append(preview, pre);
      const render = current => {
        const language = current.attrs.language;
        dom.className = ['math', 'chart'].includes(language) ? 'report-media' : 'report-code';
        preview.replaceChildren();
        preview.hidden = !['math', 'chart'].includes(language);
        try {
          if (language === 'math') katex.render(current.textContent, preview, {
            output: 'mathml', displayMode: true, throwOnError: true, trust: false,
            maxExpand: 1000, maxSize: 20,
          });
          if (language === 'chart') chart(current.textContent, preview);
        } catch (_) { preview.textContent = 'Preview unavailable. Check the formula or chart data below.'; }
      };
      render(node);
      return { dom, contentDOM: code,
        update(updated) { if (updated.type.name !== 'codeBlock') return false; render(updated); return true; },
        ignoreMutation(mutation) { return preview.contains(mutation.target); } };
    };
  },
});
