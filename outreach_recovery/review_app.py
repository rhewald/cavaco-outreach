"""Local, single-reviewer pilot. Binding to public interfaces is unsupported."""
import argparse
import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from outreach_recovery.review_repository import ReviewRepository

ROOT=Path(__file__).resolve().parent


def create_app(repository,reviewer,port=8765,demo=False):
    if not isinstance(reviewer,str) or not reviewer.strip() or len(reviewer)>200:
        raise ValueError("Configure a reviewer identity of 1..200 characters")
    if not 1024<=port<=65535:
        raise ValueError("Port must be 1024..65535")
    origin=f"http://127.0.0.1:{port}"
    csrf=secrets.token_urlsafe(32)
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    templates=Environment(loader=FileSystemLoader(ROOT/'review_templates'),autoescape=select_autoescape(['html']))

    @app.middleware('http')
    async def local_boundary(request,call_next):
        if request.headers.get('host') != f'127.0.0.1:{port}':
            return Response('Use the configured loopback address',status_code=400)
        if request.method=='POST' and (request.headers.get('origin')!=origin or
                request.headers.get('sec-fetch-site') not in (None,'same-origin')):
            return Response('Cross-site action blocked',status_code=403)
        try:
            response=await call_next(request)
        except Exception:
            # Do not disclose database errors, credentials, or email content.
            response=Response('Review service unavailable. Reload before retrying.',status_code=503)
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    def page(name,**context):
        return HTMLResponse(templates.get_template(name).render(reviewer=reviewer,demo=demo,csrf=csrf,**context))

    @app.get('/')
    def home():
        return RedirectResponse('/reviews',status_code=303)

    @app.get('/style.css')
    def stylesheet():
        return Response((ROOT/'review_templates/style.css').read_text(),media_type='text/css')

    @app.get('/cavaco-logo.avif')
    def brand_logo():
        return Response((ROOT/'review_templates/cavaco-logo.avif').read_bytes(),media_type='image/avif')

    @app.get('/reviews')
    def reviews(offset:int=0,q:str='',kind:str='',mailbox:str='',sort:str='oldest'):
        if offset<0 or offset>1000000 or len(q)>200 or len(mailbox)>320:
            raise HTTPException(400,'Invalid queue filter')
        if sort not in ('oldest','newest','prospect','company') or kind not in ('','initial','reply','followup'):
            raise HTTPException(400,'Invalid queue filter')
        data=repository.queue(offset,q,kind,mailbox,sort)
        filters=dict(q=q,kind=kind,mailbox=mailbox,sort=sort)
        return page('list.html',**data,**filters,offset=offset,
                    previous='/reviews?'+urlencode(dict(filters,offset=max(0,offset-50))),
                    next_page='/reviews?'+urlencode(dict(filters,offset=offset+50)))

    @app.get('/reviews/{draft_id}')
    def detail(draft_id:UUID):
        draft=repository.detail(draft_id)
        if not draft:
            raise HTTPException(404,'Draft not found')
        return page('detail.html',draft=draft)

    async def action(request,draft_id,decision):
        if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':
            raise HTTPException(415,'Expected a review form')
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>16384:
                raise HTTPException(413,'Review form too large')
        try:
            form=parse_qs(body.decode('utf-8'),keep_blank_values=True,max_num_fields=4)
        except (ValueError,UnicodeError):
            raise HTTPException(400,'Invalid form')
        if set(form)-{'csrf','reason','envelope_id'} or any(len(v)!=1 for v in form.values()):
            raise HTTPException(400,'Invalid form fields')
        token=form.get('csrf',[''])[0]
        if not hmac.compare_digest(token.encode('utf-8'),csrf.encode('utf-8')):
            raise HTTPException(403,'Review token expired; reload this page')
        reason=form.get('reason',[''])[0].strip()
        if len(reason)>2000:
            raise HTTPException(400,'Reason exceeds 2000 characters')
        envelope_id=form.get('envelope_id',[''])[0]
        try:
            envelope_id=UUID(envelope_id) if envelope_id else None
        except ValueError:
            raise HTTPException(400,'Invalid delivery envelope')
        result=await run_in_threadpool(repository.decide,draft_id,decision,reviewer,reason,envelope_id)
        if result=='not_found':
            raise HTTPException(404,'Draft not found')
        if result in ('superseded','conflict'):
            text='A new reply arrived. This draft cannot be approved.' if result=='superseded' else 'The draft decision or delivery details changed. Reload before approving.'
            response=page('result.html',message=text,draft_id=draft_id)
            response.status_code=409
            return response
        return RedirectResponse(f'/reviews/{draft_id}',status_code=303)

    @app.post('/reviews/{draft_id}/approve')
    async def approve(request:Request,draft_id:UUID):
        return await action(request,draft_id,'approved')

    @app.post('/reviews/{draft_id}/reject')
    async def reject(request:Request,draft_id:UUID):
        return await action(request,draft_id,'rejected')

    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reviewer',default=os.environ.get('OUTREACH_REVIEWER'))
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--demo',action='store_true',help='Disposable synthetic review queue; no model/API calls')
    args=parser.parse_args()
    if not args.reviewer or not args.reviewer.strip():
        parser.error('Set --reviewer or OUTREACH_REVIEWER')
    if args.demo:
        import tempfile
        import pgserver
        import psycopg2
        from outreach_recovery.smoke_openai import run_synthetic_job
        server=pgserver.get_server(Path(tempfile.mkdtemp(prefix='outreach-review-')),cleanup_mode='delete')
        dsn=server.get_uri()
        conn=psycopg2.connect(dsn)
        try:
            with conn,conn.cursor() as cur:
                for name in ('inbound.sql','002_ingestion_concurrency.sql','003_draft_generation.sql','004_seller_validation.sql','005_human_review.sql','006_delivery_outbox.sql','007_initial_outreach.sql','008_provider_observations.sql','009_crm_activity_jobs.sql','010_crm_handoff.sql','011_gmail_monitor.sql','012_review_context.sql','013_review_contact_links.sql'):
                    cur.execute((ROOT/name).read_text())
        finally:
            conn.close()
        class DemoGenerator:
            timeout_seconds=5
            def generate(self,messages):
                return 'Thank you for your interest. I would be happy to discuss the example product and answer your questions. What time would work for a short conversation?'
        run_synthetic_job(dsn,DemoGenerator())
    else:
        dsn=os.environ.get('OUTREACH_DATABASE_URL')
        if not dsn:
            parser.error('Set OUTREACH_DATABASE_URL or use --demo')
    app=create_app(ReviewRepository(dsn),args.reviewer,args.port,args.demo)
    import uvicorn
    print(f'Open http://127.0.0.1:{args.port}/reviews')
    uvicorn.run(app,host='127.0.0.1',port=args.port,proxy_headers=False,access_log=False)


if __name__=='__main__':
    main()
