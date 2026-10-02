class HederaMirrorNodeClientException(Exception):
    """Base exception for errors talking to the Hedera Mirror Node REST API."""


class HederaMirrorNodeNotFoundException(HederaMirrorNodeClientException):
    """Raised when the Mirror Node returns 404 for a resource lookup."""


class HederaMirrorNodeRateLimitException(HederaMirrorNodeClientException):
    """Raised when the Mirror Node keeps returning 429 after retries are exhausted."""
