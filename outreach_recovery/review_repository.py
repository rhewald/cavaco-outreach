"""Short review transactions, no generation or delivery."""
from psycopg2.extras import RealDictCursor
from outreach_recovery.generation import GenerationRepository


class ReviewRepository(GenerationRepository):
    def pending(self, offset=0):
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT d.id,d.version_snapshot,left(d.body,180) AS preview,
                c.gmail_thread_id,m.email AS mailbox
                FROM outreach_pilot.drafts d
                JOIN outreach_pilot.conversations c ON c.id=d.conversation_id
                JOIN outreach_pilot.mailboxes m ON m.id=c.mailbox_id
                WHERE d.state='pending_review' ORDER BY d.id LIMIT 50 OFFSET %s""",(offset,))
            return cur.fetchall()

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
            return draft

    def decide(self,draft_id,decision,reviewer,reason=''):
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT outreach_pilot.review_draft(%s,%s,%s,%s)",
                        (str(draft_id),decision,reviewer,reason))
            return cur.fetchone()[0]
