import json

import pytest
from pydantic import ValidationError

from app.triggers.subscriptions import SubscriptionCreate
from app.dashboard.repository import PostgresDashboardStore
from tests.test_scheduler import ScheduleDashboard
from tests.test_dashboard_pages import AUTH, make_client, post


class SubscriptionDashboard(ScheduleDashboard):
    def __init__(self):
        super().__init__()
        self.subscriptions = {}

    def list_subscriptions(self):
        return list(self.subscriptions.values())

    def create_subscription(self, values):
        self.subscriptions[1] = {"id": 1, **values.model_dump(), "source_name": "Acme",
                                 "filter_name": "Python", "active": True, "source_active": True,
                                 "filter_active": True, "last_polled_at": None}
        return True

    def set_subscription_active(self, subscription_id, active):
        self.subscriptions[subscription_id]["active"] = active
        return True

    def delete_subscription(self, subscription_id):
        return self.subscriptions.pop(subscription_id, None) is not None


def test_fixture_defaults_and_interval_validation(project_root):
    values = json.loads((project_root / "tests/fixtures/subscription.json").read_text())
    assert SubscriptionCreate(**values).polling_interval_minutes == 10
    assert SubscriptionCreate(source_id=1, search_filter_id=2).polling_interval_minutes == 10
    with pytest.raises(ValidationError):
        SubscriptionCreate(**{**values, "polling_interval_minutes": 0})


def test_subscription_page_crud(tmp_path):
    store = SubscriptionDashboard()
    client = make_client(store, tmp_path)
    assert client.get("/subscriptions").status_code == 401
    assert 'value="10"' in client.get("/subscriptions", auth=AUTH).text
    response = post(client, "/subscriptions", {"source_id": 1, "search_filter_id": 2, "polling_interval_minutes": 0})
    assert "error=" in response.headers["location"] and not store.subscriptions
    post(client, "/subscriptions", {"source_id": 1, "search_filter_id": 2})
    assert "Acme" in client.get("/subscriptions", auth=AUTH).text
    post(client, "/subscriptions/1/active", {"active": "false"})
    assert not store.subscriptions[1]["active"]
    post(client, "/subscriptions/1/delete", {})
    assert not store.subscriptions
