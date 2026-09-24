import base64
import unittest
from email.message import EmailMessage
from unittest.mock import Mock, patch
from outreach_recovery.gmail_inbound import parse_reply, verify_accepted

class InboundTests(unittest.TestCase):
    def data(self,html=False):
        m=EmailMessage()
        m['From']='Rui <rui@cavaco.ai>';m['To']='sdr@cavaco.ai'
        m['Message-ID']='<reply@example.com>';m['In-Reply-To']='<rewritten@gmail.com>'
        m['References']='<rewritten@gmail.com>'
        m.set_content('received',subtype='html' if html else 'plain')
        return dict(id='reply1',threadId='thread1',labelIds=['INBOX'],internalDate='1790000000000',raw=base64.urlsafe_b64encode(m.as_bytes()).decode())
    def parse(self,d):
        return parse_reply(d,expected_sender='rui@cavaco.ai',mailbox_email='sdr@cavaco.ai',thread_id='thread1')
    def test_reply_headers_and_plain_text(self):
        p=self.parse(self.data());self.assertEqual(p['body'].strip(),'received')
        self.assertEqual(p['reply_ids'],['<rewritten@gmail.com>'])
    def test_wrong_thread_and_sent_rejected(self):
        for values in ({'threadId':'other'},{'labelIds':['SENT']}):
            d=self.data();d.update(values)
            with self.assertRaises(ValueError):self.parse(d)
    def test_sender_spoof_and_html_only_rejected(self):
        d=self.data();raw=base64.urlsafe_b64decode(d['raw']).replace(b'rui@cavaco.ai',b'other@example.com');d['raw']=base64.urlsafe_b64encode(raw).decode()
        with self.assertRaises(ValueError):self.parse(d)
        with self.assertRaises(ValueError):self.parse(self.data(True))
    def test_known_accepted_id_allows_only_message_id_change(self):
        adapter=Mock();expected=('sdr@cavaco.ai','rui@cavaco.ai','Test','<pinned@cavaco.ai>','','','Body\n')
        adapter._validate.return_value=expected
        data=dict(labelIds=['SENT'],threadId='thread1',raw='eA==')
        actual=list(expected);actual[3]='<rewritten@gmail.com>'
        with patch('outreach_recovery.gmail_inbound.fetch_message',return_value=data) as fetch, patch('outreach_recovery.gmail_inbound.fingerprint',return_value=tuple(actual)):
            self.assertEqual(verify_accepted(adapter,{},'known1','thread1'),actual[3])
            fetch.assert_called_once_with(adapter,'known1')
        actual[-1]='Wrong body'
        with patch('outreach_recovery.gmail_inbound.fetch_message',return_value=data),patch('outreach_recovery.gmail_inbound.fingerprint',return_value=tuple(actual)):
            with self.assertRaises(ValueError):verify_accepted(adapter,{},'known1','thread1')
