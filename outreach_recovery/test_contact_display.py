import unittest
from outreach_recovery.contact_display import linkedin_url,contact_fields,decorate
class ContactDisplayTests(unittest.TestCase):
    def test_profile_links_are_safe(self):
        self.assertEqual(linkedin_url('linkedin.com/in/rui?tracking=x'),'https://www.linkedin.com/in/rui')
        for value in ['javascript:alert(1)','https://linkedin.com.evil.com/in/x','https://evil.com/in/x','https://user@linkedin.com/in/x','https://linkedin.com/company/x']:
            self.assertEqual(linkedin_url(value),'')
    def test_fields_and_missing_data(self):
        link,phones=contact_fields({'phone':'123','mobilephone':'123','sl_phone_numbers':'456;789','sl_contact_linkedin_url':'https://linkedin.com/in/example'})
        self.assertEqual(len(phones),2)
        self.assertTrue(link)
        self.assertEqual(contact_fields({}),('',[]))
        self.assertEqual(decorate({'portal_id':'javascript:x','contact_id':'123'})['hubspot_url'],'')
