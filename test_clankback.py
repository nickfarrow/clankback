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


class Target(unittest.TestCase):
    def test_pr_number_with_hash(self):
        here = cb.os.path.dirname(cb.os.path.abspath(cb.__file__))
        opts = {'staged': False}
        for arg in ('676', '#676'):
            t = cb.resolve_target(opts, [arg], [], here)
            self.assertEqual((t['mode'], t['pr']), ('pr', 676))


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
        # the shape GitHub really returns for a line outside the diff (seen live on 2026-11-27)
        self.gh = Fake(1, stdout='{"message":"Validation Failed","errors":[{"resource":"PullRequestReviewComment","code":"custom","field":"pull_request_review_thread.line","message":"could not be resolved"}]}')
        self.assertEqual(cb.gh_json(['api', 'x'], '/r'), (None, 'Validation Failed; pull_request_review_thread.line could not be resolved'))


def gh_msg(i, login, body, assoc='COLLABORATOR', kind='User', hunk='@@ -1,2 +1,2 @@\n a\n+B'):
    return {'databaseId': i, 'author': {'__typename': kind, 'login': login}, 'authorAssociation': assoc, 'body': body,
            'createdAt': '2026-11-20T10:00:%02dZ' % (i % 60), 'diffHunk': hunk, 'url': 'https://github.com/o/r/pull/7#discussion_r%d' % i}


def gh_thread(tid, *msgs, resolved=False, line=2):
    return {'id': tid, 'isResolved': resolved, 'isOutdated': False, 'path': 'f.py', 'line': line, 'originalLine': line,
            'diffSide': 'RIGHT', 'comments': {'nodes': list(msgs)}}


class GhMerge(unittest.TestCase):
    def setUp(self):
        self.st = {'comments': {}}

    def merge(self, nodes, backlog=False):
        return cb.gh_merge(self.st, nodes, 'me', backlog)

    def only(self):
        (c,) = self.st['comments'].values()
        return c

    def test_new_thread_is_anchored_by_its_line_and_starts_a_round(self):
        wake = self.merge([gh_thread('T1', gh_msg(1, 'alice', 'why B?'))])
        c = self.only()
        self.assertEqual(wake, [c['id']])
        self.assertEqual((c['by'], c['text'], c['line'], c['line_text'], c['sent'], c['gh']['top']), ('alice', 'why B?', 2, '+B', False, 1))

    def test_backlog_is_unsent_but_does_not_start_a_round(self):
        self.assertEqual(self.merge([gh_thread('T1', gh_msg(1, 'alice', 'x'))], backlog=True), [])
        self.assertFalse(self.only()['sent'])

    def test_only_collaborators_start_a_round(self):
        nodes = [gh_thread('T1', gh_msg(1, 'stranger', 'x', assoc='NONE')), gh_thread('T2', gh_msg(2, 'copilot', 'x', kind='Bot')),
                 gh_thread('T3', gh_msg(3, 'me', 'note to self')), gh_thread('T4', gh_msg(4, 'alice', 'x'), resolved=True)]
        self.assertEqual(self.merge(nodes), [])
        by = {c['gh']['thread']: c for c in self.st['comments'].values()}
        self.assertEqual(by['T2']['by'], 'copilot[bot]')
        self.assertEqual((by['T3']['by'], by['T3']['sent']), ('you', True))
        self.assertTrue(by['T4']['sent'])
        self.assertFalse(by['T1']['sent'])

    def test_resync_adds_only_new_replies_and_takes_edits(self):
        self.merge([gh_thread('T1', gh_msg(1, 'alice', 'x'))], backlog=True)
        c = self.only()
        c['sent'] = True  # Claude has seen it
        wake = self.merge([gh_thread('T1', gh_msg(1, 'alice', 'x, edited'), gh_msg(2, 'bob', 'agree'))])
        self.assertEqual(wake, [c['id']])
        self.assertEqual(c['text'], 'x, edited')
        self.assertEqual([(r['by'], r['text'], r['sent']) for r in c['replies']], [('bob', 'agree', False)])
        self.assertEqual(self.merge([gh_thread('T1', gh_msg(1, 'alice', 'x, edited'), gh_msg(2, 'bob', 'agree'))]), [])
        self.assertEqual(len(c['replies']), 1)

    def test_a_thread_posted_from_here_is_matched_by_its_first_comment(self):
        self.st['comments']['local'] = {'id': 'local', 'file': 'f.py', 'text': 'mine', 'by': 'you', 'gh_id': 9, 'replies': [],
                                        'gh': {'top': 9}, 'sent': True}
        self.merge([gh_thread('T9', gh_msg(9, 'me', 'mine'), gh_msg(10, 'alice', 'ok'))])
        c = self.only()
        self.assertEqual((c['gh']['thread'], [r['by'] for r in c['replies']]), ('T9', ['alice']))

    def test_anchor_matches_text_when_the_line_is_context_locally(self):
        self.merge([gh_thread('T1', gh_msg(1, 'alice', 'x'))])
        files = [{'path': 'f.py', 'hunks': [{'hash': 'h1', 'lines': [[' ', 'a', 1, 1, False], [' ', 'B', 2, 2, False]]}]}]
        cb.attach_comments(files, self.st['comments'])
        c = self.only()
        self.assertEqual((c['outdated'], c['hunk'], c['offset']), (False, 'h1', 1))

    def test_round_labels_github_authors(self):
        self.merge([gh_thread('T1', gh_msg(1, 'alice', 'why?'), gh_msg(2, 'bob', 'agree'))])
        self.st.update(cwd='/r', gh={'number': 7, 'url': 'https://github.com/o/r/pull/7', 'head': 'abc'})
        out = cb.report(self.st, 'PR #7')
        self.assertIn('Pull request https://github.com/o/r/pull/7, head abc', out)
        self.assertIn('alice on GitHub:\n  why?', out)
        self.assertIn('> bob on GitHub: agree', out)


class GhConversation(unittest.TestCase):
    def test_comments_and_reviews_that_say_something_oldest_first(self):
        user = lambda l, t='User': {'login': l, '__typename': t}
        pr = {'comments': {'nodes': [
                  {'databaseId': 1, 'author': user('alice'), 'body': 'retry slower?', 'createdAt': '2026-01-02T00:00:00Z', 'url': 'u1'},
                  {'databaseId': 2, 'author': user('me'), 'body': 'done', 'createdAt': '2026-01-04T00:00:00Z', 'url': 'u2'}]},
              'reviews': {'nodes': [
                  {'databaseId': 3, 'author': user('bob'), 'body': '', 'state': 'APPROVED', 'submittedAt': '2026-01-03T00:00:00Z', 'url': 'u3'},
                  {'databaseId': 4, 'author': user('bob'), 'body': '', 'state': 'COMMENTED', 'submittedAt': '2026-01-01T00:00:00Z', 'url': 'u4'},
                  {'databaseId': 5, 'author': user('me'), 'body': 'draft', 'state': 'PENDING', 'submittedAt': '2026-01-01T00:00:00Z', 'url': 'u5'},
                  {'databaseId': 6, 'author': user('ci', 'Bot'), 'body': 'lgtm', 'state': 'COMMENTED', 'submittedAt': '2026-01-05T00:00:00Z', 'url': 'u6'}]}}
        out = cb.gh_conversation(pr, 'me')
        self.assertEqual([(m['gh_id'], m['by'], m['kind']) for m in out],
                         [(1, 'alice', 'comment'), (3, 'bob', 'review'), (2, 'you', 'comment'), (6, 'ci[bot]', 'review')])
        self.assertEqual(out[1]['state'], 'APPROVED')


class GhPost(unittest.TestCase):
    GH = {'repo': 'o/r', 'number': 7}
    FILES = [{'path': 'f.py', 'hunks': [{'hash': 'h1', 'lines': [
        [' ', 'a', 1, 1, False], ['-', 'b', 2, None, False], ['+', 'B', None, 2, False], ['+', 'C', None, 3, False]]}]}]

    def req(self, head='sha', **kw):
        c = {'file': 'f.py', 'hunk': 'h1', 'offset': 2, 'end_offset': None, 'side': 'new'}
        c.update(kw)
        return cb.gh_post_request(c, self.GH, self.FILES, head)

    def test_thread_on_github_gets_a_reply_in_any_mode(self):
        self.assertEqual(self.req(head=None, gh={'top': 55}), ('repos/o/r/pulls/7/comments/55/replies', {}))

    def test_added_line(self):
        self.assertEqual(self.req(), ('repos/o/r/pulls/7/comments', {'commit_id': 'sha', 'path': 'f.py', 'side': 'RIGHT', 'line': 2}))

    def test_removed_line_and_context_on_the_old_side(self):
        self.assertEqual(self.req(offset=1, side='old')[1], {'commit_id': 'sha', 'path': 'f.py', 'side': 'LEFT', 'line': 2})
        self.assertEqual(self.req(offset=0, side='old')[1]['line'], 1)

    def test_range_dragged_upwards(self):
        self.assertEqual(self.req(offset=3, end_offset=2)[1], {'commit_id': 'sha', 'path': 'f.py', 'side': 'RIGHT', 'line': 3, 'start_side': 'RIGHT', 'start_line': 2})

    def test_new_comment_needs_pr_mode_and_a_live_line(self):
        self.assertIsNone(self.req(head=None)[0])
        self.assertIsNone(self.req(outdated=True)[0])

    def test_asking_for_a_draft_puts_it_in_the_round(self):
        st = {'cwd': '/r', 'comments': {'aaaaa': thread(sent=True, draft={'text': 'lgtm but rename x', 'sent': False})}}
        out = cb.report(st, 'PR #7')
        self.assertIn("DRAFT a GitHub comment for this thread (the user's draft; polish it):\n  | lgtm but rename x", out)
        self.assertTrue(st['comments']['aaaaa']['draft']['sent'])

    def test_what_the_user_posted_is_shown_once_with_the_thread(self):
        posted = msg('r1', 'you', 2.0, text='Kept it.', gh_id=101, sent=True, quiet=True)
        st = {'cwd': '/r', 'comments': {'aaaaa': thread(sent=True, replies=[posted], draft={'text': '', 'sent': False})}}
        self.assertIn('> you on GitHub: Kept it.', cb.report(st, 'PR #7'))
        st['comments']['aaaaa']['draft']['sent'] = False
        self.assertNotIn('Kept it.', cb.report(st, 'PR #7'))


if __name__ == '__main__':
    unittest.main()
