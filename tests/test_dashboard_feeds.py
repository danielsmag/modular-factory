"""The dashboard server's routes, and the Jira boards and GitHub issues it reads for the Board."""

import json
import threading
import urllib.error
import urllib.request

import pytest
from modfactory.workspace.service import DashboardServer, one_handler


@pytest.fixture
def base(world):
    """The address of a dashboard serving `world.repo` on its own."""
    server = DashboardServer(("127.0.0.1", 0), one_handler(world.repo))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url, host=None, method="GET"):
    """(status, body, headers) of one request; an HTTP error is an answer too."""
    request = urllib.request.Request(url, method=method, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as res:
            return res.status, res.read().decode(), res.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), error.headers


# ---- Routes ---------------------------------------------------------------------


def test_unknown_paths_and_methods_are_refused(world, base):
    name = world.repo.name
    for path in ("/nope", f"/projects/{name}/nope", f"/projects/{name}/issues/not%20a%20key", "/projects/x/issues/1"):
        assert get(base + path)[0] == 404, path
    assert get(base + "/projects.json", host="evil.example")[0] == 403
    assert get(base + "/projects.json", method="POST")[0] == 501


def test_the_routes_answer_with_json(world, base):
    job = world.start(until="spec")
    name = world.repo.name
    status, body, headers = get(base + "/queue.json")
    assert status == 200 and headers["Content-Type"] == "application/json; charset=utf-8"
    assert sorted(json.loads(body)) == ["errors", "rows"]
    status, body, _ = get(f"{base}/projects/{name}/issues/1")
    assert status == 200 and json.loads(body)["title"] == "Add a thing"
    status, body, _ = get(f"{base}/projects/{name}/jobs/{job}/activity")
    assert status == 200 and "runs" in json.loads(body)
    assert "no-store" in get(base + "/projects.json")[2]["Cache-Control"]
