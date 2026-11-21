"""Tests for the fixes from the 2026-11-20 review."""

import json
import os
from datetime import date, timedelta

import httpx
import pytest
import respx
from keyring.errors import KeyringError

from xero_mcp import accounting as acc
from xero_mcp import auth, localfs, server
from xero_mcp.api import XeroError
from xero_mcp.config import Config
from tests.conftest import BILL_ID, bill

CONTACT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ---------------------------------------------------------------- update_bill_lines

def discounted_lines():
    return [
        {"LineItemID": "L1", "Description": "Pens", "Quantity": 2, "UnitAmount": 50, "LineAmount": 90,
         "AccountCode": "400", "TaxType": "INPUT", "TaxAmount": 8.19, "ItemCode": "PEN", "DiscountRate": 10,
         "Tracking": [{"Name": "Region", "Option": "ACT"}]},
        {"LineItemID": "L2", "Description": "Snacks", "Quantity": 1, "UnitAmount": 43, "LineAmount": 43,
         "AccountCode": "420", "TaxType": "INPUT", "TaxAmount": 3.91},
    ]


async def test_recoding_one_line_keeps_discounts_items_and_tax_adjustments(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill(lines=discounted_lines())]})
    route = mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill(lines=discounted_lines())]})
    await acc.update_bill_lines(xero, BILL_ID, changes=[{"line_item_id": "L2", "account_code": "429"}])
    sent = json.loads(route.calls[0].request.content)["Invoices"][0]["LineItems"]
    assert sent[0]["DiscountRate"] == 10 and sent[0]["ItemCode"] == "PEN" and sent[0]["TaxAmount"] == 8.19
    assert sent[0]["Tracking"] == [{"Name": "Region", "Option": "ACT"}]
    assert sent[1]["AccountCode"] == "429" and sent[1]["TaxAmount"] == 3.91


async def test_changing_an_amount_lets_xero_recalculate_tax(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill(lines=discounted_lines())]})
    route = mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill(lines=discounted_lines())]})
    await acc.update_bill_lines(xero, BILL_ID, changes=[{"line_item_id": "L2", "unit_amount": 50}])
    sent = json.loads(route.calls[0].request.content)["Invoices"][0]["LineItems"]
    assert "TaxAmount" not in sent[1] and sent[1]["UnitAmount"] == 50
    assert sent[0]["TaxAmount"] == 8.19  # untouched line keeps its adjustment


async def test_sales_invoices_are_not_edited(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill(Type="ACCREC")]})
    post = mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill()]})
    with pytest.raises(ValueError, match="sales invoice"):
        await acc.update_bill_lines(xero, BILL_ID, changes=[{"line_item_id": "L1", "account_code": "400"}])
    assert not post.called


async def test_duplicate_line_ids_in_changes_are_refused(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill()]})
    with pytest.raises(ValueError, match="more than once"):
        await acc.update_bill_lines(xero, BILL_ID, changes=[{"line_item_id": "L1", "account_code": "400"},
                                                            {"line_item_id": "L1", "account_code": "420"}])


# ---------------------------------------------------------------- approving

async def test_approving_by_invoice_number_approves_the_checked_bill(mock, monkeypatch):
    monkeypatch.setattr(server.cfg, "approve_mode", "confirm")

    async def token():
        return "t"
    monkeypatch.setattr(server, "_xero", server.Xero(token, "TENANT", httpx.AsyncClient()))
    mock.get("/Invoices", params={"InvoiceNumbers": "INV-1"}).respond(
        json={"Invoices": [bill(lines=discounted_lines())]})
    approve = mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill(status="AUTHORISED")]})

    class Ctx:
        client_capabilities = None
    code = (await server.approve_bills(["INV-1"], Ctx()))["confirmation_code"]
    out = await server.approve_bills(["INV-1"], Ctx(), confirmation_code=code)
    assert out["results"][0]["approved"] is True
    assert json.loads(approve.calls[0].request.content)["Invoices"][0]["InvoiceID"] == BILL_ID


async def test_approve_refuses_anything_but_an_id(xero):
    with pytest.raises(ValueError, match="isn't a Xero invoice_id"):
        await acc.approve_bills(xero, ["INV-1"])


# ---------------------------------------------------------------- lists

def row(i, due_days_ago):
    due = (date.today() - timedelta(days=due_days_ago)).isoformat()
    return {"InvoiceID": f"id{i}", "Type": "ACCREC", "Status": "AUTHORISED", "AmountDue": 10, "Total": 10,
            "DueDate": due, "Date": due}


async def test_outstanding_reads_every_page_and_keeps_the_oldest(mock, xero):
    pages = {"1": [row(i, 400 - i) for i in range(100)], "2": [row(100 + i, 200 - i) for i in range(100)],
             "3": [row(200 + i, 50) for i in range(30)]}
    route = mock.get("/Invoices").mock(side_effect=lambda req: httpx.Response(
        200, json={"Invoices": pages[req.url.params["page"]]}))
    out = await acc.outstanding_invoices(xero, "invoices", min_days_overdue=0, limit=5)
    assert route.call_count == 3
    assert route.calls[0].request.url.params["order"] == "DueDate ASC"
    assert out[0]["days_overdue"] == 400 and len(out) == 5


async def test_payments_page_past_100(mock, xero):
    route = mock.get("/Payments").mock(side_effect=lambda req: httpx.Response(200, json={"Payments": [
        {"PaymentID": f"p{i}"} for i in range(100 if req.url.params["page"] == "1" else 20)]}))
    out = await acc.list_payments(xero, "2026-10-01", limit=300)
    assert len(out) == 120 and route.call_count == 2


async def test_bad_dates_and_kinds_get_clear_errors(xero):
    with pytest.raises(ValueError, match="2026-10-16"):
        await acc.list_payments(xero, "12/10/2026")
    with pytest.raises(ValueError, match="kind must be"):
        await acc.list_invoices(xero, "receipts")


# ---------------------------------------------------------------- contacts and escaping

def test_where_values_escape_backslashes_then_quotes():
    assert acc._q('Smith \\ Co "Ltd"') == 'Smith \\\\ Co \\"Ltd\\"'


async def test_ambiguous_contact_name_is_refused(mock, xero):
    mock.get("/Contacts").respond(json={"Contacts": [{"ContactID": "a"}, {"ContactID": "b"}]})
    with pytest.raises(ValueError, match="More than one"):
        await acc._contact_ref(xero, None, "Acme")


async def test_update_contact_needs_a_real_id(xero):
    with pytest.raises(ValueError, match="isn't a Xero contact_id"):
        await acc.update_contact(xero, "../Invoices/x", email="a@b.com")


@pytest.mark.parametrize("raw, bsb, account", [
    ("062-000 1234 5678", "062000", "12345678"),
    ("06200012345678", "062000", "12345678"),
    ("Acct 12345678", None, None),
    ("Westpac 062 000 / 1234 5678 (2nd acct 9)", None, None),
])
def test_bsb_is_only_split_when_clear(raw, bsb, account):
    assert acc._split_bsb(raw) == (bsb, account)


# ---------------------------------------------------------------- local files

@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs admin rights on Windows")
def test_write_does_not_follow_a_broken_symlink(tmp_path):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "receipt.pdf").symlink_to(outside / "planted.txt")
    saved = localfs.write_file(str(allowed), "receipt.pdf", b"data", [allowed.resolve()])
    assert saved.name == "receipt (1).pdf" and not (outside / "planted.txt").exists()


def test_relative_and_root_folders_are_not_allowed(tmp_path):
    cfg = Config(local_dirs=[".", "/", str(tmp_path)])
    assert cfg.local_roots() == [tmp_path.resolve()]


# ---------------------------------------------------------------- sign-in refresh

async def test_keychain_save_failure_keeps_the_new_token(cfg, monkeypatch):
    auth.save_refresh_token(cfg, "r1")
    calls = []

    def failing_save(c, token):
        calls.append(token)
        raise KeyringError("locked")
    with respx.mock() as router:
        route = router.post(auth.TOKEN_URL).mock(side_effect=[
            httpx.Response(200, json={"access_token": "A1", "refresh_token": "r2", "expires_in": 0}),
            httpx.Response(200, json={"access_token": "A2", "refresh_token": "r3", "expires_in": 1800}),
        ])
        async with httpx.AsyncClient() as client:
            provider = auth.TokenProvider(cfg, client)
            monkeypatch.setattr(auth, "save_refresh_token", failing_save)
            assert await provider() == "A1"
            monkeypatch.undo()
            assert await provider() == "A2"
        # The second refresh used the unsaved r2, not the spent r1.
        assert b"refresh_token=r2" in route.calls[1].request.content
    assert auth.load_refresh_token(cfg) == "r3"


async def test_outage_is_not_reported_as_expired_sign_in(cfg):
    auth.save_refresh_token(cfg, "r1")
    with respx.mock() as router:
        router.post(auth.TOKEN_URL).respond(503, text="down")
        async with httpx.AsyncClient() as client:
            with pytest.raises(XeroError, match="don't need to sign in again"):
                await auth.TokenProvider(cfg, client)()


async def test_refresh_rereads_the_token_another_copy_saved(cfg):
    auth.save_refresh_token(cfg, "r1")
    with respx.mock() as router:
        route = router.post(auth.TOKEN_URL).respond(
            json={"access_token": "A", "refresh_token": "r-next", "expires_in": 0})
        async with httpx.AsyncClient() as client:
            provider = auth.TokenProvider(cfg, client)
            await provider()
            auth.save_refresh_token(cfg, "r-from-other-copy")  # Claude Desktop's copy refreshed meanwhile
            await provider()
        assert b"refresh_token=r-from-other-copy" in route.calls[1].request.content


async def test_refresh_lock_is_exclusive(tmp_path):
    path = tmp_path / "refresh.lock"
    async with auth._refresh_lock(path):
        with pytest.raises(XeroError, match="busy"):
            async with auth._refresh_lock(path, timeout=0.2):
                pass
