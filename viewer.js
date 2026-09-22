'use strict';
const D = window.DATA, files = D.files, comments = D.comments || {}, viewed = new Set(D.viewed || []);
let view = (D.prefs && D.prefs.view) || 'unified', finished = false, focusRow = null, drag = null, activeFile = 0;
const $ = (s, r) => (r || document).querySelector(s), $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const esc = s => s.replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const el = (tag, attrs, html) => { const e = document.createElement(tag); if (attrs) for (const k in attrs) { if (k === 'class') e.className = attrs[k]; else if (k.startsWith('on')) e.addEventListener(k.slice(2), attrs[k]); else e.setAttribute(k, attrs[k]); } if (html != null) e.innerHTML = html; return e; };
const uid = () => { let id; do { id = Math.random().toString(36).slice(2, 7); } while (comments[id]); return id; };

async function api(path, body) {
  try {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch (e) {
    if (!finished) banner('clankback is not running, so comments are not being saved. Re-run /clankback, then reload this page.');
  }
}
let diffRev = D.diff_rev;
function banner(msg) { const b = $('#banner'); b.textContent = msg; b.hidden = !msg; }
async function reloadDiff() {  // the working tree changed: re-render in place, keep scroll and drafts
  let r; try { r = await (await fetch('/data')).json(); } catch (e) { return; }
  if (!r || !r.files) return;
  diffRev = r.diff_rev;
  const main = $('#main'), top = main.scrollTop, drafts = {};
  $$('.thread').forEach(t => { const ta = t.querySelector('.tfoot textarea'); if (ta && ta.value) drafts[t.dataset.id] = ta.value; });
  const wasRendered = new Set(rendered);
  files.splice(0, files.length, ...r.files); rendered.clear();
  buildSidebar(); buildFiles();
  wasRendered.forEach(i => { if (i < files.length) renderFile(i); });
  for (const id in drafts) { const ta = $('.thread[data-id="' + id + '"] .tfoot textarea'); if (ta) ta.value = drafts[id]; }
  main.scrollTop = top; refreshCount(); if (!$('#summary').hidden) buildSummary();
  toast('The files changed on disk. Diff reloaded.');
}

// ---------------------------------------------------------------- syntax highlighting
const KW = {
  js: 'async await break case catch class const continue debugger default delete do else enum export extends false finally for from function if implements import in instanceof interface let new null of package private protected public return static super switch this throw true try type typeof undefined var void while with yield as declare namespace readonly abstract satisfies keyof infer never unknown any string number boolean',
  py: 'False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield self cls print match case',
  rs: 'as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while u8 u16 u32 u64 u128 i8 i16 i32 i64 i128 usize isize f32 f64 bool str String Vec Option Some None Result Ok Err Box',
  go: 'break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false string int int64 uint byte error bool float64 make new len cap append',
  c: 'auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while class namespace template typename public private protected virtual override new delete this nullptr true false bool using constexpr',
  java: 'abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while true false null var val fun when object data override sealed open let guard func struct protocol extension',
  sh: 'if then else elif fi for while until do done case esac in function return exit export local readonly set unset shift source echo true false',
  rb: 'alias and begin break case class def defined do else elsif end ensure false for if in module next nil not or redo rescue retry return self super then true undef unless until when while yield require attr_accessor puts',
  sql: 'select from where and or not in is null as join left right inner outer on group by order having limit offset insert into values update set delete create table index view drop alter add primary key foreign references unique default int integer text varchar boolean date timestamp begin commit rollback with union all distinct exists between like case when then else end',
  css: 'important inherit initial unset none auto flex grid block inline absolute relative fixed sticky',
};
const LANGS = {
  js: {kw: KW.js, lc: '//', bc: true}, jsx: 'js', ts: 'js', tsx: 'js', mjs: 'js', cjs: 'js', json: {kw: 'true false null', lc: null},
  py: {kw: KW.py, lc: '#'}, pyi: 'py', rs: {kw: KW.rs, lc: '//', bc: true}, go: {kw: KW.go, lc: '//', bc: true},
  c: {kw: KW.c, lc: '//', bc: true}, h: 'c', cpp: 'c', cc: 'c', hpp: 'c', cs: 'c', m: 'c',
  java: {kw: KW.java, lc: '//', bc: true}, kt: 'java', kts: 'java', swift: 'java', dart: 'java', scala: 'java',
  sh: {kw: KW.sh, lc: '#'}, bash: 'sh', zsh: 'sh', fish: 'sh', rb: {kw: KW.rb, lc: '#'}, sql: {kw: KW.sql, lc: '--', bc: true},
  css: {kw: KW.css, lc: null, bc: true}, scss: 'css', less: 'css', yaml: {kw: 'true false null yes no', lc: '#'}, yml: 'yaml',
  toml: 'yaml', ini: 'yaml', conf: 'yaml', html: {kw: '', lc: null, tag: true}, xml: 'html', svg: 'html', vue: 'html', svelte: 'html',
  nix: {kw: 'let in with rec inherit if then else true false null', lc: '#', bc: true}, lua: {kw: 'and break do else elseif end false for function if in local nil not or repeat return then true until while', lc: '--'},
};
const langCache = {};
function langFor(path) {
  const ext = (path.match(/\.([A-Za-z0-9]+)$/) || [, ''])[1].toLowerCase();
  const base = path.split('/').pop().toLowerCase();
  let key = ext || (/^(makefile|dockerfile)$/.test(base) ? 'sh' : '');
  let l = LANGS[key]; while (typeof l === 'string') l = LANGS[l];
  if (!l) return null;
  if (!langCache[key]) {
    const parts = [];
    if (l.lc) parts.push('(' + l.lc.replace(/[.*+?^${}()|[\]\\\/]/g, '\\$&') + '.*)'); else parts.push('(\\0)');
    parts.push(l.bc ? '(\\/\\*.*?(?:\\*\\/|$))' : '(\\0)');
    parts.push('("(?:[^"\\\\]|\\\\.)*"?|\'(?:[^\'\\\\]|\\\\.)*\'?|`(?:[^`\\\\]|\\\\.)*`?)');
    parts.push('(\\b\\d[\\w.]*|#[0-9a-fA-F]{3,8}\\b)');
    parts.push(l.tag ? '(<\\/?[\\w:-]+|\\/?>)' : '(\\0)');
    parts.push('([A-Za-z_$][\\w$]*)');
    langCache[key] = {re: new RegExp(parts.join('|'), 'g'), kw: new Set(l.kw.split(' ').filter(Boolean))};
  }
  return langCache[key];
}
function tokenize(s, lang) {  // -> [{t, c}] segments
  if (!lang) return [{t: s, c: ''}];
  const out = []; let last = 0, m; lang.re.lastIndex = 0;
  while ((m = lang.re.exec(s))) {
    if (m.index > last) out.push({t: s.slice(last, m.index), c: ''});
    let c = '';
    if (m[1] != null || m[2] != null) c = 'hl-c';
    else if (m[3] != null) c = 'hl-s';
    else if (m[4] != null) c = 'hl-n';
    else if (m[5] != null) c = 'hl-k';
    else if (m[6] != null) { const w = m[6]; c = lang.kw.has(w) ? 'hl-k' : (s[lang.re.lastIndex] === '(' ? 'hl-f' : (/^[A-Z]/.test(w) && /[a-z]/.test(w) ? 'hl-t' : '')); }
    out.push({t: m[0], c}); last = lang.re.lastIndex;
    if (m[0] === '') lang.re.lastIndex++;
  }
  if (last < s.length) out.push({t: s.slice(last), c: ''});
  return out;
}
function renderCode(s, lang, marks) {  // marks: sorted [start,end) char ranges to <mark>
  const segs = tokenize(s, lang); let html = '', pos = 0, mi = 0;
  const wrap = (t, c, marked) => { let h = esc(t); if (c) h = '<span class="' + c + '">' + h + '</span>'; return marked ? '<mark>' + h + '</mark>' : h; };
  for (const seg of segs) {
    let start = pos, end = pos + seg.t.length;
    while (start < end) {
      while (mi < (marks || []).length && marks[mi][1] <= start) mi++;
      const mk = marks && marks[mi];
      if (mk && mk[0] <= start) { const e = Math.min(end, mk[1]); html += wrap(s.slice(start, e), seg.c, true); start = e; }
      else { const e = mk ? Math.min(end, mk[0]) : end; html += wrap(s.slice(start, e), seg.c, false); start = e; }
    }
    pos = end;
  }
  return html || '';
}

// ---------------------------------------------------------------- word-level diff
function wordDiff(a, b) {
  const ta = a.match(/\w+|\s+|[^\w\s]/g) || [], tb = b.match(/\w+|\s+|[^\w\s]/g) || [];
  if (ta.length * tb.length > 250000) return null;
  const n = ta.length, m = tb.length, L = Array.from({length: n + 1}, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = ta[i] === tb[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const ma = [], mb = []; let i = 0, j = 0, pa = 0, pb = 0, same = 0;
  const push = (arr, s, e) => { if (arr.length && arr[arr.length - 1][1] === s) arr[arr.length - 1][1] = e; else arr.push([s, e]); };
  while (i < n && j < m) {
    if (ta[i] === tb[j]) { same += ta[i].length; pa += ta[i].length; pb += tb[j].length; i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) { push(ma, pa, pa + ta[i].length); pa += ta[i].length; i++; }
    else { push(mb, pb, pb + tb[j].length); pb += tb[j].length; j++; }
  }
  while (i < n) { push(ma, pa, pa + ta[i].length); pa += ta[i].length; i++; }
  while (j < m) { push(mb, pb, pb + tb[j].length); pb += tb[j].length; j++; }
  if (same < 0.4 * Math.max(a.length, b.length)) return null;
  return [ma, mb];
}
function computeMarks(h) {  // marks per line index in hunk
  if (h._marks) return h._marks;
  const marks = h._marks = {}; const ls = h.lines; let i = 0;
  while (i < ls.length) {
    if (ls[i][0] !== '-') { i++; continue; }
    let d = i; while (d < ls.length && ls[d][0] === '-') d++;
    let a = d; while (a < ls.length && ls[a][0] === '+') a++;
    const nd = d - i, na = a - d;
    for (let k = 0; k < Math.min(nd, na); k++) {
      const r = wordDiff(ls[i + k][1], ls[d + k][1]);
      if (r) { marks[i + k] = r[0]; marks[d + k] = r[1]; }
    }
    i = a;
  }
  return marks;
}

// ---------------------------------------------------------------- sidebar
const badge = f => f.binary ? 'B' : {added: 'A', deleted: 'D', renamed: 'R', copied: 'C', mode: 'M'}[f.status] || '';
const badgeEl = f => badge(f) ? [el('span', {class: 'badge ' + badge(f)}, badge(f))] : [];
const counts = f => (f.adds ? '<span class="a">+' + f.adds + '</span> ' : '') + (f.dels ? '<span class="d">-' + f.dels + '</span>' : '');
function buildSidebar() {
  const ul = $('#filelist'); ul.innerHTML = '';
  files.forEach((f, i) => {
    const li = el('li', {'data-f': i, class: viewed.has(f.path) ? 'viewed' : '', title: f.path});
    li.append(el('input', {type: 'checkbox', title: 'Viewed'}), ...badgeEl(f),
      el('span', {class: 'name'}, esc(f.path)), el('span', {class: 'cnt'}, counts(f)));
    li.querySelector('input').checked = viewed.has(f.path);
    li.querySelector('input').onclick = e => { e.stopPropagation(); setViewed(i, e.target.checked); };
    li.onclick = () => jumpToFile(i);
    ul.append(li);
  });
  let ta = 0, td = 0; files.forEach(f => { ta += f.adds; td += f.dels; });
  $('#stats').innerHTML = files.length + ' file' + (files.length === 1 ? '' : 's') + ' <span class="a">+' + ta + '</span> <span class="d">-' + td + '</span>';
  $('#target').innerHTML = '<span>' + esc(D.target) + '</span>'; $('#target').title = D.target;
  $('#filter').oninput = e => { const q = e.target.value.toLowerCase(); $$('#filelist li').forEach(li => li.hidden = q && !files[+li.dataset.f].path.toLowerCase().includes(q)); };
}
function setViewed(i, on) {
  const f = files[i]; on ? viewed.add(f.path) : viewed.delete(f.path);
  const li = $('#filelist li[data-f="' + i + '"]'); li.classList.toggle('viewed', on); li.querySelector('input').checked = on;
  const sec = $('#file-' + i); if (sec) { sec.querySelector('.fhead input').checked = on; sec.classList.toggle('collapsed', on); }
  api('/viewed', {path: f.path, viewed: on});
}
function setActive(i) { activeFile = i; $$('#filelist li').forEach(li => li.classList.toggle('active', +li.dataset.f === i)); }

// ---------------------------------------------------------------- files & diff tables
const rendered = new Set();
function buildFiles() {
  const main = $('#main'); main.innerHTML = '';
  files.forEach((f, i) => {
    const sec = el('section', {class: 'file' + (viewed.has(f.path) ? ' collapsed' : ''), id: 'file-' + i});
    const head = el('div', {class: 'fhead'});
    const nm = f.status === 'renamed' || f.status === 'copied' ? '<span class="old">' + esc(f.old_path) + ' → </span>' + esc(f.path) : esc(f.path);
    head.append(el('button', {class: 'tog', title: 'Collapse'}, '▾'), ...badgeEl(f), el('span', {class: 'path'}, nm),
      el('span', {class: 'cnt'}, counts(f)));
    const lab = el('label'); const cb = el('input', {type: 'checkbox'}); cb.checked = viewed.has(f.path); cb.onchange = () => setViewed(i, cb.checked); lab.append(cb, 'Viewed'); head.append(lab);
    head.querySelector('.tog').onclick = () => sec.classList.toggle('collapsed');
    const nlines = f.hunks.reduce((a, h) => a + h.lines.length + 2, 0);
    const body = el('div', {class: 'fbody'}); body.append(el('div', {class: 'placeholder', style: 'min-height:' + Math.min(nlines * 20, 20000) + 'px'}, '…'));
    sec.append(head, body); main.append(sec);
  });
  const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) renderFile(+e.target.id.slice(5)); }), {root: main, rootMargin: '1000px 0px'});
  $$('#main .file').forEach(s => io.observe(s));
  main.onscroll = throttle(() => { const top = main.getBoundingClientRect().top; for (const s of $$('#main .file')) { const r = s.getBoundingClientRect(); if (r.bottom > top + 40) { setActive(+s.id.slice(5)); break; } } }, 150);
}
function throttle(fn, ms) { let t = 0; return () => { const n = Date.now(); if (n - t > ms) { t = n; fn(); } }; }
function renderFile(i, force) {
  if (rendered.has(i) && !force) return;
  rendered.add(i);
  const f = files[i], body = $('#file-' + i + ' .fbody'); body.innerHTML = '';
  if (f.binary) { body.append(el('div', {class: 'note'}, 'Binary file' + (f.status === 'added' ? ' added' : f.status === 'deleted' ? ' deleted' : ' changed') + '.')); return; }
  if (f.status === 'mode') { body.append(el('div', {class: 'note'}, 'Mode changed ' + f.old_mode + ' → ' + f.new_mode + '.')); return; }
  if (!f.hunks.length) { body.append(el('div', {class: 'note'}, (f.status === 'renamed' ? 'Renamed without changes' + (f.similarity ? ' (similarity ' + f.similarity + ')' : '') : f.status === 'added' ? 'Empty file added' : 'No content changes') + (f.old_mode && f.new_mode && f.old_mode !== f.new_mode ? '; mode ' + f.old_mode + ' → ' + f.new_mode : '') + '.')); return; }
  const tbl = el('table', {class: 'diff'}), tb = el('tbody'); body.append(tbl);
  const lang = langFor(f.path), cols = view === 'split' ? 4 : 3;
  tbl.innerHTML = view === 'split' ? '<colgroup><col class="cn"><col><col class="cn"><col></colgroup>' : '<colgroup><col class="cn"><col class="cn"><col></colgroup>';
  tbl.append(tb);
  if (f.old_mode && f.new_mode && f.old_mode !== f.new_mode) tb.append(el('tr', {class: 'hunk'}, '<td colspan="' + cols + '">mode ' + f.old_mode + ' → ' + f.new_mode + '</td>'));
  f.hunks.forEach((h, hi) => {
    const prev = f.hunks[hi - 1];
    const gapFrom = prev ? prev.new_start + prev.new_count : 1, gapTo = h.new_start - 1;
    if (gapTo >= gapFrom && D.expandable && f.status !== 'deleted') tb.append(gapRow(i, gapFrom, gapTo, h.new_start - h.old_start, cols, hi === 0 ? 'up' : 'both'));
    const hr = el('tr', {class: 'hunk', 'data-f': i, 'data-h': hi}, '<td colspan="' + cols + '">@@ -' + h.old_start + ',' + h.old_count + ' +' + h.new_start + ',' + h.new_count + ' @@<span class="section">' + esc(h.section || '') + '</span></td>');
    tb.append(hr);
    (view === 'split' ? splitRows : unifiedRows)(f, i, h, hi, lang).forEach(r => tb.append(r));
    if (hi === f.hunks.length - 1 && D.expandable && f.status !== 'deleted') {
      const last = h.new_start + h.new_count;
      tb.append(gapRow(i, last, -1, (h.new_start + h.new_count) - (h.old_start + h.old_count), cols, 'down'));
    }
  });
  Object.values(comments).forEach(c => { if (c.file === f.path) mountThread(c); });
}
function lineRow(f, fi, h, hi, li, lang, marks) {
  const l = h.lines[li], t = l[0], cls = t === '+' ? 'add' : t === '-' ? 'del' : 'ctx';
  const tr = el('tr', {class: 'line ' + cls, 'data-f': fi, 'data-h': hi, 'data-i': li});
  const code = '<td class="code" data-p="' + (t === ' ' ? '' : t) + '">' + renderCode(l[1], lang, marks[li]) + (l[4] ? '<span class="nonl" title="No newline at end of file">⏎✗</span>' : '') + '</td>';
  tr.innerHTML = '<td class="num">' + (l[2] ?? '') + '</td><td class="num g" data-side="' + (t === '-' ? 'old' : 'new') + '">' + (l[3] ?? '') + '<span class="gb" title="Add a comment (drag for a range)">+</span></td>' + code;
  return tr;
}
function unifiedRows(f, fi, h, hi, lang) { const marks = computeMarks(h); return h.lines.map((_, li) => lineRow(f, fi, h, hi, li, lang, marks)); }
function splitRows(f, fi, h, hi, lang) {
  const marks = computeMarks(h), rows = [], ls = h.lines;
  const cell = (li, side) => {
    if (li == null) return '<td class="num"></td><td class="code empty"></td>';
    const l = ls[li], t = l[0], k = t === '+' ? ' add' : t === '-' ? ' del' : '';
    return '<td class="num g' + k + '" data-side="' + side + '" data-i="' + li + '">' + ((side === 'old' ? l[2] : l[3]) ?? '') + '<span class="gb">+</span></td><td class="code' + k + '" data-i="' + li + '" data-p="' + (t === ' ' ? '' : t) + '">' + renderCode(l[1], lang, marks[li]) + (l[4] ? '<span class="nonl">⏎✗</span>' : '') + '</td>';
  };
  const mk = (a, b) => { const tr = el('tr', {class: 'line split', 'data-f': fi, 'data-h': hi, 'data-i': b ?? a}); tr.dataset.a = a ?? ''; tr.dataset.b = b ?? ''; tr.innerHTML = cell(a, 'old') + cell(b, 'new'); return tr; };
  let i = 0;
  while (i < ls.length) {
    if (ls[i][0] === ' ') { rows.push(mk(i, i)); i++; continue; }
    let d = i; while (d < ls.length && ls[d][0] === '-') d++;
    let a = d; while (a < ls.length && ls[a][0] === '+') a++;
    for (let k = 0; k < Math.max(d - i, a - d); k++) rows.push(mk(k < d - i ? i + k : null, k < a - d ? d + k : null));
    i = a;
  }
  return rows;
}
function gapRow(fi, from, to, delta, cols, kind) {
  const tr = el('tr', {class: 'hunk gap', 'data-from': from, 'data-to': to, 'data-delta': delta});
  const btn = (k, label) => '<button class="xp" data-k="' + k + '">' + label + '</button>';
  const n = to < 0 ? '' : ' (' + (to - from + 1) + ' lines)';
  tr.innerHTML = '<td colspan="' + cols + '">' + (kind !== 'down' ? btn('up', '⇡ 20') : '') + (kind !== 'up' ? btn('down', '⇣ 20') : '') + btn('all', 'expand all' + n) + '</td>';
  tr.onclick = e => { const k = e.target.dataset.k; if (k) expandGap(fi, tr, k); };
  return tr;
}
async function expandGap(fi, tr, k) {
  let from = +tr.dataset.from, to = +tr.dataset.to; const delta = +tr.dataset.delta;
  let qf = from, qt = to;
  if (k === 'up') qf = to < 0 ? from : Math.max(from, to - 19);
  else if (k === 'down') qt = to < 0 ? from + 19 : Math.min(to, from + 19);
  const r = await fetch('/lines?f=' + fi + '&from=' + qf + '&to=' + qt).then(x => x.json()).catch(() => null);
  if (!r || r.error) { banner(r && r.error ? 'Cannot show more lines: ' + r.error : 'Cannot reach clankback.'); return; }
  const lang = langFor(files[fi].path), cols = view === 'split' ? 4 : 3;
  const frag = document.createDocumentFragment();
  r.lines.forEach((s, j) => {
    const n = r.from + j, o = n - delta, c = renderCode(s, lang, null);
    const tr2 = el('tr', {class: 'ctx x'});
    tr2.innerHTML = view === 'split' ? '<td class="num">' + o + '</td><td class="code">' + c + '</td><td class="num">' + n + '</td><td class="code">' + c + '</td>' : '<td class="num">' + o + '</td><td class="num">' + n + '</td><td class="code">' + c + '</td>';
    frag.append(tr2);
  });
  const got = r.lines.length, eof = r.from + got - 1 >= r.total;
  if (k === 'up') { tr.after(frag); to = qf - 1; }
  else { tr.before(frag); from = r.from + got; }
  if (to < 0 && eof) to = from - 1;
  if (got === 0 || (to >= 0 && from > to)) { tr.remove(); return; }
  tr.dataset.from = from; tr.dataset.to = to;
  const btn = tr.querySelector('[data-k="all"]'); if (btn && to >= 0) btn.textContent = 'expand all (' + (to - from + 1) + ' lines)';
}

// ---------------------------------------------------------------- comments
function rowFor(fi, hi, li) {
  return $$('#file-' + fi + ' tr.line[data-h="' + hi + '"]').find(tr => view === 'split' ? (tr.dataset.a == li || tr.dataset.b == li) : +tr.dataset.i === li) || null;
}
function hunkIndex(fi, hash) { return files[fi].hunks.findIndex(h => h.hash === hash); }
function fileIndex(path) { return files.findIndex(f => f.path === path); }
function commentRowAfter(tr) {  // the .crow immediately following tr (create if missing)
  let n = tr.nextElementSibling;
  if (n && n.classList.contains('crow')) return n;
  const cols = view === 'split' ? 4 : 3;
  const cr = el('tr', {class: 'crow'}, '<td colspan="' + cols + '"></td>');
  tr.after(cr); return cr;
}
function where(c) {
  const a = c.line, b = c.end_line;
  return c.side + ' L' + (b != null && b !== a ? Math.min(a, b) + '–' + Math.max(a, b) : a);
}
function anchorRow(fi, line) {  // best row for a line whose hunk is gone: same new line, else the hunk after it, else last
  let row = null;
  if (line != null) files[fi].hunks.some((h, hi) => h.lines.some((l, li) => { if (l[3] === line || (l[3] == null && l[2] === line)) { row = rowFor(fi, hi, li); return true; } }));
  if (row) return row;
  const hs = $$('#file-' + fi + ' tr.hunk:not(.gap)');
  return hs.find(h => files[fi].hunks[+h.dataset.h].new_start > (line || 0)) || hs[hs.length - 1] || null;
}
function mountThread(c) {
  const fi = fileIndex(c.file); if (fi < 0) return;
  const hi = hunkIndex(fi, c.hunk);
  const tr = hi >= 0 ? rowFor(fi, hi, c.end_offset ?? c.offset) : anchorRow(fi, c.end_line ?? c.line);
  if (!tr) return;
  const cr = commentRowAfter(tr), old = cr.querySelector('[data-id="' + c.id + '"]');
  const t = threadEl(c); old ? old.replaceWith(t) : cr.firstElementChild.append(t);
}
function unmountThread(id) { const t = $('.thread[data-id="' + id + '"]'); if (!t) return; const cr = t.closest('tr.crow'); t.remove(); if (!cr.firstElementChild.children.length) cr.remove(); }
const folded = new Set();  // thread ids shown as a one-line tab (page-local, not saved)
function threadEl(c) {
  const t = el('div', {class: 'thread' + (c.resolved ? ' resolved' : '') + (c.outdated ? ' outdated' : '') + (c.by === 'claude' ? ' claude' : '') + (waiting(c) ? ' waiting' : ''), 'data-id': c.id});
  if (folded.has(c.id)) {
    const n = 1 + (c.replies || []).length, first = c.text.split(/(?<=[.!?])\s|\n/)[0].slice(0, 120);
    t.classList.add('tab'); t.title = 'Expand thread';
    t.innerHTML = '<span class="by">' + (c.by === 'claude' ? 'Claude' : 'You') + '</span> · ' + where(c) + ' · ' + n + (unseenIn(c).length ? ' · <span class="q">new</span>' : c.resolved ? ' · <span class="res">resolved</span>' : '') + '<span class="txt">' + esc(first) + '</span><span class="car down" title="Expand thread"></span>';
    t.onclick = () => { folded.delete(c.id); mountThread(c); refreshCount(); };
    return t;
  }
  const cm = (text, meta, extra, cls) => '<div class="cmt' + (cls || '') + '"><div class="meta"><span>' + meta + '</span><span class="grow"></span>' + (extra || '') + '</div><div class="txt">' + esc(text) + '</div></div>';
  const unsent = x => (x.sent ? '' : ' <span class="unsent">· unsent</span>') + (x === c && c.outdated ? ' <span class="out">· OUTDATED, line changed since</span>' : '');
  const fold = '<button data-a="fold" class="car up" title="Collapse thread"></button>';
  t.innerHTML = (c.by === 'claude'
      ? cm(c.text, '<span class="by">Claude</span> · ' + where(c) + (c.resolved ? ' · <span class="res">resolved</span>' : ' · <span class="q">for you to answer or resolve</span>') + unsent(c), fold, ' claude')
      : cm(c.text, '<span class="by">You</span> · ' + where(c) + (c.resolved ? ' · <span class="res">resolved</span>' : '') + unsent(c), fold)) +
    (c.replies || []).map(r => r.by === 'claude' ? cm(r.text, '<span class="by">Claude</span>', '', ' claude') : cm(r.text, '<span class="by">You</span>' + unsent(r))).join('') +
    '<div class="tfoot"><textarea placeholder="Reply…"></textarea><button class="small" data-a="reply">Reply</button><button class="small" data-a="resolve">' + (c.resolved ? 'Unresolve' : 'Resolve') + '</button>' +
    (hasUnsent(c) ? '<button class="small primary" data-a="send" title="Send this thread to the clanker now">Send</button>' : '') + '</div>';
  t.onclick = e => {
    const a = e.target.dataset.a; if (!a) return;
    if (a === 'fold') { folded.add(c.id); mountThread(c); refreshCount(); }
    else if (a === 'resolve') { const ta = t.querySelector('.tfoot textarea'), msg = ta.value.trim(); if (msg) (c.replies = c.replies || []).push({id: uid(), text: msg, created: Date.now() / 1000, by: 'you'}); c.resolved = !c.resolved; const p = save(c); if (msg) p.then(() => sendThread(c)); }
    else if (a === 'send') { e.target.disabled = true; e.target.textContent = 'Sent'; sendThread(c); }
    else if (a === 'reply') { const ta = t.querySelector('.tfoot textarea'); if (!ta.value.trim()) return; (c.replies = c.replies || []).push({id: uid(), text: ta.value.trim(), created: Date.now() / 1000, by: 'you'}); save(c); }
  };
  t.querySelector('.tfoot textarea').onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) t.querySelector('[data-a="reply"]').click(); };
  t.addEventListener('mouseenter', () => markSeen(c), {once: true});
  return t;
}
function save(c) { comments[c.id] = c; mountThread(c); const p = api('/comment', {comment: c}); refreshCount(); if (!$('#summary').hidden) buildSummary(); return p; }
const unseenIn = c => [c].concat(c.replies || []).filter(x => x.by === 'claude' && x.seen === false);
function markSeen(c) {
  const ids = unseenIn(c).map(x => x.id); if (!ids.length) return;
  unseenIn(c).forEach(x => x.seen = true); api('/seen', {ids}); refreshCount();
}
function claudeThreads(unseenOnly) {
  return Object.values(comments).filter(c => unseenOnly ? unseenIn(c).length : (c.by === 'claude' || (c.replies || []).some(r => r.by === 'claude')))
    .sort((a, b) => (fileIndex(a.file) - fileIndex(b.file)) || (a.line - b.line));
}
let navPos = -1;
function nextFromClaude(dir) {
  let list = claudeThreads(true); if (!list.length) { list = claudeThreads(false); if (!list.length) { toast('Nothing from clanker yet.'); return; } }
  navPos = (navPos + dir + list.length) % list.length; jumpToComment(list[navPos]);
}
function refreshCount() {
  const cs = Object.values(comments), open = cs.filter(c => !c.resolved).length;
  $('#ccount').textContent = cs.length + (open ? ' (' + open + ' open)' : '');
  $('#qcount').textContent = cs.filter(c => unseenIn(c).length).length;
  $('#foldBtn').textContent = cs.length && cs.every(c => folded.has(c.id)) ? 'Expand all' : 'Collapse all';
  const n = cs.filter(c => !c.sent && c.by !== 'claude').length + cs.reduce((a, c) => a + (c.replies || []).filter(r => r.by !== 'claude' && !r.sent).length, 0);
  $$('#sendBtn, #sendBtn2').forEach(b => { b.textContent = 'Send to clanker' + (n ? ' (' + n + ')' : ''); b.disabled = !n; });
}
function foldAll() {  // collapse every thread to a tab; when all are tabs, expand them
  const cs = Object.values(comments), on = !cs.every(c => folded.has(c.id));
  cs.forEach(c => on ? folded.add(c.id) : folded.delete(c.id));
  $$('.thread').forEach(t => mountThread(comments[t.dataset.id]));
  refreshCount();
}
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(t._t); t._t = setTimeout(() => t.hidden = true, 2500); }
async function send() {
  await api('/send'); toast('Sent to the clanker. Replies appear here as they arrive.');
}
const hasUnsent = c => (!c.sent && c.by !== 'claude') || (c.replies || []).some(r => r.by !== 'claude' && !r.sent);
const waiting = c => { const last = (c.replies || []).length ? c.replies[c.replies.length - 1] : c; return !c.resolved && last.by !== 'claude' && !!last.sent; };  // the clanker has it
async function sendThread(c) {  // one thread only; the top button sends everything
  await api('/send', {id: c.id}); toast('Sent this thread to the clanker.');
}
async function poll() {  // pick up replies and resolves made from the terminal
  let r; try { r = await (await fetch('/state')).json(); } catch (e) { return; }
  if (!r || !r.comments) return;
  let changed = false, fresh = null;
  for (const id in r.comments) {
    if (JSON.stringify(r.comments[id]) !== JSON.stringify(comments[id])) {
      const old = $('.thread[data-id="' + id + '"] .tfoot textarea'), draft = old && old.value;
      const before = comments[id] ? unseenIn(comments[id]).length : 0;
      comments[id] = r.comments[id]; mountThread(comments[id]); changed = true;
      if (!fresh && unseenIn(comments[id]).length > before) fresh = comments[id];
      const nw = $('.thread[data-id="' + id + '"] .tfoot textarea'); if (nw && draft) nw.value = draft;
    }
  }
  for (const id in comments) if (!r.comments[id]) { delete comments[id]; unmountThread(id); changed = true; }
  if (changed) { refreshCount(); if (!$('#summary').hidden) buildSummary(); }
  if (fresh && !$('.pointed')) jumpToComment(fresh);
  if (r.diff_rev && diffRev && r.diff_rev !== diffRev) await reloadDiff();
  if (r.focus && r.focus.seq !== focusSeq) { focusSeq = r.focus.seq; showFocus(r.focus); }
  if (r.status === 'finished' && !finished) { finished = true; $('#done').hidden = false; }
}
let focusSeq = D.focus && D.focus.seq;
function showFocus(f) {  // terminal asked us to scroll somewhere
  if (f.id) { if (comments[f.id]) jumpToComment(comments[f.id]); return; }
  const fi = fileIndex(f.file); if (fi < 0) return;
  renderFile(fi); $('#file-' + fi).classList.remove('collapsed'); setActive(fi);
  const target = anchorRow(fi, f.line) || $('#file-' + fi);
  target.scrollIntoView({block: 'center'}); if (target.classList.contains('line')) setFocus(target);
  point(target);
}

let composer = null;
function openComposer(fi, hi, li, endLi, side) {
  closeComposer();
  const h = files[fi].hunks[hi], a = Math.min(li, endLi), b = Math.max(li, endLi);
  const tr = rowFor(fi, hi, b); if (!tr) return;
  const ln = i => { const l = h.lines[i]; return l[3] ?? l[2]; };
  const rng = side + ' L' + (a === b ? ln(a) : ln(a) + '–' + ln(b));
  const cr = commentRowAfter(tr);
  composer = el('div', {class: 'composer'}, '<div class="rng">Comment on ' + rng + '</div><textarea placeholder="Leave a comment… (Ctrl+Enter to save)"></textarea><div class="btns"><button class="primary" data-a="save">Add comment</button><button data-a="cancel">Cancel</button></div>');
  cr.firstElementChild.prepend(composer);
  const ta = composer.querySelector('textarea'); ta.focus();
  const submit = () => {
    const text = ta.value.trim(); if (!text) return;
    const c = {id: uid(), file: files[fi].path, hunk: h.hash, offset: a, end_offset: a === b ? null : b, side, line: ln(a), end_line: a === b ? null : ln(b),
      line_text: h.lines[a][0] + h.lines[a][1], text, created: Date.now() / 1000, resolved: false, replies: []};
    closeComposer(); save(c);
  };
  composer.querySelector('[data-a="save"]').onclick = submit;
  composer.querySelector('[data-a="cancel"]').onclick = closeComposer;
  ta.onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit(); if (e.key === 'Escape') closeComposer(); e.stopPropagation(); };
  clearSel();
}
function closeComposer() { if (!composer) return; const cr = composer.closest('tr.crow'); composer.remove(); composer = null; if (cr && !cr.firstElementChild.children.length) cr.remove(); }
function clearSel() { $$('tr.sel').forEach(r => r.classList.remove('sel')); }

// gutter click / drag
function gutterInfo(target) {
  const td = target.closest('td.num.g'); if (!td) return null;
  const tr = td.closest('tr.line'); if (!tr) return null;
  const li = view === 'split' ? +td.dataset.i : +tr.dataset.i;
  return {fi: +tr.dataset.f, hi: +tr.dataset.h, li, side: td.dataset.side, tr};
}
document.addEventListener('mousedown', e => {
  const g = gutterInfo(e.target); if (!g || e.button !== 0) return;
  e.preventDefault(); drag = {...g, end: g.li, endSide: g.side}; document.body.style.userSelect = 'none';
  clearSel(); g.tr.classList.add('sel');
});
document.addEventListener('mouseover', e => {
  const tr = e.target.closest && e.target.closest('tr.line'); if (tr) focusRow = tr;
  if (!drag) return;
  const g = gutterInfo(e.target) || (tr && tr.dataset.f == drag.fi && tr.dataset.h == drag.hi ? {fi: +tr.dataset.f, hi: +tr.dataset.h, li: +tr.dataset.i, side: drag.side, tr} : null);
  if (!g || g.fi !== drag.fi || g.hi !== drag.hi) return;
  drag.end = g.li; drag.endSide = g.side;
  const a = Math.min(drag.li, drag.end), b = Math.max(drag.li, drag.end); clearSel();
  $$('#file-' + drag.fi + ' tr.line[data-h="' + drag.hi + '"]').forEach(r => { const i = view === 'split' ? Math.max(+r.dataset.a || -1, +r.dataset.b || -1) : +r.dataset.i; if (i >= a && i <= b) r.classList.add('sel'); });
});
document.addEventListener('mouseup', () => {
  if (!drag) return; const d = drag; drag = null; document.body.style.userSelect = '';
  const endLine = files[d.fi].hunks[d.hi].lines[d.end], side = endLine[0] === '-' ? 'old' : endLine[0] === '+' ? 'new' : d.endSide;
  openComposer(d.fi, d.hi, d.li, d.end, side);
});

// ---------------------------------------------------------------- summary / finish
function buildSummary() {
  const list = $('#sumlist'); list.innerHTML = '';
  const cs = Object.values(comments).sort((a, b) => (fileIndex(a.file) - fileIndex(b.file)) || (a.line - b.line));
  if (!cs.length) list.innerHTML = '<div class="note">No comments yet. Hover a line and click +, or press c.</div>';
  let cur = null;
  cs.forEach(c => {
    if (c.file !== cur) { cur = c.file; list.append(el('h4', null, esc(c.file))); }
    const it = el('div', {class: 'sumitem' + (c.outdated ? ' outdated' : '') + (c.resolved ? ' resolved' : '')},
      '<div class="where">' + (c.by === 'claude' ? 'Claude · ' : '') + where(c) + (unseenIn(c).length ? ' · <span class="q">new from clanker</span>' : '') + (c.outdated ? ' · OUTDATED' : '') + (c.resolved ? ' · <span class="res">resolved</span>' : '') + (c.replies && c.replies.length ? ' · ' + c.replies.length + ' repl' + (c.replies.length === 1 ? 'y' : 'ies') : '') + '</div>' +
      (c.line_text ? '<div class="where">' + esc(c.line_text.slice(0, 80)) + '</div>' : '') + '<div class="txt">' + esc(c.text) + '</div>');
    it.onclick = () => jumpToComment(c); list.append(it);
  });
}
function jumpToComment(c) {
  const fi = fileIndex(c.file); if (fi < 0) return;
  renderFile(fi); $('#file-' + fi).classList.remove('collapsed');
  if (folded.has(c.id)) { folded.delete(c.id); mountThread(c); refreshCount(); }
  const t = $('.thread[data-id="' + c.id + '"]') || $('#file-' + fi);
  t.scrollIntoView({block: 'center'}); point(t);
}
function point(el) {  // blink until the mouse reaches it
  $$('.pointed').forEach(e => e.classList.remove('pointed'));
  el.classList.add('pointed');
  el.addEventListener('mouseenter', () => el.classList.remove('pointed'), {once: true});
}
function toggleSummary(on) { const s = $('#summary'); s.hidden = on == null ? !s.hidden : !on; if (!s.hidden) buildSummary(); }
async function finish() {
  if (!confirm('Finish the review? Unsent comments go to Claude and the review closes.')) return;
  finished = true; await api('/finish'); $('#done').hidden = false;
}

// ---------------------------------------------------------------- navigation / keys
function jumpToFile(i) { renderFile(i); const s = $('#file-' + i); s.classList.remove('collapsed'); s.scrollIntoView({block: 'start'}); setActive(i); }
function hunkList() { const out = []; files.forEach((f, fi) => f.hunks.forEach((h, hi) => out.push([fi, hi]))); return out; }
let hunkPos = -1;
function gotoHunk(dir) {
  const hs = hunkList(); if (!hs.length) return;
  if (focusRow && focusRow.dataset.h != null) hunkPos = hs.findIndex(x => x[0] === +focusRow.dataset.f && x[1] === +focusRow.dataset.h);
  hunkPos = Math.max(0, Math.min(hs.length - 1, hunkPos + dir));
  const [fi, hi] = hs[hunkPos]; renderFile(fi); $('#file-' + fi).classList.remove('collapsed');
  const hr = $('#file-' + fi + ' tr.hunk[data-h="' + hi + '"]'); if (!hr) return;
  hr.scrollIntoView({block: 'center'}); setFocus(hr.nextElementSibling); setActive(fi);
}
function setFocus(tr) { $$('tr.focus').forEach(r => r.classList.remove('focus')); focusRow = tr; if (tr) tr.classList.add('focus'); }
function moveFocus(dir) {
  const rows = $$('#main tr.line'); if (!rows.length) return;
  let i = focusRow ? rows.indexOf(focusRow) : -1; i = Math.max(0, Math.min(rows.length - 1, i + dir));
  setFocus(rows[i]); rows[i].scrollIntoView({block: 'nearest'});
}
document.addEventListener('keydown', e => {
  const tag = e.target.tagName; if (tag === 'TEXTAREA' || tag === 'INPUT') { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (k === 'j') gotoHunk(1); else if (k === 'k') gotoHunk(-1);
  else if (k === 'n') jumpToFile(Math.min(files.length - 1, activeFile + 1)); else if (k === 'p') jumpToFile(Math.max(0, activeFile - 1));
  else if (k === 'ArrowDown') { moveFocus(1); e.preventDefault(); } else if (k === 'ArrowUp') { moveFocus(-1); e.preventDefault(); }
  else if (k === 'c' && focusRow && focusRow.dataset.h != null) { const li = view === 'split' ? +(focusRow.dataset.b || focusRow.dataset.a) : +focusRow.dataset.i; const l = files[+focusRow.dataset.f].hunks[+focusRow.dataset.h].lines[li]; openComposer(+focusRow.dataset.f, +focusRow.dataset.h, li, li, l[0] === '-' ? 'old' : 'new'); }
  else if (k === '/') { e.preventDefault(); $('#filter').focus(); $('#filter').select(); }
  else if (k === ']') nextFromClaude(1); else if (k === '[') nextFromClaude(-1);
  else if (k === 'v') toggleView(); else if (k === 's') toggleSummary(); else if (k === 'b') toggleSide();
  else if (k === '?') $('#help').hidden = !$('#help').hidden;
  else if (k === 'Escape') { closeComposer(); toggleSummary(false); $('#help').hidden = true; clearSel(); }
  else return;
});
function toggleView() {
  view = view === 'split' ? 'unified' : 'split'; $('#viewToggle').textContent = view === 'split' ? 'Unified' : 'Split';
  api('/prefs', {view}); closeComposer();
  const was = Array.from(rendered); rendered.clear(); was.forEach(i => renderFile(i, true));
}

function toggleSide() { const on = $('#side').classList.toggle('hidden'); api('/prefs', {sidebar: !on}); }

// ---------------------------------------------------------------- init
buildSidebar(); buildFiles(); refreshCount();
$('#viewToggle').textContent = view === 'split' ? 'Unified' : 'Split';
$('#viewToggle').onclick = toggleView; $('#sideToggle').onclick = toggleSide;
if (D.prefs && D.prefs.sidebar === false) $('#side').classList.add('hidden');
$('#summaryBtn').onclick = () => toggleSummary(); $('#closeSummary').onclick = () => toggleSummary(false);
$('#claudeBtn').onclick = () => nextFromClaude(1); $('#foldBtn').onclick = foldAll;
$('#finishBtn').onclick = finish; $('#sendBtn').onclick = send; $('#sendBtn2').onclick = send;
setInterval(() => { if (!finished) poll(); }, 2000);
if (Object.values(comments).some(c => c.outdated)) banner('Some comments point at code that has changed since. They are marked OUTDATED in the comments list.');
if (Object.keys(comments).length) toggleSummary(true);
setInterval(() => { if (!finished) api('/beat'); }, 5000);
window.addEventListener('pagehide', () => { if (!finished) try { navigator.sendBeacon('/bye', '{}'); } catch (e) {} });
