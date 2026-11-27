"""Pre-customer hardening (teamshift #4633): the sample price book never reaches a customer, the accept page takes only
Hermes profile names and always answers with its own page, and typed /quote and /pricebook changes are the owner's."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import commands, demo, invoices, owner, runtime, service as svc  # noqa: E402
from docket import pricebook as pb  # noqa: E402
from docket import pbedit as pe  # noqa: E402
from docket.accept_server import AcceptApp, Target  # noqa: E402
from docket.store import Store  # noqa: E402

DEMO = Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml"
PUBLIC = "https://portal.example"
JOB = "Dana Henderson, 14 Maple St 62707, water heater, permit, haul away"


@pytest.fixture
def sample_mark(monkeypatch):
    """The suite runs with the sample mark off (conftest); these tests turn it back on."""
    monkeypatch.setattr(pb, "SAMPLE_MARK", "demo")
    monkeypatch.setattr(pb, "SAMPLE_BUSINESS", "Copperline Plumbing & Drain")


@pytest.fixture
def store():
    return Store(sqlite3.connect(":memory:", check_same_thread=False))


def sample_book():
    book, errors = pb.load_pricebook(DEMO)
    assert not errors
    return book


def own_book():
    """The sample's prices under the owner's own name and without the mark."""
    return pb.parse_pricebook(DEMO.read_text().replace("demo: true\n", "").replace("Copperline Plumbing & Drain", "Rivera Plumbing"))


def test_the_bundled_book_is_marked_and_quotes_from_it_carry_the_mark(sample_mark, store):
    book = sample_book()
    assert book.demo
    rec = svc.draft_from_words(store, book, JOB).record
    assert rec.quote["demo_pricebook"] is True


def test_a_sample_priced_quote_is_never_sent(sample_mark, store, tmp_path):
    book = sample_book()
    rec = svc.draft_from_words(store, book, JOB).record
    with pytest.raises(svc.ServiceError, match="sample price book"):
        svc.send_quote(store, book, rec.id, outbox_root=tmp_path, actor="owner", base_url=PUBLIC)
    assert store.get(rec.id).status == "draft" and not list(tmp_path.iterdir())
    assert not store.has_send(rec.id, rec.version, "outbox")


def test_a_local_try_out_still_sends_from_the_sample(sample_mark, store, tmp_path):
    book = sample_book()
    rec = svc.draft_from_words(store, book, JOB).record
    result = svc.send_quote(store, book, rec.id, outbox_root=tmp_path, actor="owner", base_url="http://127.0.0.1:8787")
    assert result["ok"] and store.get(rec.id).status == "sent"


def test_the_deployment_link_makes_sample_sends_refused(sample_mark, store, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKET_PUBLIC_LINK", PUBLIC)
    book = sample_book()
    rec = svc.draft_from_words(store, book, JOB).record
    with pytest.raises(svc.ServiceError, match="sample price book"):
        svc.send_quote(store, book, rec.id, outbox_root=tmp_path, actor="owner")
    assert svc.reaches_customers(store)


def test_a_draft_priced_from_the_sample_stays_refused_after_switching_books(sample_mark, store, tmp_path):
    rec = svc.draft_from_words(store, sample_book(), JOB).record
    own, errors = own_book()
    assert not errors and not own.demo
    with pytest.raises(svc.ServiceError, match="sample price book"):
        svc.send_quote(store, own, rec.id, outbox_root=tmp_path, actor="owner", base_url=PUBLIC)
    revised = svc.revise_from_words(store, own, rec.id, "add a shutoff valve").record   # repriced from the owner's book
    assert "demo_pricebook" not in revised.quote
    assert svc.send_quote(store, own, rec.id, outbox_root=tmp_path, actor="owner", base_url=PUBLIC)["ok"]


def test_an_owner_book_quote_is_refused_while_the_sample_is_current(sample_mark, store, tmp_path):
    own, _ = own_book()
    rec = svc.draft_from_words(store, own, JOB).record
    with pytest.raises(svc.ServiceError, match="sample price book"):
        svc.send_quote(store, sample_book(), rec.id, outbox_root=tmp_path, actor="owner", base_url=PUBLIC)


def test_editing_the_sample_book_keeps_the_mark_and_a_new_book_does_not(sample_mark, store, tmp_path):
    own = tmp_path / "pricebook.yaml"
    pe.propose(store, DEMO, "labor 150/hr")
    pe.save(store, own, DEMO)
    edited, errors = pb.load_pricebook(own)
    assert not errors and edited.labor_rate == 150 and edited.demo
    fresh = pe.propose(store, own, "Rivera Plumbing, labor 120/hr, tax 7%", fresh=True)
    started, errors = pb.parse_pricebook(fresh.text)
    assert not errors and not started.demo


def test_the_demo_company_still_loads(sample_mark, store, tmp_path):
    made = demo.seed(store, sample_book(), tmp_path)
    assert len(made) == len(demo.SEEDS)


def test_no_invoice_from_the_sample_book(sample_mark, store, tmp_path):
    book = sample_book()
    demo.seed(store, book, tmp_path)
    done = next(r for r in store.list() if r.status == "done")
    store.set_meta("public_base_url", PUBLIC)
    with pytest.raises(invoices.InvoiceError, match="sample price book"):
        invoices.close_out(store, book, done.id, outbox_root=tmp_path)
    assert store.get(done.id).status == "done" and invoices.get_invoice(store, done.id) is None
    store.set_meta("public_base_url", "")                             # a local try-out still invoices
    assert invoices.close_out(store, book, done.id, outbox_root=tmp_path)["number"]


# ---------------------------------------------------------------- accept page

class Boom(RuntimeError):
    pass


def app_with(resolve):
    return AcceptApp(lambda: None, lambda: None, resolve=resolve)


@pytest.mark.parametrize("name", ["..", ".", "Owner", "a" * 65, "-x", "a.b", "%2e%2e", "a b"])
def test_a_non_profile_name_never_reaches_the_resolver(name):
    seen = []
    app = app_with(lambda n: seen.append(n))
    status, _, _ = app.handle("GET", f"/p/{name}/q/abcdefghijklmnopqrstuv", {})
    assert status == 404 and seen == []


def test_a_valid_profile_name_reaches_the_resolver():
    seen = []
    app = app_with(lambda n: seen.append(n))
    assert app.handle("GET", "/p/crew-2/q/abcdefghijklmnopqrstuv", {})[0] == 404   # resolver said: no such profile
    assert seen == ["crew-2"]


def test_an_unexpected_error_is_a_page_not_a_dropped_connection(store, monkeypatch):
    def explode(*a, **k):
        raise Boom("crafted")
    monkeypatch.setattr(svc, "customer_action", explode)
    app = app_with(lambda n: Target(store, None, None, None))
    status, ctype, body = app.handle("GET", "/q/abcdefghijklmnopqrstuv", {})
    assert status == 503 and ctype == "text/html" and b"Quote unavailable" in body


def test_a_link_to_a_deleted_quote_is_gone(store, tmp_path):
    book, _ = pb.load_pricebook(DEMO)
    rec = svc.draft_from_words(store, book, JOB).record
    svc.send_quote(store, book, rec.id, outbox_root=tmp_path, actor="owner", base_url=PUBLIC)
    token = svc.accept_url(store, store.get(rec.id), "https://q.example").rsplit("/", 1)[1]
    demo.reset(store)
    app = app_with(lambda n: Target(store, book, None, None))
    assert app.handle("GET", f"/q/{token}", {})[0] == 410


# ---------------------------------------------------------------- typed commands are the owner's

class FakeCtx:
    def get_config(self, key, default=None):
        return default

    def set_config(self, key, value):
        pass

    def current_cron_execution(self):
        return None


def chat(user, platform="telegram"):
    return {"platform": platform, "source": SimpleNamespace(chat_id="c1", user_id=user, thread_id="")}


@pytest.fixture
def owned(monkeypatch, store):
    monkeypatch.setattr(runtime, "CTX", FakeCtx())
    runtime.reset_for_tests(store)
    commands.docket_admin("setup", **chat("42"))
    assert owner.owners(store)
    yield store
    runtime.reset_for_tests(None)


def test_only_the_owner_types_quote_and_price_book_changes(owned, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "outbox_root", lambda: tmp_path)
    quote, pricebook = commands.make_quote_command(None), commands.make_pricebook_command(None)
    rec = svc.draft_from_words(owned, sample_book(), JOB).record
    for args in (f"send {rec.number}", f"discard {rec.number}", f"show {rec.number}", JOB):
        assert quote(args, **chat("7")) == commands.NOT_OWNER
    assert owned.get(rec.id).status == "draft"
    for args in ("save", "undo", "add labor 150/hr", "new Rivera Plumbing, labor 120/hr"):
        assert pricebook(args, **chat("7")) == commands.NOT_OWNER
    assert "Docket quotes" in quote("help", **chat("7"))
    assert pricebook("show", **chat("7")) != commands.NOT_OWNER
    assert quote(f"discard {rec.number}", **chat("42")) == f"{rec.number} discarded."
    rec2 = svc.draft_from_words(owned, sample_book(), JOB).record
    assert quote(f"discard {rec2.number}", platform="cli", source=None) == f"{rec2.number} discarded."   # terminal


# ---------------------------------------------------------------- review follow-ups

def test_a_copy_edited_before_the_mark_existed_is_still_the_sample(sample_mark):
    legacy, errors = pb.parse_pricebook(DEMO.read_text().replace("demo: true\n", ""))
    assert not errors and legacy.demo


def test_a_try_out_send_repeated_after_going_public_is_refused(sample_mark, store, tmp_path):
    book = sample_book()
    rec = svc.draft_from_words(store, book, JOB).record
    svc.send_quote(store, book, rec.id, outbox_root=tmp_path, actor="owner", base_url="http://127.0.0.1:8787")
    with pytest.raises(svc.ServiceError, match="sample price book"):
        svc.send_quote(store, book, rec.id, outbox_root=tmp_path, actor="owner", base_url=PUBLIC)


def test_the_public_accept_page_refuses_a_sample_priced_quote(sample_mark, store, tmp_path):
    book = sample_book()
    rec = svc.draft_from_words(store, book, JOB).record
    svc.send_quote(store, book, rec.id, outbox_root=tmp_path, actor="owner", base_url="http://127.0.0.1:8787")
    token = svc.accept_url(store, store.get(rec.id), "http://127.0.0.1:8787").rsplit("/", 1)[1]
    public = app_with(lambda n: Target(store, book, None, PUBLIC))
    for method, action in (("GET", ""), ("POST", "/accept")):
        assert public.handle(method, f"/q/{token}{action}", {})[0] == 410
    assert store.get(rec.id).status == "sent"                           # not viewed, not accepted
    local = app_with(lambda n: Target(store, book, None, "http://127.0.0.1:8787"))
    assert local.handle("GET", f"/q/{token}", {})[0] == 200
    own, _ = own_book()                                                  # the owner's book, but the quote is still sample-priced
    assert app_with(lambda n: Target(store, own, None, PUBLIC)).handle("GET", f"/q/{token}", {})[0] == 410


def test_a_try_out_invoice_is_not_handed_to_obol_once_public(sample_mark, store, tmp_path):
    book = sample_book()
    demo.seed(store, book, tmp_path)
    done = next(r for r in store.list() if r.status == "done")
    assert invoices.close_out(store, book, done.id, outbox_root=tmp_path)["number"]      # local try-out
    store.set_meta("public_base_url", PUBLIC)
    with pytest.raises(invoices.InvoiceError, match="sample price book"):
        invoices.close_out(store, book, done.id, outbox_root=tmp_path, obol=True)


@pytest.mark.parametrize("url,local", [
    ("http://127.0.0.1:8787", True), ("http://localhost:8787", True), ("http://[::1]:8787", True),
    ("http://10.0.0.5", True), ("http://172.20.1.1", True), ("http://shop.local", True), ("", True),
    ("https://portal.teamshift.io", False), ("https://10.example.com", False),
    ("https://localhost-portal.example.com", False), ("http://8.8.8.8", False)])
def test_link_is_local(url, local):
    assert svc.link_is_local(url) is local
