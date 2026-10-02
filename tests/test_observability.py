import json
from contextlib import nullcontext

from app.dashboard.repository import PostgresDashboardStore
from tests.test_scheduler import ScheduleDashboard
from tests.test_dashboard_pages import AUTH, make_client


def test_observability_page_reports_costs_failures_and_manual_reasons(project_root, tmp_path):
    data = json.loads((project_root / "tests/fixtures/observability.json").read_text())
    store = ScheduleDashboard()
    store.observability = lambda timezone: data
    client = make_client(store, tmp_path)
    assert client.get("/observability").status_code == 401
    response = client.get("/observability", auth=AUTH)
    assert response.status_code == 200
    assert "0.125000" in response.text and "watch" in response.text
    assert "CAPTCHA detected" in response.text and "&lt;script&gt;" in response.text
    assert "<script>alert" not in response.text


def test_observability_queries_aggregate_without_double_counting_pipeline(project_root):
    data = json.loads((project_root / "tests/fixtures/observability.json").read_text())
    class Connection:
        def __init__(self):
            self.queries = []
        def transaction(self):
            return nullcontext()
        def execute(self, query, params=None):
            self.queries.append((query, params))
            key = ("costs", "failures", "manual")[len(self.queries)-1]
            return [tuple(row.values()) for row in data[key]]
    connection = Connection()
    result = PostgresDashboardStore(connection).observability("Asia/Colombo")
    assert result == data
    assert connection.queries[0][1] == ("Asia/Colombo",)
    assert "cost_usd IS NULL" in connection.queries[0][0]
    assert "step <> 'pipeline'" in connection.queries[1][0]
    assert "LATERAL" in connection.queries[2][0]
