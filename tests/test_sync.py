"""kami_flask.sync: the registry with a tiny two-entity app (a parent `note` list and a child `item`)."""

import uuid
from datetime import timedelta

import pytest
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

from kami_flask.sync import (
    Entity,
    Rejected,
    StaleCursor,
    StampsParent,
    SyncError,
    SyncSpec,
    models_for,
    register,
    utcnow,
)

db = SQLAlchemy()


def new_id() -> str:
    return uuid.uuid4().hex


class Box(db.Model):
    __tablename__ = "boxes"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    title = db.Column(db.String(100), nullable=False)
    touched = db.Column(db.Integer, default=0)  # bookkeeping: not a synced column
    change_seq = db.Column(db.Integer, nullable=False, default=0, index=True)


class Item(db.Model):
    __tablename__ = "items"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    box_id = db.Column(db.String(32), db.ForeignKey("boxes.id"), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    change_seq = db.Column(db.Integer, nullable=False, default=0, index=True)


class Part(db.Model):
    """Not an entity: a change restamps its item."""

    __tablename__ = "parts"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.String(32), db.ForeignKey("items.id"), nullable=False)
    name = db.Column(db.String(100))
    item = db.relationship(Item, backref="parts")


def _validate_item(op, _id, fields):
    unknown = set(fields) - {"boxId", "label"}
    if unknown:
        raise Rejected("invalid", f"Unknown fields: {', '.join(sorted(unknown))}")
    if op == "create" and "boxId" not in fields:
        raise Rejected("invalid", "create needs a boxId")


class HttpError(Exception):
    def __init__(self, status_code: int, message: str = "x") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _handle_item(ctx, op, item_id, fields):
    ctx["calls"].append((op, item_id))
    item = db.session.get(Item, item_id)
    if fields.get("label") == "boom":
        raise RuntimeError("database is busy: secret")
    if fields.get("label") == "teapot":
        raise HttpError(418)
    if fields.get("label") == "missing":
        raise HttpError(404, "box missing")
    if op == "delete":
        if item:
            db.session.delete(item)
            db.session.commit()
        return False
    if item is None:
        if op == "update":
            raise Rejected("gone", "item no longer exists")
        db.session.add(Item(id=item_id, box_id=fields["boxId"], label=fields.get("label", "")))
    elif "label" in fields:
        item.label = fields["label"]
    db.session.commit()
    return True  # restamp (a no-op update must still come back to the client)


SPEC = SyncSpec(
    entities=(
        Entity(
            "box",
            Box,
            ("title",),
            "boxes",
            lambda b: {"id": b.id, "title": b.title},
            tombstone_children=("items", "box_id", "item"),
            read_only_message="boxes are read-only for clients",
        ),
        Entity(
            "item",
            Item,
            ("box_id", "label"),
            "items",
            lambda i: {"id": i.id, "boxId": i.box_id, "label": i.label},
            handler=_handle_item,
            validate=_validate_item,
            parent=(Box, "box_id"),
        ),
    ),
    stamps_parents=(StampsParent(Part, "item", ("name",)),),
    context=lambda: {"calls": []},
    max_mutations=5,
)
sync = register(db, SPEC)


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
    db.init_app(app)
    sync.init_app(app)

    @app.get("/changes")
    def changes():
        return jsonify(sync.changes(request.args.get("since"), request.args.get("limit")))

    @app.post("/mutations")
    def mutations():
        return jsonify(sync.apply_mutations(request.get_json(silent=True)))

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def mut(entity, op, ident, fields=None, op_id=None):
    return {"opId": op_id or new_id(), "entity": entity, "op": op, "id": ident, "fields": fields or {}}


def seq_of(obj):
    return db.session.get(type(obj), obj.id).change_seq


def test_register_is_idempotent_and_models_are_cached():
    assert register(db, SPEC) is sync
    assert models_for(db) is models_for(db)
    assert models_for(db).Tombstone.__tablename__ == "tombstones"
    assert models_for(db).SyncState.__tablename__ == "sync_state"
    assert models_for(db).AppliedOp.__tablename__ == "applied_ops"


def test_spec_rejects_duplicate_names_and_reserved_feed_key():
    entity = SPEC.entities[0]
    with pytest.raises(ValueError):
        SyncSpec(entities=(entity, entity))
    with pytest.raises(ValueError):
        SyncSpec(entities=(Entity("x", Box, ("title",), "tombstones", lambda b: {}),))
    with pytest.raises(ValueError):
        SyncSpec(entities=(Entity("x", Box, ("title",), "xs", lambda b: {}, ops=("upsert",)),))


def test_new_and_changed_rows_get_increasing_seqs(app):
    box = Box(title="a")
    db.session.add(box)
    db.session.commit()
    first = seq_of(box)
    assert first == 1
    box.title = "b"
    db.session.commit()
    assert seq_of(box) == 2
    box.title = "b"  # same value
    box.touched = 5  # not a synced column
    db.session.commit()
    assert seq_of(box) == 2


def test_collection_and_stamps_parent_restamp_the_owner(app):
    box = Box(title="a")
    db.session.add(box)
    db.session.flush()
    item = Item(box_id=box.id, label="i")
    db.session.add(item)
    db.session.commit()
    before = seq_of(item)
    db.session.add(Part(item=item, name="p"))
    db.session.commit()
    after_add = seq_of(item)
    assert after_add > before
    part = Part.query.one()
    part.name = "q"
    db.session.commit()
    assert seq_of(item) > after_add


def test_deleting_a_parent_tombstones_children_that_were_not_loaded(app):
    box = Box(title="a")
    db.session.add(box)
    db.session.flush()
    item = Item(box_id=box.id, label="i")
    db.session.add(item)
    db.session.commit()
    box_id, item_id = box.id, item.id
    db.session.delete(db.session.get(Box, box_id))  # the item is not in the session: tombstone_children asks the DB
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    db.session.commit()
    tombs = {(t.entity, t.id) for t in models_for(db).Tombstone.query.all()}
    assert tombs == {("box", box_id), ("item", item_id)}


def test_child_deleted_with_its_parent_gets_no_own_tombstone(app):
    box = Box(title="a")
    db.session.add(box)
    db.session.flush()
    item = Item(box_id=box.id, label="i")
    db.session.add(item)
    db.session.commit()
    db.session.delete(item)
    db.session.delete(box)
    db.session.commit()
    tombs = {(t.entity, t.id) for t in models_for(db).Tombstone.query.all()}
    assert ("box", box.id) in tombs
    assert ("item", item.id) in tombs  # from tombstone_children, once (parent covers the child's own)
    assert len(tombs) == 2


def test_recreating_a_deleted_id_removes_its_tombstone(app):
    box = Box(title="a")
    db.session.add(box)
    db.session.commit()
    bid = box.id
    db.session.delete(box)
    db.session.commit()
    assert models_for(db).Tombstone.query.count() == 1
    db.session.add(Box(id=bid, title="again"))
    db.session.commit()
    assert models_for(db).Tombstone.query.count() == 0


def test_feed_pages_merge_entities_and_tombstones_in_seq_order(app):
    c = app.test_client()
    box = Box(title="a")
    db.session.add(box)
    db.session.flush()
    db.session.add(Item(box_id=box.id, label="i"))
    gone = Box(title="x")
    db.session.add(gone)
    db.session.commit()
    db.session.delete(gone)
    db.session.commit()

    full = c.get("/changes").get_json()
    assert [b["title"] for b in full["boxes"]] == ["a"]
    assert [i["label"] for i in full["items"]] == ["i"]
    assert full["tombstones"] == [{"entity": "box", "id": gone.id}]
    assert full["hasMore"] is False
    last = full["cursor"]

    page = c.get("/changes?limit=2").get_json()
    assert page["hasMore"] is True
    assert len(page["boxes"]) + len(page["items"]) + len(page["tombstones"]) == 2
    rest = c.get(f"/changes?since={page['cursor']}").get_json()
    assert rest["cursor"] == last and rest["hasMore"] is False
    # Caught up: the cursor never moves backwards.
    assert c.get(f"/changes?since={last}").get_json()["cursor"] == last


def test_cursor_errors(app):
    c = app.test_client()
    assert c.get("/changes?since=abc").status_code == 400
    assert c.get("/changes?since=-1").status_code == 400
    db.session.add(Box(title="a"))
    db.session.commit()
    assert c.get("/changes?since=99").status_code == 410  # newer than the server
    with pytest.raises(StaleCursor):
        sync.changes("99", None)
    assert issubclass(StaleCursor, SyncError)


def test_prune_raises_pruned_below_and_410s_older_cursors(app):
    c = app.test_client()
    db.session.add_all([Box(title="a"), Box(title="b")])
    db.session.commit()
    gone = Box(title="c")
    db.session.add(gone)
    db.session.commit()
    db.session.delete(gone)
    db.session.commit()
    tomb = models_for(db).Tombstone.query.one()
    tomb.deleted_at = utcnow() - timedelta(days=91)
    models_for(db).AppliedOp.query.delete()
    db.session.add(models_for(db).AppliedOp(op_id="old", applied_at=utcnow() - timedelta(days=31)))
    db.session.add(models_for(db).AppliedOp(op_id="new"))
    db.session.commit()
    seq = tomb.change_seq
    assert sync.prune(db.session) == {"tombstones": 1, "appliedOps": 1}
    assert db.session.get(models_for(db).SyncState, 1).pruned_below == seq
    assert c.get("/changes?since=1").status_code == 410
    assert c.get(f"/changes?since={seq}").status_code == 200


def test_force_resync_jumps_the_counter(app):
    c = app.test_client()
    db.session.add(Box(title="a"))
    db.session.commit()
    floor = sync.force_resync(db.session)
    state = db.session.get(models_for(db).SyncState, 1)
    assert floor == state.pruned_below == state.last_seq and floor > 1_000_000
    assert c.get("/changes?since=1").status_code == 410
    assert c.get("/changes").get_json()["cursor"] == floor  # a full pull ends exactly at the floor


def test_mutations_statuses_and_duplicates(app):
    c = app.test_client()
    box = Box(title="a")
    db.session.add(box)
    db.session.commit()
    item_id = new_id()
    op = new_id()
    body = {"mutations": [mut("item", "create", item_id, {"boxId": box.id, "label": "x"}, op)]}
    result = c.post("/mutations", json=body).get_json()
    assert result["results"] == [{"opId": op, "status": "applied"}]
    assert db.session.get(Item, item_id).label == "x"
    assert result["cursor"] == db.session.get(models_for(db).SyncState, 1).last_seq
    again = c.post("/mutations", json=body).get_json()
    assert again["results"] == [{"opId": op, "status": "duplicate"}]


def test_rejections_are_remembered_and_do_not_stop_the_batch(app):
    c = app.test_client()
    batch = [
        mut("item", "update", new_id(), {"label": "x"}),  # handler: gone
        mut("item", "create", new_id(), {"label": "no box"}),  # validate: invalid
        mut("item", "create", new_id(), {"boxId": "z" * 32, "nope": 1}),  # unknown field
        mut("box", "create", new_id(), {"title": "t"}),  # read-only
        mut("thing", "create", new_id()),  # unknown entity
        mut("item", "merge", new_id()),  # unknown op
        mut("item", "update", "not-hex", {}),  # bad id
        {"opId": "bad op id", "entity": "item"},
        "nonsense",
        mut("item", "update", new_id(), {"label": "missing"}),  # a 404 from the handler
    ]
    results = []
    for chunk in (batch[:5], batch[5:]):
        results += c.post("/mutations", json={"mutations": chunk}).get_json()["results"]
    got = [(r["status"], r.get("error")) for r in results]
    assert got == [
        ("rejected", "gone"),
        ("rejected", "invalid"),
        ("rejected", "invalid"),
        ("rejected", "unsupported"),
        ("rejected", "invalid"),
        ("rejected", "invalid"),
        ("rejected", "invalid"),
        ("rejected", "invalid"),
        ("rejected", "invalid"),
        ("rejected", "gone"),
    ]
    assert "read-only" in results[3]["message"]
    # A rejected op with a valid opId is remembered, so a replay is a duplicate.
    first = batch[0]["opId"]
    replay = c.post("/mutations", json={"mutations": [batch[0]]}).get_json()["results"][0]
    assert replay == {"opId": first, "status": "duplicate"}


def test_failed_stops_the_batch_and_hides_the_error(app):
    c = app.test_client()
    box = Box(title="a")
    db.session.add(box)
    db.session.commit()
    boom = mut("item", "create", new_id(), {"boxId": box.id, "label": "boom"})
    after = mut("item", "create", new_id(), {"boxId": box.id, "label": "later"})
    results = c.post("/mutations", json={"mutations": [boom, after]}).get_json()["results"]
    assert [r["status"] for r in results] == ["failed"]
    assert "secret" not in str(results)
    assert db.session.get(models_for(db).AppliedOp, boom["opId"]) is None  # retried later
    # A status outside rejected_status (418) is temporary too.
    teapot = mut("item", "create", new_id(), {"boxId": box.id, "label": "teapot"})
    assert c.post("/mutations", json={"mutations": [teapot]}).get_json()["results"][0]["status"] == "failed"


def test_noop_update_still_restamps_the_row(app):
    c = app.test_client()
    box = Box(title="a")
    db.session.add(box)
    db.session.flush()
    item = Item(box_id=box.id, label="same")
    db.session.add(item)
    db.session.commit()
    before = seq_of(item)
    c.post("/mutations", json={"mutations": [mut("item", "update", item.id, {"label": "same"})]})
    db.session.expire_all()
    assert seq_of(item) > before


def test_delete_through_a_mutation_writes_a_tombstone(app):
    c = app.test_client()
    box = Box(title="a")
    db.session.add(box)
    db.session.flush()
    item = Item(box_id=box.id, label="x")
    db.session.add(item)
    db.session.commit()
    item_id = item.id
    out = c.post("/mutations", json={"mutations": [mut("item", "delete", item_id)]}).get_json()
    assert out["results"][0]["status"] == "applied"
    assert db.session.get(models_for(db).Tombstone, ("item", item_id)) is not None


def test_request_level_errors(app):
    c = app.test_client()
    assert c.post("/mutations", json={"nope": 1}).status_code == 400
    assert c.post("/mutations", data="x", content_type="text/plain").status_code == 400
    too_many = {"mutations": [mut("item", "delete", new_id()) for _ in range(6)]}
    response = c.post("/mutations", json=too_many)
    assert response.status_code == 400 and "At most 5" in response.get_json()["error"]
