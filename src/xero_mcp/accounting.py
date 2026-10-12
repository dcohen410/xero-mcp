"""Xero accounting operations used by the MCP tools."""

from __future__ import annotations

import mimetypes
import re
from datetime import date
from urllib.parse import quote

from . import localfs
from .api import Xero, XeroError
from .config import Config

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
EDITABLE_STATUSES = {"DRAFT", "SUBMITTED"}


def _where_date(field: str, iso: str) -> str:
    y, m, d = (int(x) for x in iso[:10].split("-"))
    return f"{field} >= DateTime({y}, {m}, {d})"


def _q(value: str) -> str:
    return value.replace('"', '\\"')


# ---------------------------------------------------------------- summaries

def contact_summary(c: dict) -> dict:
    return {
        "contact_id": c.get("ContactID"),
        "name": c.get("Name"),
        "email": c.get("EmailAddress") or None,
        "account_number": c.get("AccountNumber") or None,
        "is_supplier": c.get("IsSupplier"),
        "is_customer": c.get("IsCustomer"),
        "status": c.get("ContactStatus"),
    }


def invoice_summary(inv: dict) -> dict:
    return {
        "invoice_id": inv.get("InvoiceID"),
        "type": "bill" if inv.get("Type") == "ACCPAY" else "invoice",
        "number": inv.get("InvoiceNumber") or None,
        "reference": inv.get("Reference") or None,
        "contact": (inv.get("Contact") or {}).get("Name"),
        "status": inv.get("Status"),
        "date": inv.get("Date"),
        "due_date": inv.get("DueDate"),
        "total": inv.get("Total"),
        "amount_due": inv.get("AmountDue"),
        "currency": inv.get("CurrencyCode"),
        "has_attachments": inv.get("HasAttachments"),
    }


def line_summary(li: dict) -> dict:
    return {
        "line_item_id": li.get("LineItemID"),
        "description": li.get("Description"),
        "quantity": li.get("Quantity"),
        "unit_amount": li.get("UnitAmount"),
        "line_amount": li.get("LineAmount"),
        "account_code": li.get("AccountCode"),
        "tax_type": li.get("TaxType"),
        "tax_amount": li.get("TaxAmount"),
        "tracking": [{"category": t.get("Name"), "option": t.get("Option")} for t in li.get("Tracking") or []],
    }


def invoice_detail(inv: dict) -> dict:
    out = invoice_summary(inv)
    out.update({
        "line_amount_types": inv.get("LineAmountTypes"),
        "subtotal": inv.get("SubTotal"),
        "total_tax": inv.get("TotalTax"),
        "lines": [line_summary(li) for li in inv.get("LineItems") or []],
    })
    return out


# ---------------------------------------------------------------- reading

async def organisation(x: Xero) -> dict:
    org = (await x.get("/Organisation"))["Organisations"][0]
    return {k: org.get(k) for k in (
        "Name", "LegalName", "BaseCurrency", "CountryCode", "OrganisationType", "FinancialYearEndDay",
        "FinancialYearEndMonth", "SalesTaxBasis", "SalesTaxPeriod",
    )}


async def search_contacts(x: Xero, query: str, limit: int = 25) -> list[dict]:
    data = await x.get("/Contacts", params={"searchTerm": query, "summaryOnly": "true", "page": "1"})
    return [contact_summary(c) for c in data.get("Contacts", [])[:limit]]


async def list_invoices(
    x: Xero, kind: str, statuses: list[str] | None = None, contact_id: str | None = None,
    since: str | None = None, limit: int = 50,
) -> list[dict]:
    where = ['Type=="ACCPAY"' if kind == "bill" else 'Type=="ACCREC"']
    if since:
        where.append(_where_date("Date", since))
    params = {"where": " && ".join(where), "order": "Date DESC", "page": "1", "summaryOnly": "true"}
    if statuses:
        params["Statuses"] = ",".join(s.upper() for s in statuses)
    if contact_id:
        params["ContactIDs"] = contact_id
    items: list[dict] = []
    page = 1
    while len(items) < limit:
        params["page"] = str(page)
        batch = (await x.get("/Invoices", params=params)).get("Invoices", [])
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return [invoice_summary(i) for i in items[:limit]]


async def outstanding_invoices(x: Xero, kind: str = "invoice", min_days_overdue: int = 0, limit: int = 200) -> list[dict]:
    rows = await list_invoices(x, kind, statuses=["AUTHORISED"], limit=limit)
    today = date.today()
    out = []
    for r in rows:
        if not r["amount_due"]:
            continue
        overdue = (today - date.fromisoformat(r["due_date"])).days if r.get("due_date") else 0
        if overdue >= min_days_overdue:
            out.append({**r, "days_overdue": max(overdue, 0)})
    return sorted(out, key=lambda r: -r["days_overdue"])


async def _fetch_invoice(x: Xero, id_or_number: str) -> dict:
    if _GUID.match(id_or_number):
        data = await x.get(f"/Invoices/{id_or_number}")
    else:
        data = await x.get("/Invoices", params={"InvoiceNumbers": id_or_number, "page": "1"})
    found = data.get("Invoices", [])
    if not found:
        raise XeroError(404, f"No invoice or bill found for '{id_or_number}'.")
    if len(found) > 1:
        names = ", ".join(f"{f.get('InvoiceID')} ({(f.get('Contact') or {}).get('Name')})" for f in found[:5])
        raise XeroError(409, f"More than one invoice has number '{id_or_number}': {names}. Use the invoice_id.")
    return found[0]


async def get_invoice(x: Xero, id_or_number: str) -> dict:
    return invoice_detail(await _fetch_invoice(x, id_or_number))


async def list_accounts(x: Xero, query: str | None = None, account_type: str | None = None) -> list[dict]:
    accounts = (await x.get("/Accounts")).get("Accounts", [])
    out = []
    for a in accounts:
        if a.get("Status") != "ACTIVE":
            continue
        if account_type and a.get("Type", "").upper() != account_type.upper():
            continue
        if query and query.lower() not in f"{a.get('Code', '')} {a.get('Name', '')}".lower():
            continue
        out.append({"code": a.get("Code"), "name": a.get("Name"), "type": a.get("Type"),
                    "tax_type": a.get("TaxType"), "description": a.get("Description") or None})
    return out


async def list_tax_rates(x: Xero) -> list[dict]:
    rates = (await x.get("/TaxRates")).get("TaxRates", [])
    return [{"name": r.get("Name"), "tax_type": r.get("TaxType"), "rate": r.get("EffectiveRate")}
            for r in rates if r.get("Status") == "ACTIVE"]


async def list_tracking(x: Xero) -> list[dict]:
    cats = (await x.get("/TrackingCategories")).get("TrackingCategories", [])
    return [{"category": c.get("Name"), "options": [o.get("Name") for o in c.get("Options", [])]} for c in cats]


async def list_bank_transactions(x: Xero, since: str, limit: int = 100) -> list[dict]:
    data = await x.get("/BankTransactions", params={"where": _where_date("Date", since), "order": "Date DESC", "page": "1"})
    return [{
        "bank_transaction_id": t.get("BankTransactionID"),
        "type": t.get("Type"),
        "date": t.get("Date"),
        "contact": (t.get("Contact") or {}).get("Name"),
        "reference": t.get("Reference") or None,
        "total": t.get("Total"),
        "bank_account": (t.get("BankAccount") or {}).get("Name"),
        "reconciled": t.get("IsReconciled"),
        "status": t.get("Status"),
    } for t in data.get("BankTransactions", [])[:limit]]


async def list_payments(x: Xero, since: str, limit: int = 100) -> list[dict]:
    data = await x.get("/Payments", params={"where": _where_date("Date", since), "order": "Date DESC", "page": "1"})
    return [{
        "payment_id": p.get("PaymentID"),
        "date": p.get("Date"),
        "amount": p.get("Amount"),
        "reference": p.get("Reference") or None,
        "status": p.get("Status"),
        "invoice_number": (p.get("Invoice") or {}).get("InvoiceNumber"),
        "contact": ((p.get("Invoice") or {}).get("Contact") or {}).get("Name"),
        "account": (p.get("Account") or {}).get("Name") or (p.get("Account") or {}).get("Code"),
    } for p in data.get("Payments", [])[:limit]]


def flatten_report(data: dict) -> dict:
    report = data["Reports"][0]
    rows: list[list] = []

    def walk(items: list, section: str = ""):
        for r in items or []:
            if r.get("RowType") == "Section":
                walk(r.get("Rows"), r.get("Title") or section)
            elif r.get("RowType") in ("Row", "SummaryRow"):
                cells = [c.get("Value") for c in r.get("Cells", [])]
                rows.append([section] + cells)

    header = next((r for r in report.get("Rows", []) if r.get("RowType") == "Header"), None)
    walk(report.get("Rows"))
    return {
        "title": " / ".join(t for t in report.get("ReportTitles", []) if t),
        "columns": ["section"] + [c.get("Value") for c in (header or {}).get("Cells", [])],
        "rows": rows,
    }


async def profit_and_loss(x: Xero, from_date: str, to_date: str) -> dict:
    return flatten_report(await x.get("/Reports/ProfitAndLoss", params={"fromDate": from_date, "toDate": to_date}))


async def balance_sheet(x: Xero, as_at: str) -> dict:
    return flatten_report(await x.get("/Reports/BalanceSheet", params={"date": as_at}))


# ---------------------------------------------------------------- attachments

async def list_attachments(x: Xero, invoice_id: str) -> list[dict]:
    data = await x.get(f"/Invoices/{invoice_id}/Attachments")
    return [{"attachment_id": a.get("AttachmentID"), "file_name": a.get("FileName"),
             "mime_type": a.get("MimeType"), "size": a.get("ContentLength")} for a in data.get("Attachments", [])]


async def download_attachment(x: Xero, cfg: Config, invoice_id: str, attachment_id: str, local_folder: str) -> str:
    atts = await list_attachments(x, invoice_id)
    match = next((a for a in atts if a["attachment_id"] == attachment_id or a["file_name"] == attachment_id), None)
    if not match:
        raise XeroError(404, "No such attachment on that invoice. Use list_attachments to see them.")
    data = (await x.request("GET", f"/Invoices/{invoice_id}/Attachments/{match['attachment_id']}",
                            headers={"Accept": match["mime_type"] or "application/octet-stream"})).content
    return str(localfs.write_file(local_folder, match["file_name"], data, cfg.local_roots()))


async def attach_file(x: Xero, cfg: Config, invoice_id: str, local_path: str, file_name: str | None = None) -> dict:
    name, data = localfs.read_file(local_path, cfg.local_roots())
    name = localfs.safe_filename(file_name or name)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("Xero attachments must be 25 MB or smaller.")
    resp = await x.request("PUT", f"/Invoices/{invoice_id}/Attachments/{quote(name)}", content=data,
                           headers={"Content-Type": mime})
    att = (resp.json().get("Attachments") or [{}])[0]
    return {"attached": att.get("FileName", name), "attachment_id": att.get("AttachmentID"), "size": len(data)}


# ---------------------------------------------------------------- contacts

async def _contact_ref(x: Xero, contact_id: str | None, contact_name: str | None) -> dict:
    if contact_id:
        return {"ContactID": contact_id}
    if not contact_name:
        raise ValueError("Give contact_id or contact_name.")
    data = await x.get("/Contacts", params={"where": f'Name=="{_q(contact_name)}"', "summaryOnly": "true"})
    found = data.get("Contacts", [])
    if not found:
        raise ValueError(f"No Xero contact is named exactly '{contact_name}'. Use search_contacts, or create_contact.")
    return {"ContactID": found[0]["ContactID"]}


async def create_contact(
    x: Xero, name: str, email: str | None = None, phone: str | None = None, account_number: str | None = None,
    tax_number: str | None = None,
) -> dict:
    existing = await x.get("/Contacts", params={"where": f'Name=="{_q(name)}"', "summaryOnly": "true"})
    if existing.get("Contacts"):
        raise ValueError(f"A contact called '{name}' already exists ({existing['Contacts'][0]['ContactID']}).")
    body: dict = {"Name": name}
    if email:
        body["EmailAddress"] = email
    if phone:
        body["Phones"] = [{"PhoneType": "DEFAULT", "PhoneNumber": phone}]
    if account_number:
        body["AccountNumber"] = account_number
    if tax_number:
        body["TaxNumber"] = tax_number
    data = await x.post("/Contacts", json={"Contacts": [body]})
    return contact_summary(data["Contacts"][0])


async def update_contact(
    x: Xero, contact_id: str, email: str | None = None, phone: str | None = None,
    account_number: str | None = None, tax_number: str | None = None, name: str | None = None,
) -> dict:
    body: dict = {"ContactID": contact_id}
    if name:
        body["Name"] = name
    if email is not None:
        body["EmailAddress"] = email
    if phone is not None:
        body["Phones"] = [{"PhoneType": "DEFAULT", "PhoneNumber": phone}]
    if account_number is not None:
        body["AccountNumber"] = account_number
    if tax_number is not None:
        body["TaxNumber"] = tax_number
    if len(body) == 1:
        raise ValueError("Nothing to change.")
    data = await x.post(f"/Contacts/{contact_id}", json={"Contacts": [body]})
    return contact_summary(data["Contacts"][0])


# ---------------------------------------------------------------- bills

def _line_payload(line: dict) -> dict:
    out: dict = {}
    mapping = {"line_item_id": "LineItemID", "description": "Description", "quantity": "Quantity",
               "unit_amount": "UnitAmount", "account_code": "AccountCode", "tax_type": "TaxType",
               "line_amount": "LineAmount"}
    for k, v in mapping.items():
        if line.get(k) is not None:
            out[v] = line[k]
    if line.get("tracking"):
        out["Tracking"] = [{"Name": t["category"], "Option": t["option"]} for t in line["tracking"]]
    return out


async def _refuse_duplicate_bill(x: Xero, contact_id: str, invoice_number: str) -> None:
    """Xero happily accepts the same supplier invoice twice, so check first."""
    where = (f'Type=="ACCPAY" && Contact.ContactID==guid("{_q(contact_id)}") '
             f'&& InvoiceNumber=="{_q(invoice_number)}"')
    data = await x.get("/Invoices", params={"where": where, "summaryOnly": "true"})
    live = [i for i in data.get("Invoices", []) if i.get("Status") not in ("DELETED", "VOIDED")]
    if live:
        b = live[0]
        raise ValueError(
            f"A bill from this supplier with invoice number '{invoice_number}' already exists "
            f"({b.get('Status')}, {b.get('InvoiceID')}). Not creating a duplicate. If it really is a separate "
            "bill, call again with allow_duplicate=true."
        )


async def create_draft_bill(
    x: Xero, *, lines: list[dict], contact_id: str | None = None, contact_name: str | None = None,
    date_: str | None = None, due_date: str | None = None, invoice_number: str | None = None,
    reference: str | None = None, amounts_are: str = "Inclusive", allow_duplicate: bool = False,
) -> dict:
    if amounts_are not in ("Inclusive", "Exclusive", "NoTax"):
        raise ValueError("amounts_are must be Inclusive, Exclusive or NoTax.")
    if not lines:
        raise ValueError("A bill needs at least one line.")
    contact = await _contact_ref(x, contact_id, contact_name)
    if invoice_number and not allow_duplicate:
        await _refuse_duplicate_bill(x, contact["ContactID"], invoice_number)
    bill: dict = {
        "Type": "ACCPAY",
        "Status": "DRAFT",
        "Contact": contact,
        "LineAmountTypes": amounts_are,
        "LineItems": [_line_payload(li) for li in lines],
    }
    if date_:
        bill["Date"] = date_
    if due_date:
        bill["DueDate"] = due_date
    if invoice_number:
        bill["InvoiceNumber"] = invoice_number
    if reference:
        bill["Reference"] = reference
    data = await x.post("/Invoices", json={"Invoices": [bill]})
    return invoice_detail(data["Invoices"][0])


async def update_bill_lines(
    x: Xero, invoice_id: str, changes: list[dict] | None = None, replace_all_lines: list[dict] | None = None,
) -> dict:
    """Recode or edit lines on a draft bill. `changes` edits existing lines by line_item_id;
    `replace_all_lines` swaps in a whole new set of lines (e.g. to split one line into several)."""
    current = await _fetch_invoice(x, invoice_id)
    if current.get("Status") not in EDITABLE_STATUSES:
        raise ValueError(f"Only draft bills can be edited here; this one is {current.get('Status')}.")
    before = current.get("Total")
    if replace_all_lines:
        new_lines = [_line_payload(li) for li in replace_all_lines]
    elif changes:
        by_id = {c["line_item_id"]: c for c in changes}
        unknown = set(by_id) - {li["LineItemID"] for li in current.get("LineItems", [])}
        if unknown:
            raise ValueError(f"These line_item_ids aren't on the bill: {', '.join(unknown)}")
        new_lines = []
        for li in current.get("LineItems", []):
            keep = {k: li[k] for k in ("LineItemID", "Description", "Quantity", "UnitAmount", "AccountCode",
                                       "TaxType", "Tracking") if li.get(k) is not None}
            keep.update(_line_payload(by_id.get(li["LineItemID"], {})))
            new_lines.append(keep)
    else:
        raise ValueError("Give changes or replace_all_lines.")
    data = await x.post(f"/Invoices/{current['InvoiceID']}",
                        json={"Invoices": [{"InvoiceID": current["InvoiceID"], "LineItems": new_lines}]})
    after = invoice_detail(data["Invoices"][0])
    after["total_before"] = before
    if before != after["total"]:
        after["warning"] = f"The bill total changed from {before} to {after['total']}."
    return after


async def bills_for_approval(x: Xero, invoice_ids: list[str]) -> list[dict]:
    out = []
    for iid in invoice_ids:
        inv = await _fetch_invoice(x, iid)
        if inv.get("Type") != "ACCPAY":
            raise ValueError(f"{iid} is a sales invoice, not a bill.")
        if inv.get("Status") not in EDITABLE_STATUSES:
            raise ValueError(f"{inv.get('InvoiceNumber') or iid} is {inv.get('Status')}, not a draft.")
        uncoded = [li.get("Description") for li in inv.get("LineItems", []) if not li.get("AccountCode")]
        if uncoded:
            raise ValueError(f"{inv.get('InvoiceNumber') or iid} has lines without an account code: {uncoded}")
        out.append(invoice_summary(inv))
    return out


async def approve_bills(x: Xero, invoice_ids: list[str]) -> list[dict]:
    results = []
    for iid in invoice_ids:
        try:
            data = await x.post(f"/Invoices/{iid}", json={"Invoices": [{"InvoiceID": iid, "Status": "AUTHORISED"}]})
            inv = data["Invoices"][0]
            results.append({"invoice_id": iid, "approved": inv.get("Status") == "AUTHORISED", "status": inv.get("Status")})
        except XeroError as exc:
            results.append({"invoice_id": iid, "approved": False, "error": str(exc)})
    return results
