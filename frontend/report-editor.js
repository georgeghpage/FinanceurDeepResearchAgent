import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import Link from '@tiptap/extension-link';
import { TableKit } from '@tiptap/extension-table';
import { Markdown } from '@tiptap/markdown';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { ReportCodeBlock } from './report-media.js';

const $ = id => document.getElementById(id);
const key = id => `deepresearch.document.v1.${id}`;
let editor = null;
let session = null;
let timer = null;
let generation = 0;
const CitationLink = Link.extend({
  renderHTML({ HTMLAttributes }) {
    const output = this.parent({ HTMLAttributes });
    output[1].title = HTMLAttributes.title || HTMLAttributes.href;
    return output;
  },
}).configure({ openOnClick: false, protocols: ['http', 'https'],
  HTMLAttributes: { rel: 'noopener noreferrer', target: '_blank', tabindex: '0' } });

function status(text) { $('document-status').textContent = text; }
function safeHTML(markdown) {
  return DOMPurify.sanitize(marked.parse(markdown, { async: false }), {
    USE_PROFILES: { html: true }, FORBID_TAGS: ['img', 'style', 'input', 'form', 'iframe'],
    FORBID_ATTR: ['style'],
  });
}
function localSave(s) {
  try { localStorage.setItem(key(s.id), JSON.stringify(s.data)); return true; }
  catch (_) { status('Local save failed. Export your edits before leaving.'); return false; }
}
async function sync(s) {
  if (s.saving || !s.dirty || s.conflict || !s.loaded) return;
  s.saving = true;
  const data = { ...s.data };
  const version = s.version;
  try {
    const response = await fetch(`/api/documents/${s.id}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: data.content, markdown: data.markdown, revision: s.data.revision }),
    });
    if (response.status === 409) { s.conflict = true; throw new Error('Newer edits exist in another tab. Export your copy before reloading.'); }
    if (!response.ok) throw new Error('Saved on this device · server save unavailable');
    const saved = await response.json();
    s.data.revision = saved.revision;
    s.dirty = s.version !== version;
    s.data.unsynced = s.dirty;
    const localOK = localSave(s);
    if (s === session && localOK) status(s.dirty ? 'Saving…' : 'Saved');
  } catch (error) {
    if (s === session) status(error.message);
  } finally {
    s.saving = false;
  }
  if (s.dirty && !s.conflict && s.data.revision !== data.revision) sync(s);
}
function changed() {
  if (!session || !editor) return;
  session.dirty = true;
  session.edited = true;
  session.version++;
  session.data = { ...session.data, content: editor.getJSON(), markdown: editor.getMarkdown(),
    updated: Date.now(), unsynced: true, edited: true };
  if (localSave(session)) status(session.conflict
    ? 'Newer edits exist elsewhere. Export this copy before reloading.'
    : 'Saved on this device · syncing…');
  clearTimeout(timer);
  const s = session;
  timer = setTimeout(() => sync(s), 500);
}
function reset() {
  clearTimeout(timer);
  if (session) sync(session);
  generation++;
  editor?.destroy();
  editor = null;
  session = null;
  $('document-tools').hidden = true;
  $('document-status').textContent = '';
}
function mount(markdown, editable) {
  $('answer').replaceChildren();
  editor = new Editor({
    element: $('answer'), injectCSS: false,
    extensions: [StarterKit.configure({ codeBlock: false, link: false }), CitationLink, TableKit, ReportCodeBlock, Markdown],
    content: safeHTML(markdown), editable,
    editorProps: { attributes: { 'aria-label': 'Research document', role: 'textbox', 'aria-multiline': 'true' } },
    onUpdate: changed,
  });
}
async function finish(id, markdown, incomplete = false) {
  if (!id) return;
  if (session?.id === id) return; // Replayed final results never replace user edits.
  reset();
  const ticket = generation;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(key(id))); } catch (_) { /* recover from server */ }
  mount(markdown, false);
  const s = { id, version: 0, edited: Boolean(saved?.edited), loaded: false, dirty: false, saving: false,
    data: saved || { content: editor.getJSON(), markdown, revision: 0, updated: Date.now(), edited: false } };
  session = s;
  if (saved?.content) {
    try { editor.commands.setContent(saved.content, { emitUpdate: false }); }
    catch (_) { status('Saved document could not be restored. Original report shown.'); }
  }
  $('document-tools').hidden = false;
  $('format-tools').hidden = false;
  $('edit-toggle').textContent = 'Reading view';
  $('edit-toggle').setAttribute('aria-pressed', 'true');
  $('document-completeness').textContent = incomplete ? 'Incomplete research · review limitations' : 'Research report';
  editor.setEditable(true, false);
  status('Restoring saved document…');
  try {
    const response = await fetch(`/api/documents/${id}`);
    if (!response.ok) throw new Error('Saved on this device · server storage unavailable');
    const { document: remote } = await response.json();
    if (ticket !== generation) return;
    if (remote && (s.data.unsynced || s.edited && s.dirty)) {
      if (s.data.revision !== remote.revision) { s.conflict = true; status('Newer edits exist elsewhere. Export this copy before reloading.'); }
    } else if (remote && !s.dirty) {
      editor.commands.setContent(remote.content, { emitUpdate: false });
      s.data = { ...remote, edited: true, updated: Date.now() };
      s.edited = true;
      localSave(s);
    }
    if (!s.conflict) status('Saved · click anywhere to edit');
  } catch (error) { if (ticket === generation) status(error.message); }
  finally {
    s.loaded = true;
    s.dirty = s.dirty || Boolean(s.data.unsynced);
    if (ticket === generation) sync(s);
  }
}
function preview(markdown) {
  if (session?.edited) return;
  if (!editor) mount(markdown, false);
  else editor.commands.setContent(safeHTML(markdown), { emitUpdate: false });
  $('document-completeness').textContent = 'Generating · incomplete, not yet validated';
}
function command(action) {
  if (!editor?.isEditable) return;
  const c = editor.chain().focus();
  const actions = { bold: () => c.toggleBold().run(), italic: () => c.toggleItalic().run(),
    h2: () => c.toggleHeading({ level: 2 }).run(), h3: () => c.toggleHeading({ level: 3 }).run(),
    bullet: () => c.toggleBulletList().run(), ordered: () => c.toggleOrderedList().run(),
    code: () => c.toggleCodeBlock().run(), table: () => c.insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run(),
    math: () => {
      const latex = window.prompt('Formula in LaTeX', 'E = mc^2');
      if (latex) c.insertContent({ type: 'codeBlock', attrs: { language: 'math' }, content: [{ type: 'text', text: latex }] }).run();
    },
    chart: () => {
      const data = window.prompt('Chart JSON: title and labeled numeric values', '{"title":"Comparison","values":[{"label":"Example","value":0}]}');
      if (data) c.insertContent({ type: 'codeBlock', attrs: { language: 'chart' }, content: [{ type: 'text', text: data }] }).run();
    },
    row: () => c.addRowAfter().run(), column: () => c.addColumnAfter().run(),
    deleteTable: () => c.deleteTable().run(), undo: () => c.undo().run(), redo: () => c.redo().run(),
    link: () => {
      const url = window.prompt('Link URL (https:// or http://)', editor.getAttributes('link').href || 'https://');
      if (url === '') c.extendMarkRange('link').unsetLink().run();
      else if (url && /^https?:\/\//i.test(url)) c.extendMarkRange('link').setLink({ href: url }).run();
    } };
  actions[action]?.();
}
for (const button of document.querySelectorAll('[data-editor-command]')) {
  button.addEventListener('mousedown', event => event.preventDefault());
  button.addEventListener('click', () => command(button.dataset.editorCommand));
}
$('edit-toggle').addEventListener('click', () => {
  if (!editor) return;
  editor.setEditable(!editor.isEditable, false);
  $('edit-toggle').textContent = editor.isEditable ? 'Reading view' : 'Edit document';
  $('edit-toggle').setAttribute('aria-pressed', String(editor.isEditable));
  $('format-tools').hidden = !editor.isEditable;
  if (session) sync(session);
});
$('copy-report').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(editor?.getMarkdown() || ''); status('Markdown copied'); }
  catch (_) { status('Copy unavailable. Use Save report instead.'); }
});
$('print-report').addEventListener('click', () => window.print());
window.addEventListener('online', () => session && sync(session));
window.addEventListener('pagehide', () => { if (session?.dirty) localSave(session); });
$('answer').addEventListener('click', event => {
  const link = event.target.closest('a');
  if (link && (!editor?.isEditable || event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    if (/^https?:\/\//i.test(link.href)) window.open(link.href, '_blank', 'noopener,noreferrer');
  } else if (link && editor?.isEditable) {
    // Native anchor focus can reset ProseMirror's caret to the old selection.
    // Keep a source click in the editor so formatting and keyboard activation
    // use this link's mark, consistently across browser engines.
    event.preventDefault();
    const position = editor.view.posAtDOM(link, 0);
    editor.chain().focus().setTextSelection(position + Math.min(1, link.textContent.length)).run();
  }
});
$('answer').addEventListener('keydown', event => {
  const link = event.target.closest('a');
  const selected = window.getSelection()?.anchorNode?.parentElement?.closest('a');
  const selectedHref = selected && $('answer').contains(selected) ? selected.href : null;
  const href = link?.href || (editor?.isEditable && (event.metaKey || event.ctrlKey)
    ? selectedHref || editor.getAttributes('link').href : null);
  if (href && event.key === 'Enter' && /^https?:\/\//i.test(href)) {
    event.preventDefault();
    event.stopPropagation();
    window.open(href, '_blank', 'noopener,noreferrer');
  }
}, true);
window.ReportEditor = { reset, finish, preview, markdown: () => editor?.getMarkdown(),
  hasEdits: () => Boolean(session?.edited) };
