"""Run with: python3 -m unittest test_clankback"""
import unittest

import clankback as cb


def thread(**kw):
    c = {'id': 'aaaaa', 'file': 'f.py', 'hunk': 'h', 'offset': 0, 'line': 3, 'text': 'why?', 'created': 1.0,
         'resolved': False, 'replies': []}
    c.update(kw)
    return c


def msg(i, by, t, **kw):
    r = {'id': i, 'text': '%s says' % by, 'created': t, 'by': by}
    r.update(kw)
    return r


class MergeComment(unittest.TestCase):
    def test_new_thread_from_browser_is_unsent(self):
        c = cb.merge_comment({}, thread(sent=True))
        self.assertFalse(c['sent'])

    def test_keeps_reply_added_after_the_browser_last_polled(self):
        old = thread(sent=True, replies=[msg('r1', 'claude', 2.0, sent=True, seen=False)])
        new = thread(sent=False, resolved=True)  # stale copy, with the user's resolve click
        c = cb.merge_comment(old, new)
        self.assertEqual([r['id'] for r in c['replies']], ['r1'])
        self.assertTrue(c['resolved'])
        self.assertTrue(c['sent'])

    def test_orders_new_user_reply_after_server_reply_by_time(self):
        old = thread(sent=True, replies=[msg('r1', 'claude', 2.0, sent=True)])
        new = thread(replies=[msg('r2', 'you', 3.0)])
        c = cb.merge_comment(old, new)
        self.assertEqual([(r['id'], r['sent']) for r in c['replies']], [('r1', True), ('r2', False)])

    def test_browser_cannot_change_text_it_did_not_write(self):
        old = thread(by='octocat', text='orig', gh_id=7, sent=True, replies=[msg('r1', 'claude', 2.0, text='a', sent=True)])
        new = thread(by='you', text='changed', replies=[msg('r1', 'you', 2.0, text='b')])
        c = cb.merge_comment(old, new)
        self.assertEqual((c['by'], c['text'], c['gh_id']), ('octocat', 'orig', 7))
        self.assertEqual((c['replies'][0]['by'], c['replies'][0]['text']), ('claude', 'a'))

    def test_server_owned_fields_come_from_the_saved_copy(self):
        old = thread(sent=True, resolved_sent=True, draft={'text': 'd', 'by': 'claude', 'sent': True})
        new = thread(sent=False, draft={'text': 'stale'}, gh={'thread': 'X'})
        c = cb.merge_comment(old, new)
        self.assertEqual(c['draft']['text'], 'd')
        self.assertTrue(c['resolved_sent'])
        self.assertNotIn('gh', c)


class Fake:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FindPr(unittest.TestCase):
    T = {'mode': 'working', 'root': '/r', 'pr': None}

    def setUp(self):
        self.real, self.calls, self.remote, self.gh = cb.run, [], 'origin\tgit@github.com:o/r.git (fetch)\n', Fake()
        cb.run = self.fake

    def tearDown(self):
        cb.run = self.real

    def fake(self, cmd, cwd=None, input=None):
        self.calls.append(cmd)
        return Fake(stdout=self.remote) if cmd[0] == 'git' else self.gh

    def test_no_github_remote_makes_no_gh_call(self):
        self.remote = 'origin\tgit@gitlab.com:o/r.git (fetch)\n'
        self.assertEqual(cb.gh_find_pr(self.T), (None, None))
        self.assertFalse([c for c in self.calls if c[0] == 'gh'])

    def test_open_pr_for_the_branch_links_to_the_base_repo(self):
        self.gh = Fake(stdout='{"number":7,"url":"https://github.com/up/proj/pull/7","headRefOid":"abc","state":"OPEN"}')
        link, err = cb.gh_find_pr(self.T)
        self.assertEqual((link['repo'], link['number'], link['head'], err), ('up/proj', 7, 'abc', None))

    def test_closed_pr_links_only_when_named(self):
        self.gh = Fake(stdout='{"number":7,"url":"https://github.com/o/r/pull/7","headRefOid":"abc","state":"MERGED"}')
        self.assertEqual(cb.gh_find_pr(self.T), (None, None))
        self.assertEqual(cb.gh_find_pr(dict(self.T, mode='pr', pr=7))[0]['number'], 7)

    def test_no_pr_is_absent_but_a_failure_is_an_error(self):
        self.gh = Fake(1, stderr='no pull requests found for branch "x"\n')
        self.assertEqual(cb.gh_find_pr(self.T), (None, None))
        self.gh = Fake(1, stderr='hint: try gh event listen\nerror connecting to api.github.com\n')
        self.assertEqual(cb.gh_find_pr(self.T), (None, 'error connecting to api.github.com'))

    def test_api_error_body_is_the_message(self):
        self.gh = Fake(1, stdout='{"message":"Validation Failed","errors":["line must be part of the diff"]}', stderr='gh: Validation Failed (HTTP 422)\n')
        self.assertEqual(cb.gh_json(['api', 'x'], '/r'), (None, 'Validation Failed; line must be part of the diff'))


if __name__ == '__main__':
    unittest.main()
