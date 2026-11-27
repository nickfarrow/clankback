#!/usr/bin/env python3
"""clankback: review a local diff in the browser, GitHub-PR style. Stdlib + git only."""
import sys, os, re, json, base64, hashlib, subprocess, time, threading, html, collections, fcntl, signal, datetime
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


def diff_files(t, cwd):
    """The parsed diff, with two loose files named as the user typed them (git drops their leading slash)."""
    files = parse_diff(produce_diff(t, cwd))
    if t['mode'] == 'files' and files:
        files[0]['old_path'], files[0]['path'] = t['names']
    return files


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
        with open('/proc/%d/status' % info['pid']) as fh:  # a zombie still answers the signal
            if 'State:\tZ' in fh.read(400):
                return None
    except OSError:
        return None
    return info


def attach_comments(files, comments):
    """Recompute line numbers from hunk offsets; a comment whose hunk changed follows its line's text, or is flagged outdated."""
    by_key = {(f['path'], h['hash']): h for f in files for h in f['hunks']}
    by_path = {f['path']: f['hunks'] for f in files}
    for c in comments.values():
        h = by_key.get((c['file'], c['hunk']))
        if h is None and c.get('line_text'):
            lt = c['line_text']
            # A line the pull request added may be plain context in a local diff, so a GitHub thread matches on text alone; a removed line must still be removed.
            same = (lambda l: l[1] == lt[1:] and (l[0] == '-') == (lt[0] == '-')) if c.get('gh') else (lambda l: l[0] + l[1] == lt)
            best = None  # the same text nearest the old line number
            for nh in by_path.get(c['file'], []):
                for i, l in enumerate(nh['lines']):
                    if same(l):
                        d = abs((l[3] if l[3] is not None else l[2]) - (c.get('line') or 0))
                        if best is None or d < best[0]:
                            best = (d, nh, i)
            if best:
                _, h, i = best
                if c.get('end_offset') is not None:
                    c['end_offset'] = max(0, min(i + c['end_offset'] - c['offset'], len(h['lines']) - 1))
                c['hunk'], c['offset'] = h['hash'], i
        c['outdated'] = h is None
        if h is None:
            continue
        def ln(off):
            l = h['lines'][min(off, len(h['lines']) - 1)]
            return l[3] if l[3] is not None else l[2]
        c['line'] = ln(c['offset'])
        if c.get('end_offset') is not None:
            c['end_line'] = ln(c['end_offset'])


SERVER_OWNED = ('sent', 'resolved_sent', 'gh', 'gh_id', 'draft')


def merge_comment(old, c):
    """The browser's copy of a thread, merged over the saved one.

    The browser may be up to one poll behind, so it can lack replies the CLI or a sync added since,
    and it never authors anything but the user's own text. Everything else is kept from the saved copy.
    """
    def keep(o, n):
        for k in SERVER_OWNED:
            if k in o:
                n[k] = o[k]
            else:
                n.pop(k, None)
        if 'seen' in o:
            n['seen'] = o['seen']
        if o.get('by') not in (None, 'you'):
            n['by'], n['text'] = o['by'], o.get('text', n.get('text'))
        return n
    if not old:
        c['sent'] = False
        return c
    keep(old, c)
    oldr = {r['id']: r for r in old.get('replies', [])}
    mine = {r['id'] for r in c.get('replies', [])}
    for r in c.get('replies', []):
        if r['id'] in oldr:
            keep(oldr[r['id']], r)
        else:
            r['sent'] = False
    c['replies'] = sorted(c.get('replies', []) + [r for r in old.get('replies', []) if r['id'] not in mine],
                          key=lambda r: r.get('created') or 0)
    return c


# ---------------------------------------------------------------- GitHub

GH_POLL = 30  # seconds between syncs while the page is open; about 120 of the 5000 requests an hour a user token gets

def gh_json(args, cwd, stdin=None):
    """Run gh and parse its JSON output. Returns (data, error) and never dies, because the daemon calls it."""
    try:
        r = run(['gh'] + args, cwd, input=stdin)
    except OSError:
        return None, 'gh is not installed'
    if r.returncode != 0:  # gh also prints hints on stderr, so only a failing exit counts
        try:
            body = json.loads(r.stdout)
            msg = '; '.join([body.get('message', '')] + [e.get('message', '') if isinstance(e, dict) else str(e) for e in body.get('errors', [])])
        except (ValueError, AttributeError):
            msg = ''
        err = [l for l in r.stderr.strip().split('\n') if l.strip() and not l.startswith('hint:')]
        return None, (msg.strip('; ') or (err[-1] if err else 'gh failed')).strip()
    try:
        return (json.loads(r.stdout) if r.stdout.strip() else None), None
    except ValueError:
        return None, 'gh printed something that is not JSON'


GH_THREADS = '''query($owner:String!,$repo:String!,$n:Int!,$endCursor:String){viewer{login}
repository(owner:$owner,name:$repo){pullRequest(number:$n){headRefOid
reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor}
nodes{id isResolved isOutdated path line originalLine diffSide
comments(first:100){nodes{databaseId author{__typename login} authorAssociation body createdAt diffHunk url}}}}}}}'''
GH_WRITERS = ('OWNER', 'MEMBER', 'COLLABORATOR')  # people with write access; only they start a round unasked


def gh_ts(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def gh_merge(st, nodes, login, backlog):
    """Fold GitHub review threads into the review state.

    Returns the ids of threads whose new comments should go to Claude now: from someone with write access,
    not the user, not a bot, on an open thread, and not part of the backlog found when the PR was first linked.
    """
    by_thread = {c['gh']['thread']: c for c in st['comments'].values() if c.get('gh', {}).get('thread')}
    by_top = {c['gh']['top']: c for c in st['comments'].values() if c.get('gh', {}).get('top')}
    wake = []
    for n in nodes:
        msgs = n['comments']['nodes']
        if not msgs:
            continue
        top = msgs[0]
        c = by_thread.get(n['id']) or by_top.get(top['databaseId'])
        if c is None:
            c = {'id': new_id(st), 'file': n['path'], 'hunk': None, 'offset': 0, 'end_offset': None,
                 'side': 'old' if n.get('diffSide') == 'LEFT' else 'new', 'line': n.get('line') or n.get('originalLine'), 'end_line': None,
                 'line_text': (top.get('diffHunk') or '').rstrip('\n').split('\n')[-1] if n.get('line') or n.get('originalLine') else '',
                 'text': None, 'created': gh_ts(top['createdAt']), 'resolved': n['isResolved'], 'replies': [], 'sent': True}
            st['comments'][c['id']] = c
        c['gh'] = {'thread': n['id'], 'top': top['databaseId'], 'url': top['url'], 'resolved': n['isResolved'], 'outdated': n['isOutdated']}
        have = {x.get('gh_id'): x for x in [c] + c['replies'] if x.get('gh_id')}
        for m in msgs:
            a = m.get('author') or {'login': 'ghost', '__typename': 'User'}
            by = 'you' if a['login'] == login else a['login'] + ('[bot]' if a['__typename'] == 'Bot' else '')
            if m['databaseId'] in have:
                have[m['databaseId']]['text'] = m['body']  # edited on GitHub
                continue
            quiet = by == 'you' or n['isResolved']  # nothing for Claude to answer
            x = c if c['text'] is None else {'id': new_id(st), 'created': gh_ts(m['createdAt'])}
            x.update(text=m['body'], by=by, gh_id=m['databaseId'], sent=quiet)
            if quiet:
                x['quiet'] = True  # sent only so no round picks it up; Claude never had it
            if x is not c:
                c['replies'].append(x)
            if not (quiet or backlog or by.endswith('[bot]') or m.get('authorAssociation') not in GH_WRITERS) and c['id'] not in wake:
                wake.append(c['id'])
    return wake


def gh_sync(spath, gh, files):
    """Pull the pull request's review threads into the review, and start a round for new ones from collaborators."""
    owner, repo = gh['repo'].split('/')
    pages, err = gh_json(['api', 'graphql', '--paginate', '--slurp', '-f', 'owner=' + owner, '-f', 'repo=' + repo,
                          '-F', 'n=%d' % gh['number'], '-f', 'query=' + GH_THREADS], None)
    with locked_state(spath) as st:
        g = st.get('gh')
        if not g or g.get('number') != gh['number']:
            return
        if err:
            g.setdefault('error_since', time.time())
            g['error'] = err
            return
        pages = pages if isinstance(pages, list) else [pages]
        pr = pages[0]['data']['repository']['pullRequest']
        nodes = [n for p in pages for n in p['data']['repository']['pullRequest']['reviewThreads']['nodes']]
        wake = gh_merge(st, nodes, pages[0]['data']['viewer']['login'], backlog=not g.get('synced'))
        attach_comments(files, st['comments'])
        g.update(login=pages[0]['data']['viewer']['login'], head=pr['headRefOid'], synced=time.time())
        g.pop('error', None), g.pop('error_since', None)
        if wake:  # as if the user had clicked each thread's Send button
            st['send_seq'] = st.get('send_seq', 0) + 1
            st.setdefault('send_ids', []).extend(wake)


def gh_post_request(c, gh, files, head):
    """The API path and body that put a comment on GitHub for thread c, or (None, reason).

    A thread that is already on GitHub gets a reply. A local thread becomes a new line comment, which only a
    review of the pull request itself can place, because only there are the page's lines GitHub's lines.
    """
    if (c.get('gh') or {}).get('top'):
        return 'repos/%s/pulls/%d/comments/%d/replies' % (gh['repo'], gh['number'], c['gh']['top']), {}
    if head is None:
        return None, 'A new line comment can only be posted from a review of the pull request itself: /clankback %d' % gh['number']
    h = next((h for f in files if f['path'] == c['file'] for h in f['hunks'] if h['hash'] == c.get('hunk')), None)
    if h is None or c.get('outdated'):
        return None, 'This line is no longer in the diff, so GitHub has nowhere to put the comment.'
    def end(off):
        l = h['lines'][min(off, len(h['lines']) - 1)]
        left = l[0] == '-' or (l[0] == ' ' and c.get('side') == 'old')
        return ('LEFT', l[2]) if left else ('RIGHT', l[3])
    a = end(c['offset'])
    b = end(c['end_offset']) if c.get('end_offset') is not None else a
    if a[0] == b[0] and a[1] > b[1]:  # a range dragged upwards
        a, b = b, a
    body = {'commit_id': head, 'path': c['file'], 'side': b[0], 'line': b[1]}
    if a != b:
        body.update(start_side=a[0], start_line=a[1])
    return 'repos/%s/pulls/%d/comments' % (gh['repo'], gh['number']), body


def gh_find_pr(t):
    """The pull request a review belongs to: the one named, else the open one for the current branch.
    Returns (link, error); (None, None) means there is none."""
    if t['mode'] not in ('pr', 'working', 'staged', 'ref') or not t['root']:
        return None, None
    if 'github.com' not in run(GIT + ['remote', '-v'], t['root']).stdout:  # local check, so a repo with no GitHub remote costs no network call
        return None, None
    d, err = gh_json(['pr', 'view'] + ([str(t['pr'])] if t['pr'] else []) + ['--json', 'number,url,headRefOid,state'], t['root'])
    if err:
        return None, (None if 'no pull requests found' in err else err)
    m = re.match(r'https://github\.com/([^/]+/[^/]+)/pull/\d+', d.get('url') or '')
    if not m or (not t['pr'] and d.get('state') != 'OPEN'):
        return None, None
    return {'repo': m.group(1), 'number': d['number'], 'url': d['url'], 'head': d['headRefOid']}, None


# ---------------------------------------------------------------- server (daemon)

class Review:
    def __init__(self, spath, target, cwd):
        self.spath, self.target, self.cwd = spath, target, cwd
        self.new_side = new_side_reader(target)
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.last_seen = self.bye_at = None
        self.files, self.html, self.cache, self.fp, self.port = [], b'', {}, None, None
        self.gh_lookup, self.gh_next, self.gh_busy = True, 0, False  # look the PR up at start and on each page load
        self.posting = threading.Lock()  # one post at a time, so a double click cannot post twice

    def gh_post(self, cid, text):
        """Post the user's draft on thread cid to GitHub, as the user. Called only from the page's Post button."""
        if not text.strip():
            return {'error': 'The draft is empty.'}
        if not self.posting.acquire(blocking=False):
            return {'error': 'Already posting.'}
        try:
            st = load_json(self.spath, {})
            c, gh = st.get('comments', {}).get(cid), st.get('gh') or {}
            if not c or not c.get('draft') or not gh.get('number'):
                return {'error': 'Nothing to post: the draft or the pull request link is gone.'}
            path, body = gh_post_request(c, gh, self.files, self.target.get('head_sha') if self.target['mode'] == 'pr' else None)
            if path is None:
                return {'error': body}
            body['body'] = text
            resp, err = gh_json(['api', '-X', 'POST', path, '--input', '-'], self.target['root'], stdin=json.dumps(body))
            if err:
                return {'error': 'GitHub refused it: ' + err}
            with locked_state(self.spath) as st:
                c = st['comments'][cid]
                c.setdefault('replies', []).append({'id': new_id(st), 'text': text, 'created': time.time(), 'by': 'you', 'gh_id': resp['id'],
                                                    'sent': True, 'quiet': True, 'drafted': (c.get('draft') or {}).get('claude')})
                c.pop('draft', None)
                if not c.get('gh'):  # the next sync matches the new GitHub thread to this one by its first comment
                    c['gh'] = {'thread': None, 'top': resp['id'], 'url': resp.get('html_url'), 'resolved': False, 'outdated': False}
            return {'ok': True, 'url': resp.get('html_url')}
        finally:
            self.posting.release()

    def gh_tick(self):
        """Link the review to its pull request. Runs in its own thread, so a slow network never stalls the page."""
        try:
            if self.gh_lookup:
                link, err = gh_find_pr(self.target)
                with locked_state(self.spath) as st:
                    old = st.get('gh') or {}
                    if err:  # unknown, not absent: keep what we had and try again next tick
                        st['gh'] = dict(old, error=err, error_since=old.get('error_since') or time.time())
                        return
                    self.gh_lookup = False
                    if link:
                        st['gh'] = dict({k: v for k, v in old.items() if old.get('number') == link['number'] and k not in ('error', 'error_since')}, **link)
                    else:
                        st.pop('gh', None)
            gh = load_json(self.spath, {}).get('gh') or {}
            if gh.get('number'):
                gh_sync(self.spath, gh, self.files)
        finally:
            self.gh_busy = False

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
        if self.target['mode'] == 'pr':  # a push to the pull request changes its diff
            out.append(((load_json(self.spath, {}).get('gh') or {}).get('head'), 0, 0))
        return tuple(out)

    def refresh(self):
        """Re-run the diff so a browser (re)load shows the current working tree."""
        files = diff_files(self.target, self.cwd)
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
            rv.last_seen = time.monotonic()
            if u.path == '/':
                rv.gh_lookup, rv.gh_next = True, 0  # a reload also picks up a PR opened since
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
                            'focus': st.get('focus'), 'diff_rev': st.get('diff_rev'), 'gh': st.get('gh')})
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
            rv.last_seen = time.monotonic()
            if p == '/beat':
                rv.bye_at = None
            elif p == '/bye':
                rv.bye_at = time.monotonic()
            elif p == '/prefs':
                prefs = load_json(os.path.join(STATE_DIR, 'prefs.json'), {})
                prefs.update(body)
                save_json(os.path.join(STATE_DIR, 'prefs.json'), prefs)
            elif p == '/gh/post':
                return self._json(rv.gh_post(body['id'], body.get('text') or ''))
            elif p in ('/comment', '/viewed', '/send', '/finish', '/seen', '/clear', '/draft'):
                with locked_state(rv.spath) as st:
                    if p == '/comment':
                        c = body['comment']
                        st['comments'][c['id']] = merge_comment(st['comments'].get(c['id'], {}), c)
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
                    elif p == '/clear':
                        st['comments'] = {}
                        (st.get('gh') or {}).pop('synced', None)  # threads the next sync brings back are backlog, not news
                    elif p == '/draft':  # the user writes, edits or discards a thread's GitHub draft; ask hands it to the clanker
                        c = st['comments'][body['id']]
                        if body.get('text') is None:
                            c.pop('draft', None)
                        else:
                            d = c.setdefault('draft', {'claude': None, 'created': time.time(), 'sent': True})
                            d['text'] = body['text']
                            if body.get('ask'):
                                d['sent'] = False
                                st['send_seq'] = st.get('send_seq', 0) + 1
                                st.setdefault('send_ids', []).append(c['id'])
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
            'cwd': state['cwd'], 'focus': state.get('focus'), 'diff_rev': state.get('diff_rev'), 'gh': state.get('gh'), 'mode': target['mode'], 'expandable': target['mode'] != 'pr' or bool(target.get('head_sha'))}
    js_data = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    page = rd('viewer.html')
    with open(os.path.join(HERE, 'favicon.ico'), 'rb') as fh:
        icon = base64.b64encode(fh.read()).decode()
    for k, v in (('{{CSS}}', rd('viewer.css')), ('{{JS}}', rd('viewer.js')), ('{{DATA}}', js_data), ('{{TITLE}}', html.escape(os.path.basename(target.get('root') or state['cwd']))), ('{{ICON}}', icon)):
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
    try:  # a port derived from the review key, so a tab left open outlives a daemon restart and reconnects
        srv = ThreadingHTTPServer(('127.0.0.1', 30000 + int(key, 16) % 30000), make_handler(rv))
    except OSError:
        srv = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(rv))
    rv.port = srv.server_address[1]
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    save_json(run_path(key), {'pid': os.getpid(), 'url': 'http://127.0.0.1:%d/' % srv.server_address[1]})
    start = time.monotonic()  # monotonic: a suspend must not read as a lost tab
    try:
        while not rv.finished.wait(1):
            now = time.monotonic()
            if now >= rv.gh_next and not rv.gh_busy:
                rv.gh_busy, rv.gh_next = True, now + GH_POLL
                threading.Thread(target=rv.gh_tick, daemon=True).start()
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


def gh_author(x):
    """The GitHub login of someone other than the user or Claude who wrote x, else None."""
    return x['by'] if x.get('gh_id') and x.get('by') not in (None, 'you', 'claude') else None


def fmt_tag(c):
    if not c.get('outdated'):
        return ' [outdated on GitHub]' if c.get('gh', {}).get('outdated') else ''
    return ' [not in this diff]' if c.get('gh') else ' [OUTDATED: hunk changed since comment]'


def report(st, desc, only=None):
    """Print what the user sent this round (unsent comments and unsent replies) and mark it sent."""
    comments = [c for c in st['comments'].values() if only is None or c['id'] in only]
    order = {f['path']: i for i, f in enumerate(st.get('files', []))}
    fresh = [c for c in comments if not c.get('sent') or any(not r.get('sent') for r in c.get('replies', []))
             or (c.get('by') == 'claude' and c.get('resolved') and not c.get('resolved_sent'))
             or (c.get('draft') and not c['draft'].get('sent'))]
    fresh.sort(key=lambda c: (order.get(c['file'], 10 ** 6), c.get('line') or 0))
    ncom = sum(1 for c in fresh if not c.get('sent'))
    nrep = sum(1 for c in fresh for r in c.get('replies', []) if not r.get('sent'))
    rnd = st.get('ack_seq', 0)
    out = ['Review round %d: %d new comment%s, %d new repl%s (%s, %s)' % (
        rnd, ncom, '' if ncom == 1 else 's', nrep, 'y' if nrep == 1 else 'ies', desc, st['cwd'])]
    gh = st.get('gh') or {}
    if gh.get('number'):
        out.append('Pull request %s, head %s' % (gh['url'], gh.get('head')))
    cur = None
    for c in fresh:
        if c['file'] != cur:
            cur = c['file']
            f = next((f for f in st.get('files', []) if f['path'] == cur), None)
            out.append('\n### %s%s' % (cur, ' (renamed from %s)' % f['old_path'] if f and f['status'] == 'renamed' else ''))
        tag = fmt_tag(c)
        if not c.get('sent') and gh_author(c):
            out.append('- [%s] %s%s%s, %s on GitHub:' % (c['id'], fmt_where(c), fmt_quote(c), tag, c['by']))
            out.append(indent(c.get('text', '')))
            c['sent'] = True
        elif not c.get('sent'):
            if c.get('kind') == 'rewrite':
                out.append('- [%s] %s%s REWRITE the selected text:' % (c['id'], fmt_where(c), tag))
                out.append(indent(c.get('selected', ''), '  | '))
                out.append('  to (the user\'s draft; polish it):')
                out.append(indent(c.get('text', ''), '  | '))
            elif c.get('selected'):
                out.append('- [%s] %s%s on the selected text:' % (c['id'], fmt_where(c), tag))
                out.append(indent(c['selected'], '  | '))
                out.append(indent(c.get('text', '')))
            else:
                out.append('- [%s] %s%s%s' % (c['id'], fmt_where(c), fmt_quote(c), tag))
                out.append(indent(c.get('text', '')))
            c['sent'] = True
        else:
            who = ' (Claude asked)' if c.get('by') == 'claude' else ''
            out.append('- [%s] %s%s, earlier thread%s "%s"' % (c['id'], fmt_where(c), tag, who, (c.get('text') or '')[:60].replace('\n', ' ')))
            # The writer may have lost its own answer to compaction or a new session, and the user's reply only makes sense against it.
            said = None
            for m in [c] + c.get('replies', []):
                if not m.get('sent'):
                    break
                if m.get('by') == 'claude':
                    said = m
            if said:
                out.append(indent('Claude had said: ' + (said.get('text') or '').strip()))
        for r in c.get('replies', []):
            if not r.get('sent') or (r.get('quiet') and not r.get('shown')):  # a quiet one (the user's own post, say) is context, shown once
                who = '%s on GitHub' % gh_author(r) if gh_author(r) else 'you on GitHub' if r.get('gh_id') else 'reply'
                out.append(indent('> %s: ' % who + r.get('text', '').strip().replace('\n', '\n  > ')))
                r['sent'] = r['shown'] = True
        if c.get('resolved'):
            out.append('  (resolved by the user)' if c.get('by') == 'claude' else '  (resolved)')
            c['resolved_sent'] = True
        d = c.get('draft')
        if d and not d.get('sent'):
            if (d.get('text') or '').strip():
                out.append('  DRAFT a GitHub comment for this thread (the user\'s draft; polish it):')
                out.append(indent(d['text'], '  | '))
            else:
                out.append('  DRAFT a GitHub comment for this thread.')
            d['sent'] = True
    return '\n'.join(out)


def wait_for_round(spath, key, desc):
    """Block until the user clicks Send (or Finish) or the tab closes; return the text to print."""
    while True:
        st = load_json(spath, {})
        if st.get('send_seq', 0) != st.get('ack_seq', 0):
            with locked_state(spath) as st:
                st['ack_seq'] = st['send_seq']
                ids = st.pop('send_ids', [])  # always consumed, or a stale list would narrow a later round
                only = None if st.pop('send_all', False) else set(ids)
                out = report(st, desc, only)
                if st.pop('finish_requested', False):  # consumed here, so a reopened review does not finish itself on the next Send
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


def cmd_draft(argv):
    if len(argv) < 2:
        die('Usage: clankback.py draft <id> <text>')
    cid, text = argv[0], ' '.join(argv[1:]).strip()
    with locked_state(find_state_for(cid)) as st:
        st['comments'][cid]['draft'] = {'text': text, 'claude': text, 'created': time.time(), 'sent': True}
    print('Drafted on [%s]. The user edits and posts it from the page.' % cid)


def cmd_thread(argv):
    """Print whole threads, for a writer that no longer has their earlier messages in context."""
    if not argv:
        die('Usage: clankback.py thread <id>...')
    out = []
    for cid in argv:
        c = load_json(find_state_for(cid), {})['comments'][cid]
        tags = ''.join(' (%s)' % k for k in ('resolved', 'outdated') if c.get(k))
        out.append('[%s] %s %s%s%s' % (cid, c['file'], fmt_where(c), fmt_quote(c), tags))
        if c.get('selected'):
            out.append(indent(c['selected'], '  | '))
        for m in [c] + c.get('replies', []):
            who = 'Claude' if m.get('by') == 'claude' else '%s on GitHub' % gh_author(m) if gh_author(m) else 'user'
            out.append(indent(who + ': ' + (m.get('text') or '').strip()))
    print('\n'.join(out))


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
    files = diff_files(st['t'], st['cwd'])
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
    if argv[:1] == ['thread']:
        return cmd_thread(argv[1:])
    if argv[:1] == ['draft']:
        return cmd_draft(argv[1:])
    if argv[:1] == ['ask']:
        return cmd_ask(argv[1:])
    opts, pos, paths = parse_args(argv)
    cwd = os.getcwd()
    if opts['resume'] and not (pos or paths):  # bare --resume: the latest pending review here, else anywhere
        pend = pending_reviews()
        pend = [p for p in pend if p.get('cwd') == cwd] or pend
        if not pend:
            die('No pending review to resume. Re-running a review\'s own command reopens it, finished or not.')
        cwd = pend[0]['cwd']
        opts, pos, paths = parse_args([a for a in pend[0]['argv'] if a != '--resume'])
    t = resolve_target(opts, pos, paths, cwd)
    key = hashlib.sha1('|'.join([t['root'] or '', t['mode'], json.dumps([t['paths'], t['ref'], t['pr'], t['files']])]).encode()).hexdigest()[:16]
    spath = state_path(key)
    if opts['resume'] and not os.path.exists(spath):  # --resume <target>: only an existing review, pending or finished
        die('No saved review for %s. Run without --resume to start one.' % t['desc'])
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
            resumed = st.get('status') if st.get('comments') else None
            st.update(status='pending', finish_requested=False, cwd=cwd, argv=[a for a in argv if a != '--resume'], target=t['desc'], key=key, t=t, created=st.get('created', time.time()))
        info = spawn_daemon(spath, key)
        open_browser(info['url'])
        if resumed:
            notes.append('%s review with %d comment%s.' % ('Reopened finished' if resumed == 'finished' else 'Resumed pending', len(st['comments']), '' if len(st['comments']) == 1 else 's'))
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
