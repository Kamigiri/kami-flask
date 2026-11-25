"""kami_flask.sync on plain SQLAlchemy: integer primary keys, `uid` on the wire, request sessions without autoflush.

The app below has its own `SyncState` class, its own `before_flush` listener and handlers that never commit.
"""

import re
import uuid
from datetime import timedelta

import pytest
from flask import Flask, g, jsonify, request
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
    inspect,
    update,
)
from sqlalchemy.orm import Session, backref, declarative_base, relationship, scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from kami_flask.sync import (
    Entity,
    Rejected,
    StampsParent,
    SyncSpec,
    register_sqlalchemy,
    utcnow,
)

Base = declarative_base()


def new_uid() -> str:
    return uuid.uuid4().hex


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    uid = Column(String(32), unique=True, default=new_uid)
    name = Column(String(100), nullable=False)
    change_seq = Column(Integer, nullable=False, default=0)


class Tx(Base):
    __tablename__ = "txs"
    id = Column(Integer, primary_key=True)
    uid = Column(String(32), unique=True, default=new_uid)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    note = Column(String(100), unique=True)
    amount = Column(Integer, nullable=False, default=0)  # not synced: only the derived `kind` is
    kind = Column(String(10), nullable=False, default="zero")
    change_seq = Column(Integer, nullable=False, default=0)
    # passive_deletes="all": deleting an account leaves the rows to the database, the ORM does not load them.
    account = relationship(Account, backref=backref("txs", passive_deletes="all"))


class Tag(Base):
    __tablename__ = "tags"
    id = Column(Integer, primary_key=True)
    uid = Column(String(32), unique=True, default=new_uid)
    name = Column(String(50), nullable=False)
    change_seq = Column(Integer, nullable=False, default=0)


class TxTag(Base):
    """Composite wire id '<tx uid>:<tag uid>'."""

    __tablename__ = "tx_tags"
    tx_id = Column(Integer, ForeignKey("txs.id"), primary_key=True)
    tag_id = Column(Integer, ForeignKey("tags.id"), primary_key=True)
    change_seq = Column(Integer, nullable=False, default=0)
    tx = relationship(Tx, backref="tag_links")
    tag = relationship(Tag)


class Budget(Base):
    __tablename__ = "budgets"
    id = Column(Integer, primary_key=True)
    uid = Column(String(32), unique=True, default=new_uid)
    name = Column(String(50), nullable=False)
    change_seq = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=True, onupdate=utcnow)


class BudgetLine(Base):
    """Not an entity: part of its budget."""

    __tablename__ = "budget_lines"
    id = Column(Integer, primary_key=True)
    budget_id = Column(Integer, ForeignKey("budgets.id"), nullable=False)
    limit = Column("line_limit", Integer, nullable=False, default=0)
    budget = relationship(Budget, backref="lines")


class SyncState(Base):
    """The app's own class of that name (Finance has one for bank sync)."""

    __tablename__ = "sync_states"
    id = Column(Integer, primary_key=True)
    cursor = Column(String(50))


def derive_kind(session, _context, _instances):
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Tx):
            amount = obj.amount or 0
            kind = "zero" if amount == 0 else "in" if amount > 0 else "out"
            if obj.kind != kind:
                obj.kind = kind


event.listen(Session, "before_flush", derive_kind)


def _link_id(link: TxTag) -> str:
    # Must work for a pending link too (only the foreign keys set): that is when its tombstone is removed.
    session = inspect(link).session
    with session.no_autoflush:
        tx = link.tx or session.get(Tx, link.tx_id)
        tag = link.tag or session.get(Tag, link.tag_id)
    return f"{tx.uid}:{tag.uid}"


def _link_where(ident: str):
    tx_uid, tag_uid = ident.split(":")
    return (
        "tx_id = (SELECT id FROM txs WHERE uid = :t) AND tag_id = (SELECT id FROM tags WHERE uid = :g)",
        {"t": tx_uid, "g": tag_uid},
    )


def _handle_tx(ctx, op, uid, fields):
    """Never commits."""
    tx = g.db.query(Tx).filter_by(uid=uid).one_or_none()
    if op == "delete":
        if tx:
            g.db.delete(tx)
        return False
    if tx is None:
        if op == "update":
            raise Rejected("gone", "transaction no longer exists")
        account = g.db.query(Account).filter_by(uid=fields.get("accountUid")).one_or_none()
        if account is None:
            raise Rejected("invalid", "unknown account")
        tx = Tx(uid=uid, account=account)
        g.db.add(tx)
    if "note" in fields:
        tx.note = fields["note"]
    if fields.get("reject"):
        g.db.flush()
        raise Rejected("invalid", "no")
    if fields.get("boom"):
        g.db.flush()
        raise RuntimeError("secret")
    return True


def _serialize_tx(tx):
    return {"id": tx.uid, "accountUid": tx.account.uid, "note": tx.note, "kind": tx.kind}


SPEC = SyncSpec(
    entities=(
        Entity(
            "account",
            Account,
            ("name",),
            "accounts",
            lambda a: {"id": a.uid, "name": a.name},
            id_attr="uid",
            tombstone_children=("txs", "account_id", "tx"),
        ),
        Entity(
            "tx",
            Tx,
            ("account_id", "note", "kind"),
            "transactions",
            _serialize_tx,
            handler=_handle_tx,
            id_attr="uid",
            parent=(Account, "account_id"),
        ),
        Entity("tag", Tag, ("name",), "tags", lambda t: {"id": t.uid, "name": t.name}, id_attr="uid"),
        Entity(
            "txTag",
            TxTag,
            ("tx_id", "tag_id"),
            "txTags",
            lambda link: {"id": _link_id(link)},
            id_of=_link_id,
            id_re=re.compile(r"[0-9a-f]{32}:[0-9a-f]{32}"),
            restamp_where=_link_where,
            restamp_tombstone=True,
            handler=lambda ctx, op, ident, fields: True,
            parent=(Tx, "tx_id"),
        ),
        Entity("budget", Budget, ("name",), "budgets", lambda b: {"id": b.uid, "name": b.name}, id_attr="uid"),
    ),
    stamps_parents=(StampsParent(BudgetLine, "budget", ("limit",)),),
    max_mutations=10,
)


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)  # before register: the bookkeeping tables still appear
    yield engine
    engine.dispose()


@pytest.fixture()
def sessions(engine):
    return sessionmaker(bind=engine, autoflush=False)


@pytest.fixture()
def sync(engine):
    return register_sqlalchemy(Base, lambda: g.db, SPEC, bind=engine, run_after=(derive_kind,))


@pytest.fixture()
def app(sync, sessions):
    app = Flask(__name__)
    app.config.update(TESTING=True)
    sync.init_app(app)

    @app.before_request
    def _open():
        g.db = sessions()

    @app.teardown_request
    def _close(_error):
        db = g.pop("db", None)
        if db is not None:
            db.commit()
            db.close()

    @app.get("/changes")
    def changes():
        return jsonify(sync.changes(request.args.get("since"), request.args.get("limit")))

    @app.post("/mutations")
    def mutations():
        return jsonify(sync.apply_mutations(request.get_json(silent=True)))

    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(sessions, sync):
    with sessions() as session:
        yield session


def tombstones(db, sync):
    return {(t.entity, t.id) for t in db.query(sync.models.Tombstone).populate_existing()}


def seq(db, obj):
    db.refresh(obj)
    return obj.change_seq


def last_seq(db, sync):
    return db.get(sync.models.SyncState, 1, populate_existing=True).last_seq


def make_tx(db, note="n", **kwargs):
    account = Account(name="a")
    tx = Tx(account=account, note=note, **kwargs)
    db.add(tx)
    db.commit()
    return account, tx


def mut(entity, op, ident, fields=None, op_id=None):
    return {"opId": op_id or new_uid(), "entity": entity, "op": op, "id": ident, "fields": fields or {}}


def post(client, *mutations):
    return client.post("/mutations", json={"mutations": list(mutations)}).get_json()


# ---- registration -------------------------------------------------------------------------------------------


def test_register_is_idempotent_per_base_and_creates_tables_on_each_bind(sync, engine):
    other = create_engine("sqlite://")
    assert register_sqlalchemy(Base, lambda: None, SPEC, bind=other) is sync
    assert {"sync_state", "tombstones", "applied_ops"} <= set(inspect(other).get_table_names())
    assert {"sync_state", "sync_states", "tombstones", "applied_ops"} <= set(inspect(engine).get_table_names())


def test_a_later_create_all_also_creates_the_bookkeeping_tables(sync):
    fresh = create_engine("sqlite://")
    Base.metadata.create_all(fresh)
    assert "sync_state" in inspect(fresh).get_table_names()
    with Session(fresh) as session:  # an engine register never saw: stamping works there too
        session.add(Account(name="a"))
        session.commit()
        assert session.query(Account).one().change_seq == 1


def test_the_apps_sync_state_class_does_not_clash(sync, db):
    assert sync.models.SyncState is not SyncState
    assert sync.models.SyncState.__table__.metadata is not Base.metadata
    db.add(SyncState(cursor="bank"))
    db.commit()
    assert db.query(SyncState).one().cursor == "bank"
    assert db.query(sync.models.SyncState).count() == 0  # not a synced model, nothing stamped


def test_run_after_must_already_be_registered(engine):
    other = declarative_base()
    with pytest.raises(ValueError, match="before register_sqlalchemy"):
        register_sqlalchemy(other, lambda: None, SyncSpec(entities=()), bind=engine, run_after=(lambda *a: None,))


def test_session_must_be_a_getter_and_scoped_session_is_one(engine):
    factory = sessionmaker(bind=engine)
    with pytest.raises(TypeError):
        register_sqlalchemy(declarative_base(), factory, SyncSpec(entities=()), bind=engine)
    with pytest.raises(TypeError):
        register_sqlalchemy(declarative_base(), None, SyncSpec(entities=()), bind=engine)
    scoped = scoped_session(factory)
    empty = register_sqlalchemy(declarative_base(), scoped, SyncSpec(entities=()), bind=engine)
    assert empty.changes(None, None) == {"cursor": 0, "hasMore": False, "tombstones": []}
    scoped.remove()


def test_an_unknown_id_attr_fails_at_registration(engine):
    spec = SyncSpec(entities=(Entity("a", Account, ("name",), "as", lambda a: {}, id_attr="nope"),))
    with pytest.raises(ValueError, match="nope"):
        register_sqlalchemy(declarative_base(), lambda: None, spec, bind=engine)


# ---- stamping -----------------------------------------------------------------------------------------------


def test_every_session_stamps(sync, db, engine):
    db.add(Account(name="a"))  # a sessionmaker session
    db.commit()
    with engine.connect() as connection:
        with Session(bind=connection) as direct:  # built by hand, e.g. in a data migration
            direct.add(Account(name="b"))
            direct.commit()
        connection.commit()
    assert [a.change_seq for a in db.query(Account).order_by(Account.id)] == [1, 2]


def test_the_apps_listener_runs_before_the_stamper(sync, db):
    _, tx = make_tx(db)
    before = seq(db, tx)
    tx.amount = -5  # not a synced column; derive_kind turns it into one in the same flush
    db.commit()
    assert seq(db, tx) > before and tx.kind == "out"


def test_parent_stamping_of_non_entity_children(sync, db):
    budget = Budget(name="b")
    db.add(budget)
    db.commit()
    first = seq(db, budget)

    db.add(BudgetLine(budget_id=budget.id, limit=1))  # only the foreign key, no relationship
    db.commit()
    by_key = seq(db, budget)
    assert by_key > first

    db.add(BudgetLine(budget=budget, limit=2))
    db.commit()
    by_relationship = seq(db, budget)
    assert by_relationship > by_key

    line = db.query(BudgetLine).filter_by(limit=1).one()
    line.limit = 10
    db.commit()
    by_change = seq(db, budget)
    assert by_change > by_relationship

    db.delete(db.query(BudgetLine).filter_by(limit=2).one())
    db.commit()
    assert seq(db, budget) > by_change


def test_lines_deleted_with_their_budget_do_not_restamp_it(sync, db):
    budget = Budget(name="b")
    line = BudgetLine(budget=budget)
    db.add(line)
    db.commit()
    uid, before = budget.uid, last_seq(db, sync)
    db.delete(line)
    db.delete(budget)
    db.commit()
    assert tombstones(db, sync) == {("budget", uid)}
    assert last_seq(db, sync) == before + 1


# ---- tombstones with wire ids -------------------------------------------------------------------------------


def test_delete_writes_the_uid_and_recreating_it_removes_the_tombstone(sync, db):
    tag = Tag(name="t")
    db.add(tag)
    db.commit()
    uid = tag.uid
    db.delete(tag)
    db.commit()
    assert tombstones(db, sync) == {("tag", uid)}
    db.add(Tag(uid=uid, name="again"))
    db.commit()
    assert tombstones(db, sync) == set()


def test_tombstone_children_that_were_not_loaded_use_their_uid(sync, db):
    account, tx = make_tx(db)
    account_uid, tx_uid, account_id = account.uid, tx.uid, account.id
    db.expunge_all()
    db.delete(db.get(Account, account_id))  # the child is never loaded: only tombstone_children knows about it
    db.commit()
    assert tombstones(db, sync) == {("account", account_uid), ("tx", tx_uid)}


def test_a_child_deleted_with_its_parent_is_covered_by_the_parent(sync, db):
    _, tx = make_tx(db)
    tag = Tag(name="t")
    link = TxTag(tx=tx, tag=tag)
    db.add(link)
    db.commit()
    tx_uid, before = tx.uid, last_seq(db, sync)
    db.delete(link)
    db.delete(tx)
    db.commit()
    assert tombstones(db, sync) == {("tx", tx_uid)}  # no own tombstone for the link, the integer keys matched
    assert last_seq(db, sync) == before + 1


def test_composite_ids(sync, db):
    _, tx = make_tx(db)
    tag = Tag(name="t")
    db.add(tag)
    db.commit()
    ident = f"{tx.uid}:{tag.uid}"
    db.add(TxTag(tx=tx, tag=tag))
    db.commit()
    db.delete(db.get(TxTag, (tx.id, tag.id)))
    db.commit()
    assert tombstones(db, sync) == {("txTag", ident)}
    db.add(TxTag(tx_id=tx.id, tag_id=tag.id))
    db.commit()
    assert tombstones(db, sync) == set()


# ---- stamp() ------------------------------------------------------------------------------------------------


def test_stamp_for_writes_the_listener_cannot_see(sync, db):
    budget = Budget(name="b")
    db.add(budget)
    db.commit()
    before = seq(db, budget)
    db.execute(update(Budget).values(name="bulk").execution_options(synchronize_session=False))
    db.commit()
    assert seq(db, budget) == before  # the bulk write alone is invisible

    updated_at = budget.updated_at
    result = sync.stamp(db, budget)
    assert result == before + 1 == budget.change_seq
    assert not db.is_modified(budget)
    db.commit()
    assert seq(db, budget) == result and budget.updated_at == updated_at


def test_stamp_rolls_back_with_the_session(sync, db):
    _, tx = make_tx(db)
    before = seq(db, tx)
    sync.stamp(db, tx)
    db.rollback()
    assert seq(db, tx) == before


def test_stamp_children_composite_pending_and_unsynced(sync, db):
    budget = Budget(name="b")
    line = BudgetLine(budget=budget)
    _, tx = make_tx(db)
    link = TxTag(tx=tx, tag=Tag(name="t"))
    db.add_all([line, link])
    db.commit()
    before = seq(db, budget)
    assert sync.stamp(db, line) == seq(db, budget) > before
    assert sync.stamp(db, link) == seq(db, link)
    assert sync.stamp(db, Account(name="pending")) is None
    with pytest.raises(ValueError):
        sync.stamp(db, SyncState())


# ---- feed, 410, prune ---------------------------------------------------------------------------------------


def test_feed_speaks_uids_and_pages(client, db, sync):
    account, tx = make_tx(db, note="x")
    gone = Tag(name="gone")
    db.add(gone)
    db.commit()
    gone_uid = gone.uid
    db.delete(gone)
    db.commit()

    full = client.get("/changes").get_json()
    assert full["accounts"] == [{"id": account.uid, "name": "a"}]
    assert full["transactions"] == [{"id": tx.uid, "accountUid": account.uid, "note": "x", "kind": "zero"}]
    assert full["tombstones"] == [{"entity": "tag", "id": gone_uid}]
    assert full["hasMore"] is False and full["cursor"] == last_seq(db, sync)

    page = client.get("/changes?limit=1").get_json()
    assert page["hasMore"] is True
    rest = client.get(f"/changes?since={page['cursor']}").get_json()
    assert rest["cursor"] == full["cursor"] and rest["hasMore"] is False


def test_410_prune_and_force_resync(app, client, db, sync):
    tag = Tag(name="t")
    db.add_all([tag, Tag(name="u")])
    db.commit()
    db.delete(tag)
    db.commit()
    assert client.get("/changes?since=99").status_code == 410

    tombstone = db.query(sync.models.Tombstone).one()
    tombstone.deleted_at = utcnow() - timedelta(days=91)
    db.add(sync.models.AppliedOp(op_id="old", applied_at=utcnow() - timedelta(days=31)))
    db.commit()
    pruned = tombstone.change_seq
    assert sync.prune(db) == {"tombstones": 1, "appliedOps": 1}  # an explicit session, for a job
    assert client.get("/changes?since=1").status_code == 410
    assert client.get(f"/changes?since={pruned}").status_code == 200

    with app.app_context():
        g.db = db
        assert sync.prune() == {"tombstones": 0, "appliedOps": 0}  # the adapter's session
        floor = sync.force_resync()
    assert client.get(f"/changes?since={pruned}").status_code == 410
    assert client.get("/changes").get_json()["cursor"] == floor


# ---- mutations with handlers that never commit --------------------------------------------------------------


def test_create_update_delete_by_uid_and_duplicates(client, db, sync):
    account = Account(name="a")
    db.add(account)
    db.commit()
    uid = new_uid()
    create = mut("tx", "create", uid, {"accountUid": account.uid, "note": "one"})
    out = post(client, create)
    assert out["results"] == [{"opId": create["opId"], "status": "applied"}]
    tx = db.query(Tx).filter_by(uid=uid).one()
    assert tx.note == "one" and out["cursor"] == last_seq(db, sync) == seq(db, tx)
    assert post(client, create)["results"][0]["status"] == "duplicate"

    assert post(client, mut("tx", "update", uid, {"note": "two"}))["results"][0]["status"] == "applied"
    db.refresh(tx)
    assert tx.note == "two"

    assert post(client, mut("tx", "delete", uid))["results"][0]["status"] == "applied"
    assert ("tx", uid) in tombstones(db, sync)
    db.expunge_all()
    assert db.query(Tx).count() == 0


def test_a_noop_op_restamps_the_row_found_by_uid(client, db):
    _, tx = make_tx(db, note="same")
    before = seq(db, tx)
    assert post(client, mut("tx", "update", tx.uid, {"note": "same"}))["results"][0]["status"] == "applied"
    assert seq(db, tx) > before


def test_restamp_with_composite_id_and_of_its_tombstone(client, db, sync):
    _, tx = make_tx(db)
    link = TxTag(tx=tx, tag=Tag(name="t"))
    db.add(link)
    db.commit()
    ident = _link_id(link)
    before = seq(db, link)
    assert post(client, mut("txTag", "update", ident))["results"][0]["status"] == "applied"
    assert seq(db, link) > before

    db.delete(link)
    db.commit()
    deleted_at = db.query(sync.models.Tombstone).populate_existing().one().change_seq
    assert post(client, mut("txTag", "delete", ident))["results"][0]["status"] == "applied"
    assert db.query(sync.models.Tombstone).populate_existing().one().change_seq > deleted_at


def test_rejected_and_failed_ops_roll_back_and_the_session_stays_usable(client, db, sync):
    account, tx = make_tx(db, note="keep")
    rejected = mut("tx", "create", new_uid(), {"accountUid": account.uid, "note": "r", "reject": True})
    gone = mut("tx", "update", new_uid(), {"note": "x"})
    # The unique note makes the library's own flush fail: that is this op's problem, not a 500.
    clash = mut("tx", "create", new_uid(), {"accountUid": account.uid, "note": "keep"})
    later = mut("tx", "update", tx.uid, {"note": "later"})
    results = post(client, rejected, gone, clash, later)["results"]
    assert [(r["status"], r.get("error")) for r in results] == [
        ("rejected", "invalid"),
        ("rejected", "gone"),
        ("failed", "retry"),
    ]
    boom = mut("tx", "create", new_uid(), {"accountUid": account.uid, "note": "b", "boom": True})
    results = post(client, boom)["results"]
    assert results[0]["status"] == "failed" and "secret" not in str(results)
    db.expunge_all()
    assert [t.note for t in db.query(Tx)] == ["keep"]
    assert db.get(sync.models.AppliedOp, clash["opId"]) is None
    assert db.get(sync.models.AppliedOp, rejected["opId"]) is not None
