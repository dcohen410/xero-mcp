import json
from datetime import date, timedelta

import httpx
import pytest

from xero_mcp import accounting as acc
from xero_mcp.api import XeroError, clean_dates
from tests.conftest import BILL_ID, bill


def test_clean_dates():
    assert clean_dates({"a": ["/Date(1760227200000+0000)/"]}) == {"a": ["2025-10-12"]}
    assert clean_dates("not a date") == "not a date"


async def test_requests_carry_tenant_header(mock, xero):
    route = mock.get("/Organisation").respond(json={"Organisations": [{"Name": "Demo Co"}]})
    assert (await acc.organisation(xero))["Name"] == "Demo Co"
    assert route.calls[0].request.headers["xero-tenant-id"] == "TENANT"
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"


async def test_list_bills_filters_type_and_status(mock, xero):
    route = mock.get("/Invoices").respond(json={"Invoices": [bill()]})
    out = await acc.list_invoices(xero, "bill", statuses=["draft"], since="2026-10-01")
    params = route.calls[0].request.url.params
    assert params["where"] == 'Type=="ACCPAY" && Date >= DateTime(2026, 10, 1)'
    assert params["Statuses"] == "DRAFT"
    assert out[0]["type"] == "bill" and out[0]["date"] == "2025-10-12"


async def test_statuses_can_be_a_string(mock, xero):
    route = mock.get("/Invoices").respond(json={"Invoices": []})
    await acc.list_invoices(xero, "bill", statuses="draft, submitted")
    assert route.calls[0].request.url.params["Statuses"] == "DRAFT,SUBMITTED"


async def test_outstanding_sorts_by_days_overdue(mock, xero):
    old = (date.today() - timedelta(days=90)).isoformat()
    recent = (date.today() - timedelta(days=5)).isoformat()
    mock.get("/Invoices").respond(json={"Invoices": [
        {"InvoiceID": "a", "Type": "ACCREC", "DueDate": recent, "AmountDue": 10, "Total": 10},
        {"InvoiceID": "b", "Type": "ACCREC", "DueDate": old, "AmountDue": 20, "Total": 20},
        {"InvoiceID": "c", "Type": "ACCREC", "DueDate": old, "AmountDue": 0, "Total": 5},
    ]})
    out = await acc.outstanding_invoices(xero, min_days_overdue=60)
    assert [r["invoice_id"] for r in out] == ["b"] and out[0]["days_overdue"] == 90


async def test_validation_errors_are_readable(mock, xero):
    mock.post("/Invoices").respond(400, json={"Elements": [{"ValidationErrors": [{"Message": "Account code '999' is not a valid code"}]}]})
    mock.get("/Contacts").respond(json={"Contacts": [{"ContactID": "C1"}]})
    with pytest.raises(XeroError, match="Account code '999'"):
        await acc.create_draft_bill(xero, contact_name="Supplier Pty Ltd", lines=[{"description": "x", "account_code": "999"}])


async def test_insufficient_scope_explains_fix(mock, xero):
    mock.get("/Payments").respond(401, headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'})
    with pytest.raises(XeroError, match="developer.xero.com"):
        await acc.list_payments(xero, "2026-01-01")


async def test_create_draft_bill(mock, xero):
    mock.get("/Contacts").respond(json={"Contacts": [{"ContactID": "C1"}]})
    mock.get("/Invoices").respond(json={"Invoices": []})
    route = mock.post("/Invoices").respond(json={"Invoices": [bill()]})
    await acc.create_draft_bill(
        xero, contact_name="Supplier Pty Ltd", invoice_number="INV-1", date_="2026-10-12",
        lines=[{"description": "Pens", "quantity": 1, "unit_amount": 72, "account_code": "453", "tax_type": "INPUT",
                "tracking": [{"category": "Region", "option": "North"}]}],
    )
    sent = json.loads(route.calls[0].request.content)["Invoices"][0]
    assert sent["Type"] == "ACCPAY" and sent["Status"] == "DRAFT" and sent["LineAmountTypes"] == "Inclusive"
    assert sent["Contact"] == {"ContactID": "C1"} and sent["InvoiceNumber"] == "INV-1"
    assert sent["LineItems"][0] == {"Description": "Pens", "Quantity": 1, "UnitAmount": 72, "AccountCode": "453",
                                    "TaxType": "INPUT", "Tracking": [{"Name": "Region", "Option": "North"}]}


async def test_duplicate_bill_is_refused(mock, xero):
    mock.get("/Contacts").respond(json={"Contacts": [{"ContactID": "C1"}]})
    lookup = mock.get("/Invoices").respond(json={"Invoices": [bill()]})
    create = mock.post("/Invoices").respond(json={"Invoices": [bill()]})
    with pytest.raises(ValueError, match="already exists"):
        await acc.create_draft_bill(xero, contact_name="Supplier Pty Ltd", invoice_number="INV-1",
                                    lines=[{"description": "x"}])
    assert 'InvoiceNumber=="INV-1"' in lookup.calls[0].request.url.params["where"] and not create.called
    await acc.create_draft_bill(xero, contact_name="Supplier Pty Ltd", invoice_number="INV-1",
                                lines=[{"description": "x"}], allow_duplicate=True)
    assert create.called


async def test_voided_bill_is_not_a_duplicate(mock, xero):
    mock.get("/Contacts").respond(json={"Contacts": [{"ContactID": "C1"}]})
    mock.get("/Invoices").respond(json={"Invoices": [bill(status="VOIDED")]})
    create = mock.post("/Invoices").respond(json={"Invoices": [bill()]})
    await acc.create_draft_bill(xero, contact_name="Supplier Pty Ltd", invoice_number="INV-1",
                                lines=[{"description": "x"}])
    assert create.called


async def test_unknown_contact_name_is_refused(mock, xero):
    mock.get("/Contacts").respond(json={"Contacts": []})
    with pytest.raises(ValueError, match="search_contacts"):
        await acc.create_draft_bill(xero, contact_name="Nobody", lines=[{"description": "x"}])


async def test_recode_keeps_other_fields(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill()]})
    route = mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill()]})
    out = await acc.update_bill_lines(xero, BILL_ID, changes=[{"line_item_id": "L2", "account_code": "422"}])
    lines = json.loads(route.calls[0].request.content)["Invoices"][0]["LineItems"]
    assert lines[0] == {"LineItemID": "L1", "Description": "Pens", "Quantity": 1, "UnitAmount": 72, "TaxType": "INPUT"}
    assert lines[1]["AccountCode"] == "422" and lines[1]["UnitAmount"] == 43
    assert "warning" not in out


async def test_recode_warns_when_total_changes(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill()]})
    mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill(total=100.0)]})
    out = await acc.update_bill_lines(xero, BILL_ID, replace_all_lines=[{"description": "All", "unit_amount": 100}])
    assert "changed from 115.0 to 100.0" in out["warning"]


async def test_cannot_edit_approved_bill(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill(status="AUTHORISED")]})
    with pytest.raises(ValueError, match="Only draft bills"):
        await acc.update_bill_lines(xero, BILL_ID, changes=[{"line_item_id": "L1", "account_code": "453"}])


async def test_unknown_line_id_is_refused(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill()]})
    with pytest.raises(ValueError, match="aren't on the bill"):
        await acc.update_bill_lines(xero, BILL_ID, changes=[{"line_item_id": "nope", "account_code": "453"}])


async def test_attach_file(mock, xero, cfg, tmp_path):
    pdf = tmp_path / "Invoice 1.pdf"
    pdf.write_bytes(b"%PDF")
    route = mock.put(f"/Invoices/{BILL_ID}/Attachments/Invoice%201.pdf").respond(
        json={"Attachments": [{"FileName": "Invoice 1.pdf", "AttachmentID": "A1"}]})
    out = await acc.attach_file(xero, cfg, BILL_ID, str(pdf))
    req = route.calls[0].request
    assert req.content == b"%PDF" and req.headers["Content-Type"] == "application/pdf"
    assert out["attachment_id"] == "A1"


async def test_attach_file_outside_allowed_folders(xero, cfg):
    with pytest.raises(Exception, match="outside the folders"):
        await acc.attach_file(xero, cfg, BILL_ID, "/etc/hosts")


async def test_approval_check_rejects_uncoded_lines(mock, xero):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [bill()]})
    with pytest.raises(ValueError, match="without an account code"):
        await acc.bills_for_approval(xero, [BILL_ID])


async def test_report_is_flattened(mock, xero):
    mock.get("/Reports/ProfitAndLoss").respond(json={"Reports": [{
        "ReportTitles": ["Profit and Loss", "Demo Co"],
        "Rows": [
            {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "Oct 2026"}]},
            {"RowType": "Section", "Title": "Income", "Rows": [
                {"RowType": "Row", "Cells": [{"Value": "Sales"}, {"Value": "1000.00"}]},
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Income"}, {"Value": "1000.00"}]},
            ]},
        ],
    }]})
    out = await acc.profit_and_loss(xero, "2026-10-01", "2026-10-31")
    assert out["columns"] == ["section", "", "Oct 2026"]
    assert out["rows"] == [["Income", "Sales", "1000.00"], ["Income", "Total Income", "1000.00"]]


async def test_rate_limit_retry_then_success(mock, xero, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr("asyncio.sleep", no_sleep)
    route = mock.get("/TaxRates").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"TaxRates": [{"Name": "GST on Expenses", "TaxType": "INPUT", "EffectiveRate": 10, "Status": "ACTIVE"}]}),
    ])
    assert (await acc.list_tax_rates(xero))[0]["tax_type"] == "INPUT"
    assert route.call_count == 2


async def test_long_rate_limit_is_reported_not_waited(mock, xero):
    mock.get("/TaxRates").respond(429, headers={"Retry-After": "3600"})
    with pytest.raises(XeroError, match="rate limiting"):
        await acc.list_tax_rates(xero)
