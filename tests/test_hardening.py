"""Per-call approval grains, owner-only setup, white-label text and the deployment-pinned public link."""
from __future__ import annotations

import email
import sqlite3
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import docket  # noqa: E402
from docket import commands, owner, runtime, service as svc  # noqa: E402
from docket import pbedit as pe  # noqa: E402
from docket.accept_server import AcceptApp, make_handler  # noqa: E402
from docket.outbox import envelope, write_outbox  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.store import Store  # noqa: E402

DEMO = Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml"
BOOK, _ = load_pricebook(DEMO)
JOB = "Dana Henderson, 14 Maple St 62707, water heater, permit, haul away"


class FakeCtx:
    def __init__(self, **values):
        self.values = values

    def get_config(self, key, default=None):
        return self.values.get(key, default)

    def set_config(self, key, value):
        self.values[key] = value

    def current_cron_execution(self):
        return None


@pytest.fixture
def store(monkeypatch):
    s = Store(sqlite3.connect(":memory:", check_same_thread=False))
    monkeypatch.setattr(runtime, "CTX", FakeCtx())
    monkeypatch.delenv("DOCKET_PUBLIC_LINK", raising=False)
    monkeypatch.delenv("DOCKET_SENDER_EMAIL", raising=False)
    runtime.reset_for_tests(s)
    yield s
    runtime.reset_for_tests(None)


def chat(user, platform="telegram", chat_id="c1"):
    return {"platform": platform, "source": SimpleNamespace(chat_id=chat_id, user_id=user, thread_id="")}


# ---------------------------------------------------------------- 1. one approval grain per quote version

def test_each_send_has_its_own_rule_key(store):
    gate = docket._make_gate(FakeCtx())
    a = svc.draft_from_words(store, BOOK, JOB).record
    b = svc.draft_from_words(store, BOOK, "Sam Ortiz, 9 Elm St 62704, water heater").record
    first = gate(tool_name="docket_send_quote", args={"quote_id": a.number})
    second = gate(tool_name="docket_send_quote", args={"quote_id": b.number})
    assert first["action"] == second["action"] == "approve"
    assert first["rule_key"] == f"docket:docket_send_quote:{a.number}-v1"
    assert first["rule_key"] != second["rule_key"]
    assert a.number in first["message"] and "Dana Henderson" in first["message"] and "$" in first["message"]
    same = gate(tool_name="docket_send_quote", args={"quote_id": str(a.id)})      # "1001" names the same quote
    assert same["rule_key"] == first["rule_key"]
    svc.revise_from_words(store, BOOK, a.id, "add a shutoff valve")               # a revision is a new approval
    assert gate(tool_name="docket_send_quote", args={"quote_id": a.number})["rule_key"].endswith("-v2")
    unknown = gate(tool_name="docket_send_quote", args={"quote_id": "Q-9999"})
    assert unknown["action"] == "approve" and unknown["rule_key"] not in {first["rule_key"], second["rule_key"]}
    assert gate(tool_name="docket_send_quote", args={})["rule_key"] == "docket:docket_send_quote:unknown-none"


def test_each_price_book_save_has_its_own_rule_key(store):
    gate = docket._make_gate(FakeCtx())
    p1 = pe.propose(store, DEMO, "labor 150/hr")
    k1 = gate(tool_name="docket_pricebook_save", args={"preview_id": p1.token})["rule_key"]
    p2 = pe.propose(store, DEMO, "labor 160/hr")
    k2 = gate(tool_name="docket_pricebook_save", args={"preview_id": p2.token})["rule_key"]
    assert k1.startswith("docket:docket_pricebook_save:") and k1 != k2


def test_rule_keys_are_safe_and_bounded():
    key = docket._rule_key("docket_send_quote", "Q-1:x y/../" + "z" * 200)
    assert key.startswith("docket:docket_send_quote:") and " " not in key and "/" not in key and len(key) < 100


# ---------------------------------------------------------------- 2. only the registered owner re-runs setup

def test_first_setup_records_the_owner_and_only_they_can_rerun(store):
    assert "set up on Telegram" in commands.docket_admin("setup", **chat("42"))
    refused = commands.docket_admin("setup", **chat("7", chat_id="group"))
    assert commands.NOT_OWNER in refused
    assert owner.get_owner(store, "telegram") == {"chat_id": "c1", "user_id": "42", "thread_id": ""}
    assert "set up on Telegram" in commands.docket_admin("setup", **chat("42", chat_id="c2"))   # owner moves chat
    assert owner.get_owner(store, "telegram")["chat_id"] == "c2"


def test_setup_needs_a_messaging_sender(store):
    out = commands.docket_admin("setup", platform="cli", source=None)
    assert "Telegram, Discord or Slack" in out and owner.owners(store) == {}


def test_another_app_needs_a_pairing_code_from_the_owner(store):
    commands.docket_admin("setup", **chat("42"))
    assert "already set up on Telegram" in commands.docket_admin("setup", **chat("d9", platform="discord"))
    assert commands.docket_admin("setup pair", **chat("7")) == commands.NOT_OWNER
    reply = commands.docket_admin("setup pair", **chat("42"))
    code = reply.split("code is ", 1)[1].split(".", 1)[0]
    assert "set up on Discord" in commands.docket_admin(f"setup {code}", **chat("d9", platform="discord"))
    assert owner.get_owner(store, "discord")["user_id"] == "d9"
    assert "already set up" in commands.docket_admin(f"setup {code}", **chat("s1", platform="slack"))   # used up


def test_a_wrong_or_expired_pairing_code_is_refused(store):
    commands.docket_admin("setup", **chat("42"))
    code = owner.issue_pair_code(store)
    assert not owner.take_pair_code(store, "deadbeef")
    assert not owner.take_pair_code(store, code)                   # a wrong guess burns the code
    code = owner.issue_pair_code(store, now=1000.0)
    assert not owner.take_pair_code(store, code, now=1000.0 + owner.PAIR_TTL_SECONDS + 1)


def test_demo_and_link_are_owner_only_from_chats(store):
    commands.docket_admin("setup", **chat("42"))
    assert commands.docket_admin("demo reset", **chat("7")) == commands.NOT_OWNER
    assert commands.docket_admin("link https://evil.example", **chat("7")) == commands.NOT_OWNER
    assert runtime.public_link() == ""
    assert "will use https://q.example.com" in commands.docket_admin("link https://q.example.com", **chat("42"))
    assert "local accept page" in commands.docket_admin("link local", platform="cli", source=None)   # terminal


# ---------------------------------------------------------------- 3. white-label text

def test_user_facing_text_names_no_engine(store, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ensure_accept_server", lambda: "http://127.0.0.1:8787")
    texts = [commands.docket_admin("setup", platform="cli", source=None), commands.docket_admin("serve"),
             commands.DOCKET_HELP, commands.CLI_HELP.split("\n\n", 1)[1], svc.LOCAL_LINK_NOTE,
             owner.perform("docket:send:1:1", platform="telegram", user_id="1", store=store, book=BOOK,
                           outbox_root=tmp_path, base_url=lambda: "").toast]
    for text in texts:
        assert "hermes" not in text.lower(), text


def test_accept_page_sends_no_server_header(store):
    app = AcceptApp(lambda: store, lambda: BOOK)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/health") as resp:
            assert resp.read() == b"ok" and resp.headers.get("Server") is None and resp.headers.get("Date")
    finally:
        server.shutdown()
        server.server_close()


def test_outbox_sender_comes_from_the_business(store, tmp_path, monkeypatch):
    biz, cust = {"name": "Rivera Plumbing", "email": "office@rivera.example"}, {"name": "Dana", "email": "d@x.example"}
    assert envelope(biz, cust) == {"From": "Rivera Plumbing <office@rivera.example>", "To": "Dana <d@x.example>"}
    runtime.CTX.values["sender_email"] = "quotes@rivera.example"
    env = envelope(biz, cust)
    assert env["From"] == "Rivera Plumbing <quotes@rivera.example>" and env["Reply-To"] == "Rivera Plumbing <office@rivera.example>"
    monkeypatch.setenv("DOCKET_SENDER_EMAIL", "hello@workspace.example")
    assert envelope(biz, cust)["From"] == "Rivera Plumbing <hello@workspace.example>"
    assert envelope({"name": "Rivera Plumbing"}, {"name": "Dana"}, sender="") == {}   # nothing invented
    folder = write_outbox(tmp_path, number="Q-1", version=1, business={"name": "Rivera"}, customer={"name": "Dana"},
                          subject="s", body="b")
    raw = (folder / "message.eml").read_bytes()
    msg = email.message_from_bytes(raw)
    assert b"invalid" not in raw and b"Docket" not in raw and msg["From"] == "Rivera <hello@workspace.example>" and msg["To"] is None


# ---------------------------------------------------------------- 4. public link per workspace

def test_public_link_env_wins_and_names_the_accept_route(store, monkeypatch):
    runtime.CTX.values["public_link"] = "https://owner-tunnel.example"
    monkeypatch.setenv("DOCKET_PUBLIC_LINK", "https://portal.example.com/w/acme/")
    rec = svc.draft_from_words(store, BOOK, JOB).record
    url = svc.accept_url(store, rec)
    assert url.startswith("https://portal.example.com/w/acme/q/") and "//q/" not in url
    assert runtime.public_link_from_env() == "https://portal.example.com/w/acme"
    assert runtime.link_base("") == "https://portal.example.com/w/acme"
    assert "set by your workspace" in commands.docket_admin("link https://elsewhere.example", platform="cli", source=None)
    assert runtime.CTX.values["public_link"] == "https://owner-tunnel.example"          # not overwritten
    assert not svc.link_is_local(url)
    monkeypatch.delenv("DOCKET_PUBLIC_LINK")
    assert svc.accept_url(store, rec).startswith("https://owner-tunnel.example/q/")      # the setting applies again
