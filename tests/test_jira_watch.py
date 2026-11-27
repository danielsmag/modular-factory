"""`[tracker.jira.columns].watch`: statuses whose cards the dashboard shows and the factory never builds."""

import json

import pytest
from conftest import JIRA_COLUMNS
from modfactory.util import Refused


def test_a_watched_card_is_shown_on_the_dashboard_and_never_built(world):
    world.use_jira(columns=JIRA_COLUMNS + 'watch = "Backlog"\n')
    queued = world.jira.ticket("Queued")
    coming = world.jira.ticket("Coming up", status="Backlog")
    text = world.cli("queue").stdout
    assert queued in text and coming not in text
    out = world.cli("queue", "--json").stdout
    rows = {row["issue"]: row for row in json.loads(out[out.index("["):])}
    assert rows[queued]["action"] == "new"
    assert {k: rows[coming][k] for k in ("action", "job", "note", "title")} == {
        "action": "watch", "job": "-", "note": "Backlog", "title": "Coming up"}
    with pytest.raises(Refused, match="only watched, not built. Move it to `Ready for factory` first"):
        world.create(coming)
