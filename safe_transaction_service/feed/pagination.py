import base64
import logging
from collections import OrderedDict
from typing import Optional

from django.conf import settings
from django.db.models import QuerySet, Q
from django.utils.dateparse import parse_datetime

from rest_framework.exceptions import ParseError
from rest_framework.pagination import BasePagination
from rest_framework.request import Request
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class KeysetPagination(BasePagination):
    """
    Keyset pagination based on timestamp and the primary key (`id`).
    Cursor is expected to be base64 encoded `timestamp|id`.
    Requires the queryset to be ordered by `("timestamp", "id")`.
    """

    page_size = settings.REST_FRAMEWORK.get("PAGE_SIZE", 100)
    page_size_query_param = "limit"
    max_page_size = 1000  # Maximum limit allowed
    ordering = ("timestamp", "id") # Use id for ordering
    cursor_query_param = "cursor"

    def get_page_size(self, request: Request) -> int:
        if self.page_size_query_param:
            try:
                limit = request.query_params.get(self.page_size_query_param)
                if limit:
                    val = int(limit)
                    if val <= 0:
                        raise ValueError("Limit must be positive")
                    return min(val, self.max_page_size)
            except (KeyError, ValueError) as e:
                logger.warning(
                    "Invalid %s pagination parameter: %s",
                    self.page_size_query_param,
                    e,
                )
        return self.page_size

    def decode_cursor(self, request: Request) -> Optional[tuple[str, str]]:
        """Decodes the base64 cursor `timestamp|id` into a tuple."""
        encoded = request.query_params.get(self.cursor_query_param)
        if not encoded:
            return None
        try:
            decoded = base64.urlsafe_b64decode(encoded.encode()).decode()
            timestamp_str, item_id = decoded.split("|")
            # Validate timestamp format (doesn't need to be a valid date, just format)
            parse_datetime(timestamp_str)  # Raises ValueError if format is wrong
            # Validate id format
            int(item_id) # Raises ValueError if format is wrong
            return timestamp_str, item_id
        except (ValueError, TypeError, UnicodeDecodeError) as e:
            raise ParseError(f"Invalid cursor format: {e}")

    def encode_cursor(self, item) -> str:
        """
        Encodes the cursor based on item's `timestamp` and `id` attributes.
        """
        timestamp_str = item.timestamp.isoformat().replace("+00:00", "Z")
        item_id = str(item.id)
        cursor_str = f"{timestamp_str}|{item_id}"
        return base64.urlsafe_b64encode(cursor_str.encode()).decode()

    def paginate_queryset(self, queryset: QuerySet, request: Request, view=None):
        self.limit = self.get_page_size(request)
        self.cursor = self.decode_cursor(request)
        self.updated_after_param = "updated_after"
        self.updated_after = None

        # Apply updated_after filter first
        updated_after_str = request.query_params.get(self.updated_after_param)
        if updated_after_str:
            try:
                self.updated_after = parse_datetime(updated_after_str)
                if self.updated_after is None:
                    raise ValueError("Invalid datetime format")
                queryset = queryset.filter(timestamp__gte=self.updated_after)
            except ValueError as e:
                raise ParseError(f"Invalid {self.updated_after_param} format: {e}")

        # Apply cursor filter
        if self.cursor:
            timestamp_cursor, id_cursor = self.cursor
            queryset = queryset.filter(
                Q(timestamp__gt=timestamp_cursor)
                | Q(timestamp=timestamp_cursor, id__gt=id_cursor) # Use id for filtering
            )

        # Apply ordering (should match cursor logic)
        queryset = queryset.order_by(*self.ordering)

        # Fetch one extra item to determine if there is a next page
        results = list(queryset[: self.limit + 1])
        self.has_next = len(results) > self.limit
        results = results[: self.limit]

        return results

    # The default get_paginated_response is not used by the view, 
    # as the view constructs the response manually with next_cursor and data.
    # We keep this for schema generation.
    def get_paginated_response(self, data):
        # This method implementation is effectively unused by AllTransactionsGlobalView
        # The view builds the response directly.
        return Response(OrderedDict())

    def get_paginated_response_schema(self, schema):
        # This defines the expected response structure for documentation
        return {
            "type": "object",
            "properties": {
                "next_cursor": {
                    "type": "string",
                    "nullable": True,
                    "example": "MjAyNC0wNy0xOVQxNjowMDowMFp8MTIzNA==", # Example cursor timestamp|id
                    "description": "Base64 encoded cursor for the next page (timestamp|id)",
                },
                "data": schema,
            },
        }

    # get_next_link is not needed as the view generates the cursor directly.

    # display_page_controls = False # Keyset pagination doesn't use standard page numbers 