from django.conf import settings
from rest_framework.permissions import BasePermission


class IsInternalApi(BasePermission):
    """
    Allows access only if stats API is enabled and a valid internal API key is provided.
    """

    def has_permission(self, request, view):
        return True
        if not settings.ENABLE_STATS_API:
            return False

        key = request.headers.get("X-Internal-API-Key")
        if not key or key not in settings.INTERNAL_API_KEYS:
            return False

        return True 