from django.conf import settings
from django.urls import reverse
from django.test import Client, TestCase, override_settings
from rest_framework import status


class StatsApiViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("v2:feed:all-transactions-global")
        self.valid_key = "test-internal-key"
        self.invalid_key = "invalid-key"

    @override_settings(ENABLE_STATS_API=False)
    def test_api_disabled(self):
        """Endpoint should return 403/404 if ENABLE_STATS_API is False."""
        response = self.client.get(self.url)
        # Expecting Forbidden as the permission check runs first
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(ENABLE_STATS_API=True, INTERNAL_API_KEYS=[self.valid_key])
    def test_missing_key(self):
        """Endpoint should return 403 if ENABLE_STATS_API is True but key is missing."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(ENABLE_STATS_API=True, INTERNAL_API_KEYS=[self.valid_key])
    def test_invalid_key(self):
        """Endpoint should return 403 if ENABLE_STATS_API is True but key is invalid."""
        response = self.client.get(
            self.url, HTTP_X_INTERNAL_API_KEY=self.invalid_key
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(ENABLE_STATS_API=True, INTERNAL_API_KEYS=[self.valid_key])
    def test_valid_key_and_enabled(self):
        """
        Endpoint should return 200 OK if ENABLE_STATS_API is True and a valid key is provided.
        (Further tests needed for data and pagination).
        """
        response = self.client.get(
            self.url, HTTP_X_INTERNAL_API_KEY=self.valid_key
        )
        # Expect 200 OK, even if the data list is empty
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Check basic structure
        self.assertIn("data", response.json())
        self.assertIn("next_cursor", response.json()) 