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


if __name__ == '__main__':
    unittest.main()
