"""Error taxonomy. The orchestrator maps each class to a job state:
TransientError -> RETRYING (bounded), ProviderBlocked -> BLOCKED_BY_PROVIDER,
BudgetExceeded -> PAUSED, QuotaExhausted -> PAUSED, PermanentError -> FAILED."""


class PipelineError(Exception):
    pass


class TransientError(PipelineError):
    """Network blips, 429/5xx, timeouts. Safe to retry with backoff."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class PermanentError(PipelineError):
    """Bad request, invalid output after repair attempts, etc. Do not retry."""


class ProviderBlocked(PipelineError):
    """A required external capability is unavailable: missing credentials, no API access,
    revoked OAuth, circuit open, API restriction. Owner action required."""

    def __init__(self, provider: str, requirement: str):
        super().__init__(f"{provider}: {requirement}")
        self.provider = provider
        self.requirement = requirement


class BudgetExceeded(PipelineError):
    pass


class QuotaExhausted(PipelineError):
    pass


class QualityGateFailed(PipelineError):
    pass


def classify_http(status: int, body: str, provider: str) -> PipelineError:
    snippet = (body or "")[:500]
    if status in (408, 425, 429) or status >= 500:
        return TransientError(f"{provider} HTTP {status}: {snippet}")
    if status in (401, 403):
        return ProviderBlocked(provider, f"authentication/permission denied (HTTP {status}): {snippet}")
    if status == 402:
        return ProviderBlocked(provider, f"payment/credits required (HTTP 402): {snippet}")
    return PermanentError(f"{provider} HTTP {status}: {snippet}")
