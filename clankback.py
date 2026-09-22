#!/usr/bin/env python3
"""clankback: review a local diff in the browser, GitHub-PR style. Stdlib + git only."""
import sys, os, re, json, hashlib, subprocess, time, threading, html, collections, fcntl, signal
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.realpath(__file__))
STATE_DIR = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'clankback')
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
GIT = ['git', '-c', 'core.quotepath=false']


def state_path(key):
    return os.path.join(STATE_DIR, key + '.json')


def run_path(key):
    return os.path.join(STATE_DIR, key + '.run')


def run(cmd, cwd=None, input=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors='replace', input=input)


def die(msg):
    print(msg)
    sys.exit(1)


# ---------------------------------------------------------------- arguments / target

def parse_args(argv):
    opts = {'staged': False, 'resume': False, 'discard': False}
    pos, paths, dd = [], [], False
    for a in argv:
        if dd:
            paths.append(a)
        elif a == '--':
            dd = True
        elif a.startswith('--'):
            k = a[2:]
            if k not in opts:
                die('Unknown option %s. Options: --staged --resume --discard' % a)
            opts[k] = True
        else:
            pos.append(a)
    return opts, pos, paths


def resolve_target(opts, pos, paths, cwd):
    """Return a target dict: mode, desc, diff text producer, new-side reader."""
    t = {'mode': None, 'desc': '', 'root': None, 'paths': list(paths), 'ref': None, 'pr': None, 'files': None}
    r = run(GIT + ['rev-parse', '--show-toplevel'], cwd)
    root = r.stdout.strip() if r.returncode == 0 else None
    if len(pos) == 2 and not paths and all(os.path.isfile(p) for p in pos):
        tracked = root and run(GIT + ['ls-files', '--error-unmatch', '--'] + pos, cwd).returncode == 0
        if not tracked:  # two arbitrary files; two tracked files act as a path filter instead
            t.update(mode='files', files=[os.path.abspath(p) for p in pos], names=list(pos), desc='%s vs %s' % (pos[0], pos[1]))
            return t
    if not root:
        fresh = pos + paths  # outside a repo, review the given files/dirs as new, against an empty baseline
        if not fresh:
            die('Not inside a git repository. Give paths to review as new files, or two files to compare.')
        for p in fresh:
            if not os.path.exists(os.path.join(cwd, p)):
                die('No such path: %s' % p)
        rel = [os.path.relpath(os.path.join(cwd, p), cwd) for p in fresh]
        t.update(mode='fresh', root=cwd, paths=[], files=rel, desc='%s vs empty' % ' '.join(rel))
        return t
    t['root'] = root
    for p in pos:
        if os.path.exists(p):
            t['paths'].append(p)
        elif p.isdigit():
            t['pr'] = int(p)
        elif run(GIT + ['rev-parse', '--verify', '--quiet', p + '^{tree}'], cwd).returncode == 0:
            t['ref'] = p
        else:
            die('Cannot interpret %r as a path, ref or PR number.' % p)
    if t['pr']:
        t['mode'] = 'pr'
        t['desc'] = 'PR #%d' % t['pr']
    elif opts['staged']:
        t['mode'] = 'staged'
        t['desc'] = 'staged vs HEAD'
    elif t['ref']:
        t['mode'] = 'ref'
        t['desc'] = 'working tree vs merge-base with %s' % t['ref']
    else:
        t['mode'] = 'working'
        t['desc'] = 'working tree vs HEAD'
    if t['paths']:
        t['desc'] += ' in ' + ' '.join(t['paths'])
    return t


def head_or_empty(cwd):
    return 'HEAD' if run(GIT + ['rev-parse', '--verify', '--quiet', 'HEAD'], cwd).returncode == 0 else EMPTY_TREE


def new_file_diff(path, cwd, common):
    """A 'new file' patch for a file or directory that has no baseline."""
    base = '/dev/null'
    if os.path.isdir(os.path.join(cwd, path)):
        base = os.path.join(STATE_DIR, 'empty')
        os.makedirs(base, exist_ok=True)
    return run(GIT + ['diff', '--no-index'] + common + ['--', base, path], cwd).stdout


def produce_diff(t, cwd):
    common = ['--no-color', '--no-ext-diff', '-M', '-U3']
    pathargs = (['--'] + t['paths']) if t['paths'] else []
    if t['mode'] == 'files':
        return run(GIT + ['diff', '--no-index'] + common + ['--'] + t['files'], cwd).stdout
    if t['mode'] == 'fresh':
        return ''.join(new_file_diff(p, cwd, common) for p in t['files'])
    if t['mode'] == 'pr':
        r = run(['gh', 'pr', 'diff', str(t['pr'])], cwd)
        if r.returncode != 0:
            die('gh pr diff failed: ' + r.stderr.strip())
        v = run(['gh', 'pr', 'view', str(t['pr']), '--json', 'headRefOid', '-q', '.headRefOid'], cwd)
        t['head_sha'] = v.stdout.strip() or None
        return r.stdout
    if t['mode'] == 'staged':
        return run(GIT + ['diff', '--cached'] + common + [head_or_empty(cwd)] + pathargs, cwd).stdout
    if t['mode'] == 'ref':
        mb = run(GIT + ['merge-base', t['ref'], 'HEAD'], cwd).stdout.strip()
        t['base'] = mb or t['ref']  # a bare tree (e.g. the empty tree) has no merge-base
    else:
        t['base'] = head_or_empty(cwd)
    out = run(GIT + ['diff'] + common + [t['base']] + pathargs, cwd).stdout
    untracked = run(GIT + ['ls-files', '--others', '--exclude-standard', '--full-name', '-z'] + pathargs, cwd).stdout
    return out + ''.join(new_file_diff(p, t['root'], common) for p in untracked.split('\0') if p)


# ---------------------------------------------------------------- diff parser

def unquote_path(p):
    if not (p.startswith('"') and p.endswith('"')):
        return p
    out, s, i = bytearray(), p[1:-1], 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            n = s[i + 1]
            if n in '01234567' and i + 3 < len(s):
                out.append(int(s[i + 1:i + 4], 8)); i += 4; continue
            out.extend({'n': b'\n', 't': b'\t', '\\': b'\\', '"': b'"'}.get(n, n.encode()))
            i += 2
        else:
            out.extend(c.encode()); i += 1
    return out.decode('utf-8', 'replace')


def strip_prefix(p, pre):
    p = unquote_path(p)
    return p[len(pre):] if p.startswith(pre) else p


HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$')


def parse_diff(text):
    files, cur, hunk = [], None, None
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()

    def new_file():
        return {'path': None, 'old_path': None, 'status': 'modified', 'binary': False, 'old_mode': None,
                'new_mode': None, 'adds': 0, 'dels': 0, 'hunks': [], 'similarity': None}

    for l in lines:
        if l.startswith('diff --git '):
            cur = new_file(); hunk = None; files.append(cur)
            m = re.match(r'^diff --git (?:"?a/)(.*?)"? (?:"?b/)(.*?)"?$', l)
            if m:
                cur['old_path'], cur['path'] = unquote_path(m.group(1)), unquote_path(m.group(2))
            continue
        if cur is None:
            continue
        if hunk is not None and (l[:1] in (' ', '+', '-', '\\') or l == ''):
            if l.startswith('\\'):
                if hunk['lines']:
                    hunk['lines'][-1][4] = True
                continue
            if l == '':
                l = ' '
            t, s = l[0], l[1:]
            if t == ' ':
                hunk['lines'].append([t, s, hunk['_o'], hunk['_n'], False]); hunk['_o'] += 1; hunk['_n'] += 1
            elif t == '-':
                hunk['lines'].append([t, s, hunk['_o'], None, False]); hunk['_o'] += 1; cur['dels'] += 1
            else:
                hunk['lines'].append([t, s, None, hunk['_n'], False]); hunk['_n'] += 1; cur['adds'] += 1
            continue
        m = HUNK_RE.match(l)
        if m:
            os_, oc, ns, nc, sec = m.groups()
            hunk = {'old_start': int(os_), 'old_count': int(oc) if oc is not None else 1,
                    'new_start': int(ns), 'new_count': int(nc) if nc is not None else 1,
                    'section': sec, 'lines': [], '_o': int(os_), '_n': int(ns)}
            cur['hunks'].append(hunk)
            continue
        hunk = None
        if l.startswith('--- '):
            p = l[4:]
            cur['old_path'] = None if p == '/dev/null' else strip_prefix(p, 'a/')
        elif l.startswith('+++ '):
            p = l[4:]
            cur['path'] = None if p == '/dev/null' else strip_prefix(p, 'b/')
        elif l.startswith('old mode '):
            cur['old_mode'] = l[9:]
        elif l.startswith('new mode '):
            cur['new_mode'] = l[9:]
        elif l.startswith('new file mode '):
            cur['status'] = 'added'; cur['new_mode'] = l[14:]
        elif l.startswith('deleted file mode '):
            cur['status'] = 'deleted'; cur['old_mode'] = l[18:]
        elif l.startswith('similarity index '):
            cur['similarity'] = l[17:]
        elif l.startswith('rename from '):
            cur['old_path'] = unquote_path(l[12:]); cur['status'] = 'renamed'
        elif l.startswith('rename to '):
            cur['path'] = unquote_path(l[10:])
        elif l.startswith('copy from '):
            cur['old_path'] = unquote_path(l[10:]); cur['status'] = 'copied'
        elif l.startswith('copy to '):
            cur['path'] = unquote_path(l[8:])
        elif l.startswith('Binary files ') or l.startswith('GIT binary patch'):
            cur['binary'] = True
    for f in files:
        if f['path'] is None:
            f['path'] = f['old_path']
        if f['old_path'] is None:
            f['old_path'] = f['path']
        if f['status'] == 'modified' and f['old_mode'] and f['new_mode'] and not f['hunks'] and not f['binary']:
            f['status'] = 'mode'
        seen = collections.Counter()
        for h in f['hunks']:
            del h['_o'], h['_n']
            body = '\n'.join(l[0] + l[1] for l in h['lines'])
            hsh = hashlib.sha1(body.encode('utf-8', 'replace')).hexdigest()[:16]
            seen[hsh] += 1
            h['hash'] = hsh if seen[hsh] == 1 else '%s#%d' % (hsh, seen[hsh])
    return files


# ---------------------------------------------------------------- state

def load_json(p, default):
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(p, data):
    tmp = p + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, p)


@contextmanager
def locked_state(path):
    """Read-modify-write the review state under a file lock (the daemon and CLI both write it)."""
    with open(path + '.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load_json(path, None) or {'comments': {}, 'viewed': []}
        yield st
        st['updated'] = time.time()
        save_json(path, st)


def pending_reviews():
    out = []
    for fn in os.listdir(STATE_DIR) if os.path.isdir(STATE_DIR) else []:
        if fn.endswith('.json') and fn != 'prefs.json':
            d = load_json(os.path.join(STATE_DIR, fn), None)
            if d and d.get('status') == 'pending':
                out.append(d)
    return sorted(out, key=lambda d: d.get('updated', 0), reverse=True)


def live_reviews():
    """Pending reviews whose daemon is still running, most recent first."""
    return [p for p in pending_reviews() if run_info(p.get('key', ''))]


def run_info(key):
    """The live daemon for this review, or None."""
    info = load_json(run_path(key), None)
    if not info:
        return None
    try:
        os.kill(info['pid'], 0)
        return info
    except OSError:
        return None


def attach_comments(files, comments):
    """Recompute line numbers from hunk offsets; flag comments whose hunk is gone."""
    by_key = {(f['path'], h['hash']): h for f in files for h in f['hunks']}
    for c in comments.values():
        h = by_key.get((c['file'], c['hunk']))
        c['outdated'] = h is None
        if h is None:
            continue
        def ln(off):
            l = h['lines'][min(off, len(h['lines']) - 1)]
            return l[3] if l[3] is not None else l[2]
        c['line'] = ln(c['offset'])
        if c.get('end_offset') is not None:
            c['end_line'] = ln(c['end_offset'])


# ---------------------------------------------------------------- server (daemon)

class Review:
    def __init__(self, spath, target, cwd):
        self.spath, self.target, self.cwd = spath, target, cwd
        self.new_side = new_side_reader(target)
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.last_seen = self.bye_at = None
        self.files, self.html, self.cache, self.fp, self.port = [], b'', {}, None, None

    def fingerprint(self):
        """Cheap change signal: mtimes of the files in the diff plus the git index."""
        paths = [os.path.join(self.target['root'] or '', f['path']) for f in self.files] if self.target['mode'] != 'files' else self.target['files']
        if self.target['root']:
            paths.append(os.path.join(self.target['root'], '.git', 'index'))
        out = []
        for p in paths:
            try:
                st = os.stat(p); out.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((p, 0, 0))
        return tuple(out)

    def refresh(self):
        """Re-run the diff so a browser (re)load shows the current working tree."""
        files = parse_diff(produce_diff(self.target, self.cwd))
        if self.target['mode'] == 'files' and files:
            files[0]['old_path'], files[0]['path'] = self.target['names']
        rev = hashlib.sha1(json.dumps([(f['path'], [h['hash'] for h in f['hunks']]) for f in files]).encode()).hexdigest()[:8]
        with self.lock:
            with locked_state(self.spath) as st:
                attach_comments(files, st['comments'])
                st['files'] = [{'path': f['path'], 'old_path': f['old_path'], 'status': f['status']} for f in files]
                st['diff_rev'] = rev
                html_ = render_html(files, st, self.target)
            self.files, self.cache, self.html = files, {}, html_
            self.fp = self.fingerprint()

    def lines(self, fi, frm, to):
        f = self.files[fi]
        if f['path'] not in self.cache:
            self.cache[f['path']] = self.new_side(f)
        text = self.cache[f['path']]
        if text is None:
            return {'error': 'file content not available'}
        ls = text.split('\n')
        if ls and ls[-1] == '':
            ls.pop()
        to = len(ls) if to < 0 else min(to, len(ls))
        return {'lines': ls[frm - 1:to], 'from': frm, 'total': len(ls)}


def make_handler(rv):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _trusted(self):
            """Only the page we served may talk to us: blocks DNS rebinding and cross-site POSTs from other tabs."""
            own = '127.0.0.1:%d' % rv.port
            return self.headers.get('Host') == own and self.headers.get('Origin') in (None, 'http://' + own)

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if not self._trusted():
                return self._json({'error': 'forbidden'}, 403)
            u = urlparse(self.path)
            rv.last_seen = time.time()
            if u.path == '/':
                rv.refresh()  # every (re)load shows the current diff and comments
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', str(len(rv.html)))
                self.end_headers()
                self.wfile.write(rv.html)
            elif u.path == '/state':
                if rv.fingerprint() != rv.fp:
                    rv.refresh()
                st = load_json(rv.spath, {})
                self._json({'comments': st.get('comments', {}), 'status': st.get('status'), 'updated': st.get('updated'),
                            'focus': st.get('focus'), 'diff_rev': st.get('diff_rev')})
            elif u.path == '/data':  # fresh diff for in-place re-render
                with rv.lock:
                    self._json({'files': rv.files, 'diff_rev': load_json(rv.spath, {}).get('diff_rev')})
            elif u.path == '/lines':
                q = parse_qs(u.query)
                try:
                    self._json(rv.lines(int(q['f'][0]), int(q['from'][0]), int(q['to'][0])))
                except (KeyError, ValueError, IndexError) as e:
                    self._json({'error': str(e)}, 400)
            else:
                self._json({'error': 'not found'}, 404)

        def do_POST(self):
            if not self._trusted():
                return self._json({'error': 'forbidden'}, 403)
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
            p = self.path
            rv.last_seen = time.time()
            if p == '/beat':
                rv.bye_at = None
            elif p == '/bye':
                rv.bye_at = time.time()
            elif p == '/prefs':
                prefs = load_json(os.path.join(STATE_DIR, 'prefs.json'), {})
                prefs.update(body)
                save_json(os.path.join(STATE_DIR, 'prefs.json'), prefs)
            elif p in ('/comment', '/viewed', '/send', '/finish', '/seen'):
                with locked_state(rv.spath) as st:
                    if p == '/comment':
                        c = body['comment']
                        old = st['comments'].get(c['id'], {})
                        c['sent'] = old.get('sent', False)
                        if old.get('by') == 'claude':
                            c['by'], c['seen'], c['text'] = 'claude', old.get('seen', True), old.get('text', c.get('text'))
                        oldr = {r['id']: r for r in old.get('replies', [])}
                        for r in c.get('replies', []):
                            o = oldr.get(r['id'], {})
                            r['sent'] = o.get('sent', r.get('by') == 'claude')
                            if 'seen' in o:
                                r['seen'] = o['seen']
                        st['comments'][c['id']] = c
                    elif p == '/viewed':
                        v = set(st.get('viewed', []))
                        (v.add if body['viewed'] else v.discard)(body['path'])
                        st['viewed'] = sorted(v)
                    elif p == '/seen':
                        ids = set(body.get('ids', []))
                        for c in st['comments'].values():
                            for x in [c] + c.get('replies', []):
                                if x.get('id') in ids:
                                    x['seen'] = True
                    else:
                        st['send_seq'] = st.get('send_seq', 0) + 1
                        if p == '/finish':
                            st['finish_requested'] = True
                        if p == '/send' and body.get('id'):  # one thread's Send button
                            st.setdefault('send_ids', []).append(body['id'])
                        else:
                            st['send_all'] = True
                if p == '/finish':
                    rv.finished.set()
            else:
                self._json({'error': 'not found'}, 404)
                return
            self._json({'ok': True})
    return H


def new_side_reader(t):
    def read(f):
        if f['binary'] or f['status'] == 'deleted':
            return None
        try:
            if t['mode'] == 'files':
                with open(t['files'][1], encoding='utf-8', errors='replace') as fh:
                    return fh.read()
            if t['mode'] == 'staged':
                r = run(GIT + ['show', ':' + f['path']], t['root'])
            elif t['mode'] == 'pr':
                if not t.get('head_sha'):
                    return None
                r = run(GIT + ['show', '%s:%s' % (t['head_sha'], f['path'])], t['root'])
            else:
                with open(os.path.join(t['root'], f['path']), encoding='utf-8', errors='replace') as fh:
                    return fh.read()
            return r.stdout if r.returncode == 0 else None
        except OSError:
            return None
    return read


def render_html(files, state, target):
    def rd(n):
        with open(os.path.join(HERE, n), encoding='utf-8') as fh:
            return fh.read()
    data = {'files': files, 'comments': state['comments'], 'viewed': state.get('viewed', []),
            'prefs': load_json(os.path.join(STATE_DIR, 'prefs.json'), {}), 'target': target['desc'],
            'cwd': state['cwd'], 'focus': state.get('focus'), 'diff_rev': state.get('diff_rev'), 'expandable': target['mode'] != 'pr' or bool(target.get('head_sha'))}
    js_data = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    page = rd('viewer.html')
    for k, v in (('{{CSS}}', rd('viewer.css')), ('{{JS}}', rd('viewer.js')), ('{{DATA}}', js_data), ('{{TITLE}}', html.escape(target['desc']))):
        page = page.replace(k, v)
    return page.encode('utf-8')


def serve(spath):
    """Daemon: serve the page until the tab closes or Finish is clicked."""
    st = load_json(spath, None)
    if not st:
        return
    key, t = st['key'], st['t']
    rv = Review(spath, t, st['cwd'])
    rv.refresh()
    srv = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(rv))
    rv.port = srv.server_address[1]
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    save_json(run_path(key), {'pid': os.getpid(), 'url': 'http://127.0.0.1:%d/' % srv.server_address[1]})
    start = time.time()
    try:
        while not rv.finished.wait(1):
            now = time.time()
            if rv.last_seen is None:
                if now - start > 120:
                    break
            elif rv.bye_at and now - rv.bye_at > 5 and rv.last_seen <= rv.bye_at + 0.5:
                break
            elif now - rv.last_seen > 100:
                break
        if rv.finished.is_set():
            time.sleep(1)  # let the page fetch its final state
    finally:
        with locked_state(spath) as st:
            st['status'] = 'finished' if rv.finished.is_set() else 'pending'
        try:
            os.remove(run_path(key))
        except OSError:
            pass
        srv.shutdown()


def spawn_daemon(spath, key):
    subprocess.Popen([sys.executable, os.path.realpath(__file__), '--serve', spath], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    for _ in range(100):
        info = run_info(key)
        if info:
            return info
        time.sleep(0.1)
    die('The review server did not start.')


def open_browser(url):
    cmd = os.environ.get('BROWSER') or 'xdg-open'
    try:
        subprocess.Popen([cmd, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        print('Could not open a browser. Open %s yourself.' % url)


# ---------------------------------------------------------------- report

def fmt_where(c):
    side = c.get('side', 'new')
    if c.get('end_line') is not None and c['end_line'] != c['line']:
        a, b = sorted([c['line'], c['end_line']])
        return '%s L%d-L%d (%d lines)' % (side, a, b, b - a + 1)
    return '%s L%s' % (side, c.get('line'))


def fmt_quote(c):
    q = (c.get('line_text') or '').rstrip().replace('`', "'")
    if len(q) > 100:
        q = q[:97] + '...'
    return ' `%s`' % q if q else ''


def indent(text, pad='  '):
    return '\n'.join(pad + l for l in text.rstrip().split('\n'))


def report(st, desc, only=None):
    """Print what the user sent this round (unsent comments and unsent replies) and mark it sent."""
    comments = [c for c in st['comments'].values() if only is None or c['id'] in only]
    order = {f['path']: i for i, f in enumerate(st.get('files', []))}
    fresh = [c for c in comments if not c.get('sent') or any(not r.get('sent') for r in c.get('replies', []))
             or (c.get('by') == 'claude' and c.get('resolved') and not c.get('resolved_sent'))]
    fresh.sort(key=lambda c: (order.get(c['file'], 10 ** 6), c.get('line') or 0))
    ncom = sum(1 for c in fresh if not c.get('sent'))
    nrep = sum(1 for c in fresh for r in c.get('replies', []) if not r.get('sent'))
    rnd = st.get('ack_seq', 0)
    out = ['Review round %d: %d new comment%s, %d new repl%s (%s, %s)' % (
        rnd, ncom, '' if ncom == 1 else 's', nrep, 'y' if nrep == 1 else 'ies', desc, st['cwd'])]
    cur = None
    for c in fresh:
        if c['file'] != cur:
            cur = c['file']
            f = next((f for f in st.get('files', []) if f['path'] == cur), None)
            out.append('\n### %s%s' % (cur, ' (renamed from %s)' % f['old_path'] if f and f['status'] == 'renamed' else ''))
        tag = ' [OUTDATED: hunk changed since comment]' if c.get('outdated') else ''
        if not c.get('sent'):
            out.append('- [%s] %s%s%s' % (c['id'], fmt_where(c), fmt_quote(c), tag))
            out.append(indent(c.get('text', '')))
            c['sent'] = True
        else:
            who = ' (Claude asked)' if c.get('by') == 'claude' else ''
            out.append('- [%s] %s%s, earlier thread%s "%s"' % (c['id'], fmt_where(c), tag, who, (c.get('text') or '')[:60].replace('\n', ' ')))
        for r in c.get('replies', []):
            if not r.get('sent'):
                out.append(indent('> reply: ' + r.get('text', '').strip().replace('\n', '\n  > ')))
                r['sent'] = True
        if c.get('resolved'):
            out.append('  (resolved by the user)' if c.get('by') == 'claude' else '  (resolved)')
            c['resolved_sent'] = True
    return '\n'.join(out)


def wait_for_round(spath, key, desc):
    """Block until the user clicks Send (or Finish) or the tab closes; return the text to print."""
    while True:
        st = load_json(spath, {})
        if st.get('send_seq', 0) != st.get('ack_seq', 0):
            with locked_state(spath) as st:
                st['ack_seq'] = st['send_seq']
                only = None if st.pop('send_all', False) else set(st.pop('send_ids', []))
                out = report(st, desc, only)
                if st.get('finish_requested'):
                    st['status'] = 'finished'
                    out += '\n\nReview finished. The user closed it, so there are no more rounds.'
            return out
        if not run_info(key):
            n = len(st.get('comments', {}))
            if st.get('status') == 'finished':
                return 'Review finished (%s).' % desc
            return 'Review left pending with %d comment%s (%s). Re-run the same command to resume.' % (n, '' if n == 1 else 's', desc)
        time.sleep(0.5)


# ---------------------------------------------------------------- CLI

def find_state_for(cid):
    for fn in os.listdir(STATE_DIR):
        if fn.endswith('.json') and fn != 'prefs.json':
            p = os.path.join(STATE_DIR, fn)
            if cid in (load_json(p, {}).get('comments') or {}):
                return p
    die('No comment with id %s in any review.' % cid)


def cmd_reply(argv):
    if len(argv) < 2:
        die('Usage: clankback.py reply <id> <text>')
    cid, text = argv[0], ' '.join(argv[1:]).strip()
    with locked_state(find_state_for(cid)) as st:
        st['comments'][cid].setdefault('replies', []).append(
            {'id': new_id(st), 'text': text, 'created': time.time(), 'by': 'claude', 'sent': True, 'seen': False})
    print('Replied on [%s].' % cid)


def cmd_resolve(argv, on=True):
    ids = [a for a in argv if re.fullmatch(r'[a-z0-9]{5}', a)]
    note = ' '.join(a for a in argv if a not in ids).strip()
    if not ids:
        die('Usage: clankback.py resolve <id>... ["note"]')
    for cid in ids:
        with locked_state(find_state_for(cid)) as st:
            c = st['comments'][cid]
            if note:
                c.setdefault('replies', []).append({'id': new_id(st), 'text': note, 'created': time.time(), 'by': 'claude', 'sent': True, 'seen': False})
            c['resolved'] = on
    print(('Resolved' if on else 'Reopened') + ' ' + ' '.join('[%s]' % c for c in ids) + ('.' if not note else ' with a note.'))


def new_id(st):
    while True:
        i = hashlib.sha1(os.urandom(8)).hexdigest()[:5]
        if i not in st['comments']:
            return i


def open_review(path=None):
    """The review currently open in the browser, with `path` made repo-relative."""
    live = live_reviews()
    if not live:
        die('No review is open in the browser.')
    st = live[0]
    root = (st.get('t') or {}).get('root')
    if path and root and os.path.isabs(path):
        path = os.path.relpath(path, root)
    if path and not any(f['path'] == path for f in st.get('files', [])):
        die('%s is not in the open review (%s).' % (path, st.get('target')))
    return st, path


def cmd_ask(argv):
    """Leave a Claude-authored thread at path:line for the user to answer."""
    if len(argv) < 2 or not re.search(r':\d+$', argv[0]):
        die('Usage: clankback.py ask <path>:<line> <text>')
    path, _, line = argv[0].rpartition(':')
    line, text = int(line), ' '.join(argv[1:]).strip()
    st, path = open_review(path)
    files = parse_diff(produce_diff(st['t'], st['cwd']))
    f = next((f for f in files if f['path'] == path), None)
    hit = None
    for side in (3, 2):  # prefer the new side; fall back to a deleted old line
        for h in f['hunks'] if f else []:
            for i, l in enumerate(h['lines']):
                if l[side] == line and not hit:
                    hit = (h, i, l)
        if hit:
            break
    if not hit:
        rng = ', '.join('%d-%d' % (h['new_start'], h['new_start'] + max(h['new_count'], 1) - 1) for h in (f['hunks'] if f else []))
        die('%s:%d is not inside a changed hunk. Hunks cover new lines: %s' % (path, line, rng or 'none (binary or mode-only)'))
    h, i, l = hit
    with locked_state(state_path(st['key'])) as st2:
        cid = new_id(st2)
        st2['comments'][cid] = {'id': cid, 'file': path, 'hunk': h['hash'], 'offset': i, 'end_offset': None,
                                'side': 'old' if l[0] == '-' else 'new', 'line': line, 'end_line': None, 'line_text': l[0] + l[1],
                                'text': text, 'created': time.time(), 'resolved': False, 'replies': [], 'by': 'claude', 'sent': True, 'seen': False}
    print('Asked at %s:%d [%s].' % (path, line, cid))


def cmd_show(argv):
    """Scroll the open review to a comment id or to path[:line]."""
    if len(argv) != 1:
        die('Usage: clankback.py show <id> | <path>[:<line>]')
    arg = argv[0]
    live = live_reviews()
    if re.fullmatch(r'[a-z0-9]{5}', arg) and any(arg in p['comments'] for p in live):
        spath, focus = find_state_for(arg), {'id': arg}
    else:
        path, _, line = arg.rpartition(':') if re.search(r':\d+$', arg) else (arg, '', '')
        st, path = open_review(path)
        spath, focus = state_path(st['key']), {'file': path, 'line': int(line) if line else None}
    focus['seq'] = time.time()
    with locked_state(spath) as st:
        st['focus'] = focus
    print('Showing %s in the review.' % arg)


def main(argv):
    os.makedirs(STATE_DIR, exist_ok=True)
    if argv[:1] == ['--serve']:
        return serve(argv[1])
    if argv[:1] == ['reply']:
        return cmd_reply(argv[1:])
    if argv[:1] == ['resolve']:
        return cmd_resolve(argv[1:])
    if argv[:1] == ['unresolve']:
        return cmd_resolve(argv[1:], False)
    if argv[:1] == ['show']:
        return cmd_show(argv[1:])
    if argv[:1] == ['ask']:
        return cmd_ask(argv[1:])
    opts, pos, paths = parse_args(argv)
    cwd = os.getcwd()
    if opts['resume']:
        pend = pending_reviews()
        if not pend:
            die('No pending review to resume.')
        cwd = pend[0]['cwd']
        opts, pos, paths = parse_args([a for a in pend[0]['argv'] if a != '--resume'])
    t = resolve_target(opts, pos, paths, cwd)
    key = hashlib.sha1('|'.join([t['root'] or '', t['mode'], json.dumps([t['paths'], t['ref'], t['pr'], t['files']])]).encode()).hexdigest()[:16]
    spath = state_path(key)
    if opts['discard']:
        info = run_info(key)
        if info:
            os.kill(info['pid'], signal.SIGTERM)
        for p in (spath, spath + '.lock', run_path(key)):
            if os.path.exists(p):
                os.remove(p)
        print('Discarded saved review for %s.' % t['desc'])
        return
    notes = []
    info = run_info(key)
    if not info:
        files = parse_diff(produce_diff(t, cwd))
        if not files:
            print('No changes to review (%s).' % t['desc'])
            return
        with locked_state(spath) as st:
            resumed = st.get('status') == 'pending' and st['comments']
            st.update(status='pending', cwd=cwd, argv=argv, target=t['desc'], key=key, t=t, created=st.get('created', time.time()))
        info = spawn_daemon(spath, key)
        open_browser(info['url'])
        if resumed:
            notes.append('Resumed pending review with %d comment%s.' % (len(st['comments']), '' if len(st['comments']) == 1 else 's'))
        for p in pending_reviews():
            if p.get('key') != key:
                notes.append('Also pending: %s in %s (%d comments). `--resume` reopens the most recent.' % (p.get('target'), p.get('cwd'), len(p.get('comments', {}))))
                break
    print('\n'.join(notes + [wait_for_round(spath, key, t['desc'])]))


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        print('Interrupted. The review is still open in the browser; re-run to keep waiting.')
