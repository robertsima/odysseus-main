"""/api/today validates its body, and /today serves the app shell."""


def test_an_action_body_without_a_key_is_rejected(api):
    res = api.as_user("alice").post("/api/today/action", json={"action": "defer"})

    assert res.status_code == 422


def test_the_today_page_is_served_for_the_deep_link(api):
    res = api.as_user("alice").get("/today")

    assert res.status_code == 200 and "text/html" in res.headers["content-type"]
