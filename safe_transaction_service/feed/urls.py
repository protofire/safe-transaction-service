from django.urls import path

from . import views

app_name = "feed"

urlpatterns = [
    path(
        "stats/all-transactions/",
        views.AllTransactionsGlobalView.as_view(),
        name="all-transactions-global",
    ),
] 