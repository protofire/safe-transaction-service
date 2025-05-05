import logging
import time
import logging
import time
from collections import OrderedDict

from django.conf import settings
from django.db.models import QuerySet

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.exceptions import ParseError
from rest_framework.generics import ListAPIView
from rest_framework.response import Response

from safe_transaction_service.history.models import (
    SafeRelevantTransaction,
)

from .pagination import KeysetPagination
from .permissions import IsInternalApi
# Import the new response serializer
from .serializers import GlobalAllTransactionsResponseSerializer
# Import our new service
from .services import GlobalTransactionService

logger = logging.getLogger(__name__)

# Default settings for pagination and performance
DEFAULT_PAGE_SIZE = getattr(settings, 'TRANSACTIONS_DEFAULT_PAGE_SIZE', 1000)
MAX_PAGE_SIZE = getattr(settings, 'TRANSACTIONS_MAX_PAGE_SIZE', 10000)

@extend_schema(
    tags=["stats"],
    parameters=[
        OpenApiParameter(
            "updated_after",
            location="query",
            description="Return items created or modified after this timestamp (ISO 8601 format).",
            required=False,
            type=OpenApiTypes.DATETIME,
        ),
        OpenApiParameter(
            "limit",
            location="query",
            description="Number of items to retrieve (default: 1000, max: 10000).",
            required=False,
            type=OpenApiTypes.INT,
        ),
        OpenApiParameter(
            "cursor",
            location="query",
            description="Base64 encoded cursor for keyset pagination (timestamp|id).",
            required=False,
            type=OpenApiTypes.STR,
        ),
    ],
    responses={
        200: OpenApiResponse(
            description="List of all executed transactions across all Safes, paginated.",
            response=GlobalAllTransactionsResponseSerializer,
        ),
        400: OpenApiResponse(description="Invalid query parameter format (cursor, updated_after, limit)."),
        403: OpenApiResponse(description="Forbidden. Stats API not enabled or invalid/missing API key."),
    },
)
class AllTransactionsGlobalView(ListAPIView):
    """
    Returns all *executed* transactions across all Safes on the chain, ordered by execution time (oldest first).
    This endpoint uses keyset pagination based on `(timestamp, id)`.

    Requires `ENABLE_STATS_API=True` in settings and a valid `X-Internal-API-Key` header.
    """
    permission_classes = [IsInternalApi]
    pagination_class = KeysetPagination

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transaction_service = GlobalTransactionService()
        self.performance_metrics = {
            "queries": [],
            "total_time": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

    def get_queryset(self) -> QuerySet:
        """
        Returns the base queryset used for pagination. This efficiently gets just the
        identifiers and timestamps needed for pagination without loading all transaction data.
        """
        # We only need to fetch minimal data for pagination
        start_time = time.time()
        
        # Use an optimized query with only essential fields for pagination
        # Using specific indices (recommended to add):
        # CREATE INDEX IF NOT EXISTS idx_saferelevant_timestamp_id ON history_saferelevant_transaction (timestamp ASC, id ASC);
        queryset = (
            SafeRelevantTransaction.objects
            .select_related("ethereum_tx")
            .only("id", "timestamp", "ethereum_tx_id")
            .order_by("timestamp", "id")
        )
        
        # Note: Not executing the query here as it's executed in paginate_queryset
        self.performance_metrics["queries"].append({
            "name": "Base queryset preparation",
            "time": time.time() - start_time,
            "count": "N/A - lazy query"
        })
        return queryset

    def list(self, request, *args, **kwargs):
        """
        Process the request and return paginated transaction data.
        This implementation is optimized to minimize database queries and memory usage.
        """
        # Uncomment for debugging specific requests
        # import pdb; pdb.set_trace()
        
        logger.info(f"Starting AllTransactionsGlobalView.list with request: {request.query_params}")
        
        start_total = time.time()
        
        # Get the base queryset for pagination
        start_time = time.time()
        queryset = self.filter_queryset(self.get_queryset())
        self.performance_metrics["queries"].append({
            "name": "Filter queryset",
            "time": time.time() - start_time,
            "count": "N/A - lazy query"
        })

        # Get paginated identifiers using cursor pagination
        start_time = time.time()
        page_items = self.paginate_queryset(queryset)
        page_count = len(page_items) if page_items else 0
        elapsed = time.time() - start_time
        self.performance_metrics["queries"].append({
            "name": "Pagination query",
            "count": page_count,
            "time": elapsed
        })
        
        if page_items:
            # Debug info for pagination items (uncomment when needed)
            logger.info(f"Page items: count={page_count}, first_id={page_items[0].id if page_items else None}")
            
        if not page_items:
            # Return empty response with pagination structure
            self.performance_metrics["total_time"] = time.time() - start_total
            self._log_performance_metrics()
            return Response(OrderedDict([
                ("metrics", self._format_metrics_for_response()),
                ("next_cursor", None), 
                ("data", [])
            ]))

        # Extract ethereum_tx_ids from the paginated items
        ethereum_tx_ids = [item.ethereum_tx_id for item in page_items]
        
        # Debug transaction IDs (uncomment when needed)
        # logger.info(f"Processing transaction IDs: {', '.join(ethereum_tx_ids[:5])}{'...' if len(ethereum_tx_ids) > 5 else ''}")

        # Fetch full transaction data using our optimized service
        start_time = time.time()
        transactions = self.transaction_service.get_transactions_with_transfers_from_tx_ids(
            ethereum_tx_ids
        )
        elapsed = time.time() - start_time
        self.performance_metrics["queries"].append({
            "name": "Fetch and serialize transactions",
            "count": len(transactions),
            "time": elapsed,
        })
        
        # Retrieve service performance metrics
        service_metrics = self.transaction_service.get_performance_metrics()
        
        # Update our cache metrics with service metrics
        if "cache_hits" in service_metrics:
            self.performance_metrics["cache_hits"] += service_metrics["cache_hits"]
            self.performance_metrics["cache_misses"] += service_metrics["cache_misses"]

        # Build response with pagination info
        start_time = time.time()
        next_cursor = None
        if self.paginator.has_next and page_items:
            # Create cursor from the last item in current page
            next_cursor = self.paginator.encode_cursor(page_items[-1])
        self.performance_metrics["queries"].append({
            "name": "Build pagination cursor",
            "time": time.time() - start_time,
            "count": "N/A"
        })

        self.performance_metrics["total_time"] = time.time() - start_total
        self._log_performance_metrics()

        # Log combined metrics - view + service
        logger.info("Combined View + Service Performance:")
        total_time = self.performance_metrics["total_time"] + service_metrics["total_time"]
        logger.info(f"  View time: {self.performance_metrics['total_time']:.4f} seconds")
        logger.info(f"  Service time: {service_metrics['total_time']:.4f} seconds")
        logger.info(f"  Total time: {total_time:.4f} seconds")
        if "cache_hits" in self.performance_metrics:
            logger.info(f"  Total cache hits: {self.performance_metrics['cache_hits']}")
            logger.info(f"  Total cache misses: {self.performance_metrics['cache_misses']}")

        # Return the final response with metrics included
        return Response(
            OrderedDict([
                ("metrics", self._format_metrics_for_response(service_metrics)),
                ("next_cursor", next_cursor),
                ("data", transactions),
            ])
        )
        
    def _format_metrics_for_response(self, service_metrics=None):
        """
        Format performance metrics for inclusion in the API response.
        """
        # Calculate total query time from both view and service
        total_time = self.performance_metrics["total_time"]
        service_time = service_metrics["total_time"] if service_metrics else 0
        
        # Format view metrics
        formatted_metrics = {
            "execution_time": {
                "total_seconds": round(total_time + service_time, 4),
                "view_processing_seconds": round(total_time, 4),
                "service_processing_seconds": round(service_time, 4) if service_metrics else 0,
            },
            "cache": {
                "hits": self.performance_metrics.get("cache_hits", 0),
                "misses": self.performance_metrics.get("cache_misses", 0),
                "hit_ratio": round(self.performance_metrics.get("cache_hits", 0) / 
                              (self.performance_metrics.get("cache_hits", 0) + 
                               self.performance_metrics.get("cache_misses", 1)) * 100, 2) 
                              if (self.performance_metrics.get("cache_hits", 0) + 
                                  self.performance_metrics.get("cache_misses", 0)) > 0 else 0
            },
            "queries": []
        }
        
        # Add view queries
        for query in self.performance_metrics.get("queries", []):
            formatted_query = {
                "name": query.get("name", "Unknown"),
                "time_seconds": round(query.get("time", 0), 4)
            }
            
            if "count" in query and query["count"] != "N/A" and query["count"] != "N/A - lazy query":
                formatted_query["record_count"] = query["count"]
                
            formatted_metrics["queries"].append(formatted_query)
        
        # Add service queries if available
        if service_metrics and "queries" in service_metrics:
            for query in service_metrics.get("queries", []):
                formatted_query = {
                    "name": query.get("name", "Unknown"),
                    "time_seconds": round(query.get("time", 0), 4)
                }
                
                if "count" in query and query["count"] != "N/A" and query["count"] != "N/A - lazy query":
                    formatted_query["record_count"] = query["count"]
                
                # Add any additional service-specific metrics
                if "cache_hits" in query:
                    formatted_query["cache_hits"] = query["cache_hits"]
                if "db_queries" in query:
                    formatted_query["db_queries"] = query["db_queries"]
                    
                formatted_metrics["queries"].append(formatted_query)
        
        return formatted_metrics

    def _log_performance_metrics(self):
        """Log the performance metrics collected during execution"""
        logger.info("Performance metrics for AllTransactionsGlobalView:")
        for query in self.performance_metrics["queries"]:
            logger.info(f"  {query['name']}: {query.get('count', 'N/A')} records in {query['time']:.4f} seconds")
        if "cache_hits" in self.performance_metrics:
            logger.info(f"  Cache hits: {self.performance_metrics['cache_hits']}")
            logger.info(f"  Cache misses: {self.performance_metrics['cache_misses']}")
        logger.info(f"  Total time: {self.performance_metrics['total_time']:.4f} seconds")
