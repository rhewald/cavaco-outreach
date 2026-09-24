"""Short review transactions, no generation or delivery."""
from datetime import timezone
from outreach_recovery.contact_display import decorate
from psycopg2.extras import RealDictCursor
from outreach_recovery.generation import GenerationRepository


class ReviewRepository(GenerationRepository):
    def pending(self, offset=0, q='', kind='', mailbox='', sort='oldest', company=''):
        return self.queue(offset,q,kind,mailbox,sort,company)['drafts']

    def queue(self, offset=0, q='', kind='', mailbox='', sort='oldest', company=''):
        orders={'oldest':'activity_at ASC NULLS LAST,id', 'newest':'activity_at DESC NULLS LAST,id',
                'prospect':'lower(display_name),lower(recipient),id', 'company':'lower(company_name),lower(recipient),id'}
        if sort not in orders or kind not in ('','initial','reply','followup'):
            raise ValueError('Invalid queue filter')
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SELECT DISTINCT m.email FROM outreach_pilot.drafts d JOIN outreach_pilot.conversations c ON c.id=d.conversation_id JOIN outreach_pilot.mailboxes m ON m.id=c.mailbox_id WHERE d.state='pending_review' ORDER BY m.email")
            mailboxes=[row['email'] for row in cur.fetchall()]
            base="""WITH queue AS (
                SELECT d.id,d.version_snapshot,left(d.body,180) AS preview,
                  c.gmail_thread_id,m.email AS mailbox,
                  route.portal_id,route.contact_id,rc.linkedin_url,rc.phones,
                  coalesce(rc.display_name,'') AS display_name,coalesce(rc.company_name,'') AS company_name,
                  coalesce(e.payload->>'to',route.contact_email,h.sender,'') AS recipient,
                  coalesce(e.payload->>'subject',h.subject,'Subject not available') AS subject,
                  coalesce(j.updated_at,c.updated_at) AS activity_at,
                  CASE WHEN d.triggering_message_id IS NOT NULL THEN 'reply'
                       WHEN hist.message_count=0 THEN 'initial' ELSE 'followup' END AS kind,
                  hist.message_count, d.version_snapshot<>c.version_counter AS stale
                FROM outreach_pilot.drafts d
                JOIN outreach_pilot.conversations c ON c.id=d.conversation_id
                JOIN outreach_pilot.mailboxes m ON m.id=c.mailbox_id
                LEFT JOIN outreach_pilot.reply_jobs j ON j.triggering_message_id=d.triggering_message_id
                LEFT JOIN outreach_pilot.messages trigger ON trigger.id=d.triggering_message_id
                LEFT JOIN outreach_pilot.crm_inbound_headers h ON h.mailbox_id=trigger.mailbox_id AND h.gmail_message_id=trigger.gmail_message_id
                LEFT JOIN outreach_pilot.delivery_envelopes e ON e.draft_id=d.id
                LEFT JOIN outreach_pilot.crm_conversation_routes route ON route.conversation_id=c.id
                LEFT JOIN outreach_pilot.review_contacts rc ON rc.conversation_id=c.id
                CROSS JOIN LATERAL (SELECT count(*) AS message_count FROM outreach_pilot.messages msg WHERE msg.conversation_id=c.id AND msg.conversation_version<=d.version_snapshot) hist
                WHERE d.state='pending_review'
            ) """
            cur.execute(base+"SELECT DISTINCT company_name FROM queue WHERE company_name<>'' ORDER BY company_name")
            companies=[row['company_name'] for row in cur.fetchall()]
            where=" WHERE (%s='' OR strpos(lower(display_name||' '||company_name||' '||recipient||' '||subject),lower(%s))>0) AND (%s='' OR kind=%s) AND (%s='' OR mailbox=%s) AND (%s='' OR (%s='missing:' AND company_name='') OR company_name=%s)"
            params=(q,q,kind,kind,mailbox,mailbox,company,company,company[5:] if company.startswith('name:') else None)
            cur.execute(base+'SELECT count(*) AS total FROM queue'+where,params)
            total=cur.fetchone()['total']
            cur.execute(base+'SELECT * FROM queue'+where+' ORDER BY '+orders[sort]+' LIMIT 50 OFFSET %s',params+(offset,))
            drafts=cur.fetchall()
            for draft in drafts:
                decorate(draft)
                if draft['activity_at']: draft['activity_at']=draft['activity_at'].astimezone(timezone.utc)
            return dict(drafts=drafts,total=total,mailboxes=mailboxes,companies=companies)

    def detail(self,draft_id):
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("""SELECT d.*,c.version_counter,c.gmail_thread_id,m.email AS mailbox,
                j.context_snapshot FROM outreach_pilot.drafts d
                JOIN outreach_pilot.conversations c ON c.id=d.conversation_id
                JOIN outreach_pilot.mailboxes m ON m.id=c.mailbox_id
                LEFT JOIN outreach_pilot.reply_jobs j ON j.triggering_message_id=d.triggering_message_id
                WHERE d.id=%s""",(str(draft_id),))
            draft=cur.fetchone()
            if not draft:
                return None
            cur.execute("SELECT direction,body_text,conversation_version,received_at FROM outreach_pilot.messages WHERE conversation_id=%s ORDER BY conversation_version,id",
                        (draft['conversation_id'],))
            draft['history']=cur.fetchall()
            cur.execute("SELECT decision,reviewer,reason,created_at FROM outreach_pilot.review_events WHERE draft_id=%s",(str(draft_id),))
            draft['audit']=cur.fetchone()
            cur.execute('SELECT id,payload FROM outreach_pilot.delivery_envelopes WHERE draft_id=%s',(str(draft_id),))
            draft['delivery_envelope']=cur.fetchone()
            cur.execute('SELECT rc.*,route.portal_id,route.contact_id,route.contact_email AS recipient FROM outreach_pilot.conversations c LEFT JOIN outreach_pilot.review_contacts rc ON rc.conversation_id=c.id LEFT JOIN outreach_pilot.crm_conversation_routes route ON route.conversation_id=c.id WHERE c.id=%s',(draft['conversation_id'],))
            draft['contact']=decorate(cur.fetchone())
            return draft

    def decide(self,draft_id,decision,reviewer,reason='',envelope_id=None):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT outreach_pilot.review_draft(%s,%s,%s,%s,%s)",
                        (str(draft_id),decision,reviewer,reason,str(envelope_id) if envelope_id else None))
            return cur.fetchone()[0]
