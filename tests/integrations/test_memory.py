"""Memory proposal, privacy, ownership, and provider behavior."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.ai.base import NotConfigured
from app.context import build_context
from app.models import Memory
from app.routers import memory
from fastapi import HTTPException


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows=(), *, owner_id=None):
        self.rows = {row.id: row for row in rows}
        self.query_rows = list(rows)
        self.owner_id = owner_id
        self.statements = []
        self.commits = 0

    def add(self, row):
        self.rows[row.id] = row
        self.query_rows.append(row)

    async def get(self, _model, row_id):
        return self.rows.get(row_id)

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params or {}))
        rows = list(self.query_rows) if "FROM memories" in sql else []
        if "memories.user_id =" in sql and self.owner_id is not None:
            rows = [row for row in rows if row.user_id == self.owner_id]
        if "memories.status =" in sql:
            rows = [row for row in rows if row.status == "confirmed"]
        if "memories.privacy_class =" in sql:
            rows = [row for row in rows if row.privacy_class == "normal"]
        if "memories.active IS true" in sql:
            rows = [row for row in rows if row.active]
        if "memories.deleted_at IS NULL" in sql:
            rows = [row for row in rows if row.deleted_at is None]
        if "memories.embedding IS NOT NULL" in sql:
            rows = [row for row in rows if row.embedding is not None]
        return _Result(rows)

    async def commit(self):
        self.commits += 1


def _memory(
    user_id,
    *,
    status="confirmed",
    privacy="normal",
    active=True,
    content="memory",
    embedding=None,
    deleted_at=None,
):
    now = datetime.now(UTC)
    return Memory(
        id=uuid.uuid4(),
        user_id=user_id,
        category="profile",
        content=content,
        status=status,
        privacy_class=privacy,
        active=active,
        embedding=embedding,
        deleted_at=deleted_at,
        created_at=now,
        updated_at=now,
    )


def _user(user_id):
    return SimpleNamespace(id=user_id)


class _EmbeddingProvider:
    def __init__(self):
        self.calls = []
        self.requests = []
        self._client = SimpleNamespace(embeddings=SimpleNamespace(create=self._create))

    async def _create(self, **request):
        self.calls.append(request["input"])
        self.requests.append(request)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.25] + [0.0] * 1_535)])


@pytest.mark.asyncio
async def test_create_update_delete_memory_crud():
    owner_id = uuid.uuid4()
    session = _Session(owner_id=owner_id)

    created = await memory.create(
        memory.MemoryIn(category="profile", content="Initial"), _user(owner_id), session
    )
    row = session.rows[uuid.UUID(created["id"])]
    assert created["status"] == "proposed"
    assert row.active is False
    assert row.privacy_class == "sensitive"
    assert row.embedding is None

    updated = await memory.update(
        row.id, memory.MemoryPatch(content="Updated", active=True), _user(owner_id), session
    )
    assert updated["content"] == "Updated"
    assert updated["status"] == "proposed"

    assert await memory.delete(row.id, _user(owner_id), session) == {"deleted": True}
    assert await memory.delete(row.id, _user(owner_id), session) == {"deleted": True}
    assert row.deleted_at is not None
    assert row.active is False


@pytest.mark.asyncio
async def test_memory_list_returns_confirmed_rows_only_for_owner():
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    confirmed = _memory(owner_id, content="confirmed")
    proposed = _memory(owner_id, status="proposed", content="proposed")
    deleted = _memory(owner_id, deleted_at=datetime.now(UTC), content="deleted")
    foreign = _memory(other_id, content="foreign")
    session = _Session([confirmed, proposed, deleted, foreign], owner_id=owner_id)

    result = await memory.list_memories(_user(owner_id), session)

    assert [item["content"] for item in result["memories"]] == ["confirmed"]
    sql = session.statements[0][0]
    assert "memories.status =" in sql
    assert "memories.user_id =" in sql


@pytest.mark.asyncio
async def test_context_excludes_proposed_sensitive_inactive_and_deleted_memories():
    owner_id = uuid.uuid4()
    eligible = _memory(owner_id, content="safe normal detail")
    candidates = [
        eligible,
        _memory(owner_id, status="proposed", content="proposed detail"),
        _memory(owner_id, privacy="sensitive", content="sensitive detail"),
        _memory(owner_id, active=False, content="inactive detail"),
        _memory(owner_id, deleted_at=datetime.now(UTC), content="deleted detail"),
    ]
    session = _Session(candidates, owner_id=owner_id)
    conversation = SimpleNamespace(id=uuid.uuid4(), user_id=owner_id, summary=None)

    context = await build_context(session, conversation)

    combined = "\n".join(item["content"] for item in context)
    assert "safe normal detail" in combined
    for private_content in (
        "proposed detail",
        "sensitive detail",
        "inactive detail",
        "deleted detail",
    ):
        assert private_content not in combined
    memory_sql = next(sql for sql, _ in session.statements if "FROM memories" in sql)
    assert "memories.status =" in memory_sql
    assert "memories.privacy_class =" in memory_sql


@pytest.mark.asyncio
async def test_confirm_and_reject_are_owner_scoped_and_repeat_safe(monkeypatch):
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    proposed = _memory(owner_id, status="proposed", active=False)
    session = _Session([proposed], owner_id=owner_id)
    provider = _EmbeddingProvider()
    monkeypatch.setattr(memory, "get_model", lambda: provider)

    first = await memory.confirm_memory(proposed.id, _user(owner_id), session)
    second = await memory.confirm_memory(proposed.id, _user(owner_id), session)
    assert first["status"] == second["status"] == "confirmed"
    assert session.commits == 1
    assert provider.calls == [proposed.content]

    assert await memory.reject_memory(proposed.id, _user(owner_id), session) == {"rejected": True}
    assert await memory.reject_memory(proposed.id, _user(owner_id), session) == {"rejected": True}
    assert proposed.deleted_at is not None
    assert proposed.active is False
    assert proposed.embedding is None
    assert session.commits == 2

    foreign = _memory(owner_id, status="proposed", active=False)
    foreign_session = _Session([foreign])
    for decision in (memory.confirm_memory, memory.reject_memory):
        with pytest.raises(HTTPException) as caught:
            await decision(foreign.id, _user(other_id), foreign_session)
        assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_confirm_embeds_normal_memory_but_never_sensitive_memory(monkeypatch):
    owner_id = uuid.uuid4()
    normal = _memory(owner_id, status="proposed", active=False, content="ordinary fact")
    sensitive = _memory(
        owner_id, status="proposed", privacy="sensitive", active=False, content="private fact"
    )
    reclassified = _memory(
        owner_id,
        content="now sensitive",
        embedding=memory._vector_literal([0.1] + [0.0] * 1_535),
    )
    session = _Session([normal, sensitive, reclassified], owner_id=owner_id)
    provider = _EmbeddingProvider()
    monkeypatch.setattr(memory, "get_model", lambda: provider)

    await memory.confirm_memory(normal.id, _user(owner_id), session)
    await memory.confirm_memory(sensitive.id, _user(owner_id), session)
    await memory.update(
        reclassified.id,
        memory.MemoryPatch(privacy_class="sensitive"),
        _user(owner_id),
        session,
    )

    assert provider.calls == ["ordinary fact"]
    assert provider.requests[0] == {
        "model": "text-embedding-3-small",
        "input": "ordinary fact",
        "dimensions": 1_536,
    }
    assert normal.embedding.startswith("[0.25,0.0")
    assert sensitive.embedding is None
    assert reclassified.embedding is None


@pytest.mark.asyncio
async def test_confirm_preserves_or_applies_explicit_privacy_choice(monkeypatch):
    owner_id = uuid.uuid4()
    omitted = _memory(owner_id, status="proposed", privacy="sensitive", active=False)
    normal = _memory(
        owner_id, status="proposed", privacy="sensitive", active=False, content="ordinary fact"
    )
    sensitive = _memory(
        owner_id,
        status="proposed",
        privacy="sensitive",
        active=False,
        embedding=memory._vector_literal([0.1] + [0.0] * 1_535),
    )
    session = _Session([omitted, normal, sensitive], owner_id=owner_id)
    provider = _EmbeddingProvider()
    monkeypatch.setattr(memory, "get_model", lambda: provider)

    await memory.confirm_memory(omitted.id, _user(owner_id), session)
    await memory.confirm_memory(
        normal.id,
        _user(owner_id),
        session,
        body=memory.MemoryConfirmation(privacy_class="normal"),
    )
    await memory.confirm_memory(
        sensitive.id,
        _user(owner_id),
        session,
        body=memory.MemoryConfirmation(privacy_class="sensitive"),
    )

    assert omitted.privacy_class == "sensitive"
    assert omitted.status == "confirmed" and omitted.active is True
    assert omitted.embedding is None
    assert normal.privacy_class == "normal"
    assert normal.status == "confirmed" and normal.active is True
    assert normal.embedding.startswith("[0.25,0.0")
    assert sensitive.privacy_class == "sensitive"
    assert sensitive.embedding is None
    assert provider.calls == ["ordinary fact"]
    assert session.commits == 3


@pytest.mark.asyncio
async def test_search_uses_only_confirmed_active_normal_non_deleted_memories(monkeypatch):
    owner_id = uuid.uuid4()
    matched = _memory(
        owner_id,
        content="semantic match",
        embedding=memory._vector_literal([0.1] + [0.0] * 1_535),
    )
    candidates = [
        matched,
        _memory(
            owner_id,
            privacy="sensitive",
            content="sensitive",
            embedding=matched.embedding,
        ),
        _memory(
            owner_id,
            status="proposed",
            content="proposed",
            embedding=matched.embedding,
        ),
        _memory(
            owner_id,
            active=False,
            content="inactive",
            embedding=matched.embedding,
        ),
        _memory(
            owner_id,
            deleted_at=datetime.now(UTC),
            content="deleted",
            embedding=matched.embedding,
        ),
    ]
    session = _Session(candidates, owner_id=owner_id)
    provider = _EmbeddingProvider()
    monkeypatch.setattr(memory, "get_model", lambda: provider)

    result = await memory.list_memories(_user(owner_id), session, q="related idea")

    assert [item["content"] for item in result["memories"]] == ["semantic match"]
    assert provider.calls == ["related idea"]
    assert provider.requests[0]["dimensions"] == 1_536
    sql, params = session.statements[0]
    assert "<=> CAST(:embedding AS vector)" in sql
    assert "memories.status =" in sql
    assert "memories.privacy_class =" in sql
    assert "memories.active IS true" in sql
    assert "memories.deleted_at IS NULL" in sql
    assert params["embedding"].startswith("[0.25,0.0")


@pytest.mark.asyncio
async def test_missing_embedding_provider_keeps_confirm_and_keyword_search_working(monkeypatch):
    owner_id = uuid.uuid4()
    proposed = _memory(owner_id, status="proposed", active=False, content="keyword fact")
    session = _Session([proposed], owner_id=owner_id)

    def missing_provider():
        raise NotConfigured("No model provider credential is set.")

    monkeypatch.setattr(memory, "get_model", missing_provider)
    confirmed = await memory.confirm_memory(proposed.id, _user(owner_id), session)
    result = await memory.list_memories(_user(owner_id), session, q="keyword")

    assert confirmed["status"] == "confirmed"
    assert proposed.embedding is None
    assert [item["content"] for item in result["memories"]] == ["keyword fact"]
    assert len(session.statements) == 1
    assert "<=>" not in session.statements[0][0]
    assert "LIKE lower(" in session.statements[0][0]


@pytest.mark.asyncio
async def test_proposed_memory_update_does_not_send_content_to_embedding(monkeypatch):
    owner_id = uuid.uuid4()
    proposed = _memory(owner_id, status="proposed", active=False, content="private draft")
    session = _Session([proposed], owner_id=owner_id)
    provider = _EmbeddingProvider()
    monkeypatch.setattr(memory, "get_model", lambda: provider)

    await memory.update(
        proposed.id, memory.MemoryPatch(content="updated draft"), _user(owner_id), session
    )

    assert provider.calls == []
    assert proposed.embedding is None


def test_memory_migration_fails_closed_for_legacy_rows_and_validates_check_online():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "database/migrations/versions/revisions_0007_0009/0010_audited_schema_gaps.py"
    )
    migration = migration_path.read_text(encoding="utf-8")
    memory_status_column = migration.split('"status",', 1)[1].split("),", 1)[0]

    assert 'server_default="proposed"' in memory_status_column
    assert "nullable=False" in memory_status_column
    assert 'op.alter_column("memories", "privacy_class", server_default="sensitive")' in migration
    assert "UPDATE memories SET status = 'proposed', privacy_class = 'sensitive'" in migration
    assert "embedding = NULL" in migration
    assert "NOT VALID" in migration
    assert "VALIDATE CONSTRAINT memories_status_valid" in migration
    assert 'server_default="normal"' not in migration


def test_memory_model_defaults_are_fail_closed():
    assert str(Memory.__table__.c.status.server_default.arg) == "proposed"
    assert str(Memory.__table__.c.privacy_class.server_default.arg) == "sensitive"


def test_memory_input_defaults_sensitive_and_accepts_explicit_normal():
    assert memory.MemoryIn(category="profile", content="Fact").privacy_class == "sensitive"
    assert (
        memory.MemoryIn(
            category="profile", content="Fact", privacy_class="normal"
        ).privacy_class
        == "normal"
    )


@pytest.mark.asyncio
async def test_existing_sensitive_memory_requires_explicit_normal_reclassification(
    monkeypatch,
):
    owner_id = uuid.uuid4()
    row = _memory(owner_id, content="explicitly classified fact", privacy="sensitive")
    session = _Session([row], owner_id=owner_id)
    provider = _EmbeddingProvider()
    monkeypatch.setattr(memory, "get_model", lambda: provider)

    await memory.update(row.id, memory.MemoryPatch(), _user(owner_id), session)
    assert row.privacy_class == "sensitive"
    assert provider.calls == []

    await memory.update(
        row.id, memory.MemoryPatch(privacy_class="normal"), _user(owner_id), session
    )
    assert row.privacy_class == "normal"
    assert provider.calls == ["explicitly classified fact"]


@pytest.mark.asyncio
async def test_embeddings_require_confirmed_active_non_deleted_normal_memory(monkeypatch):
    owner_id = uuid.uuid4()
    vector = memory._vector_literal([0.1] + [0.0] * 1_535)
    eligible = _memory(owner_id, content="eligible fact")
    ineligible = [
        _memory(owner_id, status="proposed", embedding=vector),
        _memory(owner_id, privacy="sensitive", embedding=vector),
        _memory(owner_id, active=False, embedding=vector),
        _memory(owner_id, deleted_at=datetime.now(UTC), embedding=vector),
    ]
    provider = _EmbeddingProvider()
    monkeypatch.setattr(memory, "get_model", lambda: provider)

    await memory._set_embedding(eligible)
    for row in ineligible:
        await memory._set_embedding(row)

    assert provider.calls == ["eligible fact"]
    assert eligible.embedding.startswith("[0.25,0.0")
    assert all(row.embedding is None for row in ineligible)
