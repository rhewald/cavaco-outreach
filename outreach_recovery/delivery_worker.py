"""One bounded durable operation per invocation; provider adapters are injected."""
from outreach_recovery.delivery_contracts import MutationResult, MutationState, ReconciliationResult, ReconciliationState

class DeliveryWorker:
    def __init__(self, repository, *, gmail, hubspot, timeout_seconds=30, lease_seconds=120):
        if not 0<timeout_seconds<lease_seconds/2:
            raise ValueError('Provider timeout must be less than half the lease')
        self.repository=repository
        self.gmail=gmail
        self.hubspot=hubspot
        self.timeout_seconds=timeout_seconds
        self.lease_seconds=lease_seconds

    def run_once(self, kind, operation_id=None):
        provider=self.gmail if kind=='gmail_send' else self.hubspot
        if provider is None:
            raise ValueError('Provider not configured; no job claimed')
        claim=self.repository.claim(kind=kind,operation_id=operation_id,lease_seconds=self.lease_seconds)
        if claim is None:
            return 'idle'
        kwargs=dict(operation_id=str(claim['id']),timeout_seconds=self.timeout_seconds)
        if claim['recovery']:
            try:
                result=provider.reconcile(claim['payload'],**kwargs)
            except Exception:
                result=ReconciliationResult(ReconciliationState.LOOKUP_FAILED)
            if isinstance(result,ReconciliationResult) and result.state==ReconciliationState.FOUND and result.provider_id:
                return 'completed' if self.repository.complete(claim,result.provider_id,result.thread_id) else 'lease_lost'
            event='not_found_yet' if isinstance(result,ReconciliationResult) and result.state==ReconciliationState.NOT_FOUND_YET else 'lookup_failed'
            self.repository.defer(claim,event)
            return 'reconciliation_required'
        if not self.repository.begin(claim):
            return 'dispatch_blocked'
        try:
            mutate=provider.send if kind=='gmail_send' else provider.create_email
            result=mutate(claim['payload'],**kwargs)
        except Exception:
            result=MutationResult(MutationState.UNCERTAIN)
        # A process death (BaseException) intentionally leaves a recoverable lease.
        if isinstance(result,MutationResult) and result.state==MutationState.ACCEPTED and result.provider_id:
            # Database failures must propagate, never invoke the mutation again.
            return 'completed' if self.repository.complete(claim,result.provider_id,result.thread_id) else 'lease_lost'
        if isinstance(result,MutationResult) and result.state==MutationState.REJECTED:
            self.repository.defer(claim,'rejected',rejected=True,safe_to_retry=result.safe_to_retry)
            return 'rejected'
        self.repository.defer(claim,'uncertain')
        return 'reconciliation_required'
