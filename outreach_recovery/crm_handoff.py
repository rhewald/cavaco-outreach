"""Atomic local handoff. Call with the ingestion/delivery transaction cursor."""


def register_route(cur, conversation_id, *, portal_id, contact_id, contact_email,
                   mailbox_email, reviewer, approval_reference):
    # Lock in the same order as inbound ingestion and Gmail completion.
    cur.execute("SELECT id FROM outreach_pilot.conversations WHERE id=%s FOR UPDATE", (conversation_id,))
    if not cur.fetchone():
        raise ValueError("Unknown conversation")
    cur.execute("""SELECT m.email FROM outreach_pilot.mailboxes m JOIN outreach_pilot.conversations c
        ON c.mailbox_id=m.id WHERE c.id=%s""", (conversation_id,))
    row=cur.fetchone()
    actual=row['email'] if isinstance(row,dict) else row[0]
    if actual.lower()!=mailbox_email.lower():
        raise ValueError("Mailbox association mismatch")
    cur.execute("""INSERT INTO outreach_pilot.crm_conversation_routes
        (conversation_id,portal_id,contact_id,contact_email,mailbox_email,approved_by,approval_reference)
        VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(conversation_id) DO NOTHING""",
        (conversation_id,str(portal_id),str(contact_id),contact_email,mailbox_email,reviewer,approval_reference))
    cur.execute("""SELECT portal_id,contact_id,contact_email,mailbox_email
        FROM outreach_pilot.crm_conversation_routes WHERE conversation_id=%s""", (conversation_id,))
    row=cur.fetchone()
    values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
    if values != (str(portal_id),str(contact_id),contact_email,mailbox_email):
        raise ValueError("Conversation already has a different CRM association")
    enqueue_replies(cur,conversation_id)


def enqueue_replies(cur, conversation_id):
    # A pure DB insert: no network call, no Gmail operation and no generated text.
    cur.execute("""INSERT INTO outreach_pilot.crm_activity_jobs
        (message_id,portal_id,contact_id,payload,state,approved_by,approved_at)
        SELECT m.id,r.portal_id,r.contact_id,jsonb_build_object(
            'hubspot_portal_id',r.portal_id,'hubspot_contact_id',r.contact_id,
            'from',h.sender,'to',h.recipient,'subject',h.subject,
            'body',m.body_text,'direction','inbound','timestamp',m.received_at::text),
            'pending',r.approved_by,r.created_at
        FROM outreach_pilot.messages m
        JOIN outreach_pilot.crm_conversation_routes r ON r.conversation_id=m.conversation_id
        JOIN outreach_pilot.crm_inbound_headers h ON h.mailbox_id=m.mailbox_id AND h.gmail_message_id=m.gmail_message_id
        WHERE m.conversation_id=%s AND m.direction='inbound'
          AND lower(h.sender)=lower(r.contact_email) AND lower(h.recipient)=lower(r.mailbox_email)
        ON CONFLICT(message_id,portal_id) DO NOTHING RETURNING id""", (conversation_id,))
    jobs=cur.fetchall()
    for row in jobs:
        identifier=row['id'] if isinstance(row,dict) else row[0]
        cur.execute("INSERT INTO outreach_pilot.crm_activity_events(job_id,event) VALUES(%s,'conversation_route_approved')",(identifier,))


def retain_inbound_headers(cur, parsed):
    # First verified observation wins, like inbound_receipts. Retrying altered
    # input cannot rewrite the routing used by an existing immutable receipt.
    cur.execute("""INSERT INTO outreach_pilot.crm_inbound_headers
        (mailbox_id,gmail_message_id,sender,recipient,subject) VALUES(%s,%s,%s,%s,%s)
        ON CONFLICT(mailbox_id,gmail_message_id) DO NOTHING""",
        (parsed['mailbox_id'],parsed['gmail_message_id'],parsed['sender'],parsed['recipient'],parsed['subject']))


def handoff_ingested(cur, message_id):
    if not message_id:
        return
    cur.execute("SELECT conversation_id FROM outreach_pilot.messages WHERE id=%s", (message_id,))
    row=cur.fetchone()
    if not row:
        return
    conversation=row['conversation_id'] if isinstance(row,dict) else row[0]
    cur.execute("SELECT id FROM outreach_pilot.conversations WHERE id=%s FOR UPDATE", (conversation,))
    enqueue_replies(cur,conversation)
