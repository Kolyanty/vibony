"""Task CRUD REST + validation + visibility.

Covers create/get/list/update/delete, the runtime_image whitelist + slug + private
filename validation, slug uniqueness (409), template read-only (403), and the
cross-workspace access boundary. Templates are seeded by the http_client fixture.
"""

from __future__ import annotations

import pytest

_session_loop = pytest.mark.asyncio(loop_scope="session")

VALID_TASK = {
    "slug": "my-task",
    "title": "My Task",
    "description_md": "# Build it",
    "starter_files": [{"path": "main.py", "content": "print('hi')\n"}],
    "runtime_image": "vibony/sandbox-py:3.12",
    "weakness": {"blindspots": [{"issue": "x"}]},
    "spec": {"context": "c"},
    "visibility": "workspace",
}


async def _signup_second_user(client, email="other@example.com"):
    resp = await client.post(
        "/auth/signup",
        json={"email": email, "password": "password123", "company_name": "Other Co"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["company"]["id"]


# --- CRUD happy path ---


@_session_loop
async def test_create_get_task(authed_client):
    client, _ = authed_client
    r = await client.post("/tasks", json=VALID_TASK)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "my-task"
    assert body["can_edit"] is True
    assert body["is_template"] is False
    assert body["starter_files"][0]["path"] == "main.py"

    got = await client.get(f"/tasks/{body['id']}")
    assert got.status_code == 200
    assert got.json()["title"] == "My Task"
    assert got.json()["weakness"]["blindspots"][0]["issue"] == "x"


@_session_loop
async def test_list_includes_templates_and_own(authed_client):
    client, _ = authed_client
    await client.post("/tasks", json=VALID_TASK)
    r = await client.get("/tasks")
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    slugs = {t["slug"] for t in tasks}
    assert "my-task" in slugs
    # Seeded templates appear, read-only.
    templates = [t for t in tasks if t["is_template"]]
    assert {"task-02-seat-reservation", "task-01-training-loop"} <= {t["slug"] for t in templates}
    assert all(t["can_edit"] is False for t in templates)


@_session_loop
async def test_update_task(authed_client):
    client, _ = authed_client
    created = (await client.post("/tasks", json=VALID_TASK)).json()
    r = await client.put(f"/tasks/{created['id']}", json={"title": "Renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"
    # Untouched fields preserved.
    assert r.json()["slug"] == "my-task"


@_session_loop
async def test_update_explicit_null_is_no_op_not_500(authed_client):
    """An explicit JSON null on a NOT-NULL field means 'no change', not 'set null'
    (must not 500). The untouched value is preserved."""
    client, _ = authed_client
    created = (await client.post("/tasks", json=VALID_TASK)).json()
    r = await client.put(
        f"/tasks/{created['id']}", json={"weakness": None, "title": "Kept"}
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Kept"
    assert r.json()["weakness"] == {"blindspots": [{"issue": "x"}]}  # preserved


@_session_loop
async def test_delete_task(authed_client):
    client, _ = authed_client
    created = (await client.post("/tasks", json=VALID_TASK)).json()
    r = await client.delete(f"/tasks/{created['id']}")
    assert r.status_code == 204
    assert (await client.get(f"/tasks/{created['id']}")).status_code == 404


# --- validation ---


@_session_loop
async def test_create_rejects_bad_image(authed_client):
    client, _ = authed_client
    r = await client.post("/tasks", json={**VALID_TASK, "runtime_image": "evil/img:latest"})
    assert r.status_code == 422


@_session_loop
async def test_create_rejects_bad_slug(authed_client):
    client, _ = authed_client
    r = await client.post("/tasks", json={**VALID_TASK, "slug": "Bad Slug!"})
    assert r.status_code == 422


@_session_loop
async def test_create_rejects_private_starter_file(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/tasks",
        json={**VALID_TASK, "starter_files": [{"path": "weakness.yaml", "content": "x"}]},
    )
    assert r.status_code == 422


@_session_loop
async def test_create_rejects_path_traversal(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/tasks",
        json={**VALID_TASK, "starter_files": [{"path": "../etc/passwd", "content": "x"}]},
    )
    assert r.status_code == 422


@_session_loop
async def test_create_duplicate_slug_conflicts(authed_client):
    client, _ = authed_client
    assert (await client.post("/tasks", json=VALID_TASK)).status_code == 201
    r = await client.post("/tasks", json=VALID_TASK)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "slug_taken"


@_session_loop
async def test_create_cannot_set_public(authed_client):
    client, _ = authed_client
    r = await client.post("/tasks", json={**VALID_TASK, "visibility": "public"})
    assert r.status_code == 422  # public not in EditableVisibility


@_session_loop
async def test_create_wrong_workspace_forbidden(authed_client):
    client, _ = authed_client
    import uuid

    r = await client.post(
        "/tasks", json={**VALID_TASK, "workspace_id": str(uuid.uuid4())}
    )
    assert r.status_code == 403


# --- templates read-only ---


@_session_loop
async def test_template_is_read_only(authed_client):
    client, _ = authed_client
    tasks = (await client.get("/tasks")).json()["tasks"]
    template = next(t for t in tasks if t["is_template"])
    assert template["can_edit"] is False
    assert (await client.put(f"/tasks/{template['id']}", json={"title": "x"})).status_code == 403
    assert (await client.delete(f"/tasks/{template['id']}")).status_code == 403


# --- cross-workspace boundary ---


@_session_loop
async def test_cross_workspace_task_not_visible(authed_client):
    client, _ = authed_client
    created = (await client.post("/tasks", json=VALID_TASK)).json()

    # Sign up a second user in a different workspace (cookie swaps to user B).
    await _signup_second_user(client)

    assert (await client.get(f"/tasks/{created['id']}")).status_code == 404
    slugs = {t["slug"] for t in (await client.get("/tasks")).json()["tasks"]}
    assert "my-task" not in slugs  # B can't see A's workspace task
    # B still sees the shared templates.
    assert "task-01-training-loop" in slugs

    assert (await client.put(f"/tasks/{created['id']}", json={"title": "x"})).status_code == 404
    assert (await client.delete(f"/tasks/{created['id']}")).status_code == 404


# --- favorites (per-user) ---


def _by_id(tasks: list[dict]) -> dict[str, dict]:
    return {t["id"]: t for t in tasks}


@_session_loop
async def test_favorite_toggle_reflects_in_list_and_get(authed_client):
    client, _ = authed_client
    created = (await client.post("/tasks", json=VALID_TASK)).json()
    tid = created["id"]

    assert created["is_favorite"] is False  # not favorite on create
    listed = _by_id((await client.get("/tasks")).json()["tasks"])
    assert listed[tid]["is_favorite"] is False

    assert (await client.put(f"/tasks/{tid}/favorite")).status_code == 204
    listed = _by_id((await client.get("/tasks")).json()["tasks"])
    assert listed[tid]["is_favorite"] is True
    assert (await client.get(f"/tasks/{tid}")).json()["is_favorite"] is True  # detail agrees

    assert (await client.delete(f"/tasks/{tid}/favorite")).status_code == 204
    listed = _by_id((await client.get("/tasks")).json()["tasks"])
    assert listed[tid]["is_favorite"] is False


@_session_loop
async def test_favorite_is_idempotent(authed_client):
    client, _ = authed_client
    tid = (await client.post("/tasks", json=VALID_TASK)).json()["id"]
    # Double PUT and double DELETE both succeed (unique user+task; no error).
    assert (await client.put(f"/tasks/{tid}/favorite")).status_code == 204
    assert (await client.put(f"/tasks/{tid}/favorite")).status_code == 204
    listed = _by_id((await client.get("/tasks")).json()["tasks"])
    assert listed[tid]["is_favorite"] is True
    assert (await client.delete(f"/tasks/{tid}/favorite")).status_code == 204
    assert (await client.delete(f"/tasks/{tid}/favorite")).status_code == 204


@_session_loop
async def test_favorite_a_template(authed_client):
    client, _ = authed_client
    template = next(t for t in (await client.get("/tasks")).json()["tasks"] if t["is_template"])
    assert (await client.put(f"/tasks/{template['id']}/favorite")).status_code == 204
    listed = _by_id((await client.get("/tasks")).json()["tasks"])
    assert listed[template["id"]]["is_favorite"] is True


@_session_loop
async def test_favorite_is_per_user(authed_client):
    client, _ = authed_client
    template = next(t for t in (await client.get("/tasks")).json()["tasks"] if t["is_template"])
    assert (await client.put(f"/tasks/{template['id']}/favorite")).status_code == 204

    # A second user in another workspace sees the same shared template as NOT favorite.
    await _signup_second_user(client)
    listed = _by_id((await client.get("/tasks")).json()["tasks"])
    assert listed[template["id"]]["is_favorite"] is False


@_session_loop
async def test_favorite_unseeable_task_404(authed_client):
    client, _ = authed_client
    tid = (await client.post("/tasks", json=VALID_TASK)).json()["id"]
    # User B can't see A's workspace task → can't favorite it (no existence probe).
    await _signup_second_user(client)
    assert (await client.put(f"/tasks/{tid}/favorite")).status_code == 404


# --- catalog search (?q=) ---


@_session_loop
async def test_search_by_title(authed_client):
    client, _ = authed_client
    await client.post(
        "/tasks", json={**VALID_TASK, "slug": "zephyr-widget", "title": "Zephyr Widget"}
    )
    hit = {t["slug"] for t in (await client.get("/tasks", params={"q": "zephyr"})).json()["tasks"]}
    assert "zephyr-widget" in hit
    miss = {t["slug"] for t in (await client.get("/tasks", params={"q": "nomatchxyz"})).json()["tasks"]}
    assert "zephyr-widget" not in miss


@_session_loop
async def test_search_by_slug_and_description(authed_client):
    client, _ = authed_client
    await client.post(
        "/tasks",
        json={
            **VALID_TASK,
            "slug": "quokka-task",
            "title": "Plain Title",
            "description_md": "# supercalifragilistic pipeline",
        },
    )
    by_slug = {t["slug"] for t in (await client.get("/tasks", params={"q": "quokka"})).json()["tasks"]}
    assert "quokka-task" in by_slug
    by_desc = {
        t["slug"]
        for t in (await client.get("/tasks", params={"q": "supercalifragilistic"})).json()["tasks"]
    }
    assert "quokka-task" in by_desc


@_session_loop
async def test_search_is_case_insensitive(authed_client):
    client, _ = authed_client
    await client.post(
        "/tasks", json={**VALID_TASK, "slug": "zephyr-widget", "title": "Zephyr Widget"}
    )
    hit = {t["slug"] for t in (await client.get("/tasks", params={"q": "ZEPHYR"})).json()["tasks"]}
    assert "zephyr-widget" in hit


@_session_loop
async def test_search_escapes_wildcards(authed_client):
    client, _ = authed_client
    await client.post(
        "/tasks", json={**VALID_TASK, "slug": "zephyr-widget", "title": "Zephyr Widget"}
    )
    # If '%' were an unescaped ILIKE wildcard it'd match everything; escaped, it only
    # matches a literal '%', so the plain-named task is excluded.
    hit = {t["slug"] for t in (await client.get("/tasks", params={"q": "%"})).json()["tasks"]}
    assert "zephyr-widget" not in hit


@_session_loop
async def test_search_multiword_tokens_and_across_fields(authed_client):
    client, _ = authed_client
    await client.post(
        "/tasks",
        json={
            **VALID_TASK,
            "slug": "my-ab-test",
            "title": "A/B Test Report",
            "description_md": "# conversion analysis",
        },
    )
    # "ab test": 'ab' matches the slug, 'test' matches the title — the whole phrase is a
    # substring of neither, but the tokens are ANDed so the row is found.
    hit = {t["slug"] for t in (await client.get("/tasks", params={"q": "ab test"})).json()["tasks"]}
    assert "my-ab-test" in hit
    # A token can land in the description too.
    hit2 = {
        t["slug"] for t in (await client.get("/tasks", params={"q": "test conversion"})).json()["tasks"]
    }
    assert "my-ab-test" in hit2
    # Every token must match — one absent token excludes the row.
    miss = {
        t["slug"] for t in (await client.get("/tasks", params={"q": "ab nomatchxyz"})).json()["tasks"]
    }
    assert "my-ab-test" not in miss
