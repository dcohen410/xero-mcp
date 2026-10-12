"""The MCP server: tool definitions for Claude (or any MCP client)."""

from __future__ import annotations

from typing import Any, Awaitable

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from . import accounting as acc
from . import config as config_mod
from . import localfs
from .api import Xero, XeroError
from .auth import NotSignedIn, TokenProvider

INSTRUCTIONS = """\
Tools for the signed-in person's Xero organisation.

- Bills are invoices of type ACCPAY. Create them as drafts. Approving is off unless the user turned it on.
- Before coding a bill, look up real account codes with list_accounts and tax types with
  list_tax_rates. Never invent codes.
- Nothing here deletes, voids or records payments.
- Amounts on bills are GST-inclusive by default (amounts_are="Inclusive").
- Dates are YYYY-MM-DD.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
APPROVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

server = MCPServer(name="xero-mcp", instructions=INSTRUCTIONS)
cfg = config_mod.load()
_xero: Xero | None = None


def xero() -> Xero:
    global _xero
    if _xero is None:
        _xero = Xero(TokenProvider(cfg), cfg.tenant_id)
    return _xero


async def guard(coro: Awaitable[Any]) -> Any:
    try:
        return await coro
    except (NotSignedIn, XeroError, ValueError, localfs.LocalPathError) as exc:
        raise ToolError(str(exc)) from None


# ================================================================ reading

@server.tool(annotations=READ)
async def organisation_info() -> dict:
    """The connected Xero organisation's name, currency, financial year end and tax settings."""
    return await guard(acc.organisation(xero()))


@server.tool(annotations=READ)
async def search_contacts(query: str, limit: int = 25) -> list[dict]:
    """Find contacts (suppliers and customers) by name, email or account number."""
    return await guard(acc.search_contacts(xero(), query, limit))


@server.tool(annotations=READ)
async def list_bills(
    statuses: list[str] | None = None, contact_id: str | None = None, since: str | None = None, limit: int = 50,
) -> list[dict]:
    """List bills (supplier invoices), newest first. statuses: DRAFT, SUBMITTED, AUTHORISED (awaiting
    payment), PAID, VOIDED. `since` is YYYY-MM-DD."""
    return await guard(acc.list_invoices(xero(), "bill", statuses, contact_id, since, limit))


@server.tool(annotations=READ)
async def list_invoices(
    statuses: list[str] | None = None, contact_id: str | None = None, since: str | None = None, limit: int = 50,
) -> list[dict]:
    """List sales invoices, newest first. statuses: DRAFT, SUBMITTED, AUTHORISED (awaiting payment),
    PAID, VOIDED. `since` is YYYY-MM-DD."""
    return await guard(acc.list_invoices(xero(), "invoice", statuses, contact_id, since, limit))


@server.tool(annotations=READ)
async def outstanding(kind: str = "invoice", min_days_overdue: int = 0) -> list[dict]:
    """Unpaid sales invoices (kind="invoice") or bills (kind="bill"), most overdue first, with days
    overdue. Use min_days_overdue (e.g. 60) to see only older ones."""
    return await guard(acc.outstanding_invoices(xero(), kind, min_days_overdue))


@server.tool(annotations=READ)
async def get_invoice(invoice: str) -> dict:
    """One bill or invoice in full, with its lines (account codes, tax, tracking). `invoice` is an
    invoice_id or an invoice number."""
    return await guard(acc.get_invoice(xero(), invoice))


@server.tool(annotations=READ)
async def list_accounts(query: str | None = None, account_type: str | None = None) -> list[dict]:
    """The chart of accounts (active accounts only). Filter by text in the code or name, or by type
    (e.g. EXPENSE, OVERHEADS, REVENUE, BANK)."""
    return await guard(acc.list_accounts(xero(), query, account_type))


@server.tool(annotations=READ)
async def list_tax_rates() -> list[dict]:
    """Active tax rates and their tax_type codes (e.g. INPUT for GST on Expenses)."""
    return await guard(acc.list_tax_rates(xero()))


@server.tool(annotations=READ)
async def list_tracking_categories() -> list[dict]:
    """Tracking categories and their options."""
    return await guard(acc.list_tracking(xero()))


@server.tool(annotations=READ)
async def list_bank_transactions(since: str, limit: int = 100) -> list[dict]:
    """Spend and receive money transactions since a date (YYYY-MM-DD), newest first."""
    return await guard(acc.list_bank_transactions(xero(), since, limit))


@server.tool(annotations=READ)
async def list_payments(since: str, limit: int = 100) -> list[dict]:
    """Payments against invoices and bills since a date (YYYY-MM-DD), newest first."""
    return await guard(acc.list_payments(xero(), since, limit))


@server.tool(annotations=READ)
async def profit_and_loss(from_date: str, to_date: str) -> dict:
    """Profit and loss report for a period (dates YYYY-MM-DD)."""
    return await guard(acc.profit_and_loss(xero(), from_date, to_date))


@server.tool(annotations=READ)
async def balance_sheet(as_at: str) -> dict:
    """Balance sheet as at a date (YYYY-MM-DD)."""
    return await guard(acc.balance_sheet(xero(), as_at))


@server.tool(annotations=READ)
async def list_attachments(invoice_id: str) -> list[dict]:
    """Files attached to a bill or invoice."""
    return await guard(acc.list_attachments(xero(), invoice_id))


@server.tool(annotations=WRITE)
async def download_attachment(invoice_id: str, attachment: str, local_folder: str) -> str:
    """Save an attachment (by attachment_id or file name) to a folder on this computer."""
    return await guard(acc.download_attachment(xero(), cfg, invoice_id, attachment, local_folder))


# ================================================================ writing

class Tracking(BaseModel):
    category: str
    option: str


class Line(BaseModel):
    description: str | None = None
    quantity: float | None = 1
    unit_amount: float | None = None
    account_code: str | None = None
    tax_type: str | None = Field(None, description="e.g. INPUT (GST on Expenses), EXEMPTEXPENSES, BASEXCLUDED")
    tracking: list[Tracking] | None = None


class LineChange(BaseModel):
    line_item_id: str
    description: str | None = None
    quantity: float | None = None
    unit_amount: float | None = None
    account_code: str | None = None
    tax_type: str | None = None
    tracking: list[Tracking] | None = None


def _dump(models: list[BaseModel] | None) -> list[dict] | None:
    return [m.model_dump(exclude_none=True) for m in models] if models else None


@server.tool(annotations=WRITE)
async def create_draft_bill(
    lines: list[Line],
    contact_id: str | None = None,
    contact_name: str | None = None,
    date: str | None = None,
    due_date: str | None = None,
    invoice_number: str | None = None,
    reference: str | None = None,
    amounts_are: str = "Inclusive",
) -> dict:
    """Create a DRAFT bill. `invoice_number` is the supplier's invoice number. Give each line an
    account_code and tax_type from list_accounts / list_tax_rates. Nothing is approved."""
    return await guard(acc.create_draft_bill(
        xero(), lines=_dump(lines), contact_id=contact_id, contact_name=contact_name, date_=date,
        due_date=due_date, invoice_number=invoice_number, reference=reference, amounts_are=amounts_are,
    ))


@server.tool(annotations=WRITE)
async def update_bill_lines(
    invoice_id: str, changes: list[LineChange] | None = None, replace_all_lines: list[Line] | None = None,
) -> dict:
    """Recode or edit the lines of a DRAFT bill. Use `changes` to change existing lines by line_item_id
    (e.g. just their account_code), or `replace_all_lines` to replace every line (e.g. to itemise one
    line into several). Warns if the bill total changes."""
    return await guard(acc.update_bill_lines(xero(), invoice_id, _dump(changes), _dump(replace_all_lines)))


@server.tool(annotations=WRITE)
async def attach_file(invoice_id: str, local_path: str, file_name: str | None = None) -> dict:
    """Attach a file from this computer (inside an allowed folder) to a bill or invoice."""
    return await guard(acc.attach_file(xero(), cfg, invoice_id, local_path, file_name))


@server.tool(annotations=WRITE)
async def create_contact(
    name: str, email: str | None = None, phone: str | None = None, account_number: str | None = None,
    tax_number: str | None = None,
) -> dict:
    """Create a contact. Refuses if one with the same name already exists. `tax_number` is the ABN."""
    return await guard(acc.create_contact(xero(), name, email, phone, account_number, tax_number))


@server.tool(annotations=WRITE)
async def update_contact(
    contact_id: str, name: str | None = None, email: str | None = None, phone: str | None = None,
    account_number: str | None = None, tax_number: str | None = None,
) -> dict:
    """Update a contact's name, email, phone, account number or tax number (ABN). Bank details aren't
    changed here."""
    return await guard(acc.update_contact(xero(), contact_id, email, phone, account_number, tax_number, name))


def _client_can_confirm(ctx: Context) -> bool:
    try:
        caps = ctx.client_capabilities
    except Exception:
        return False
    elicitation = getattr(caps, "elicitation", None) if caps else None
    return bool(elicitation and getattr(elicitation, "form", None) is not None) or (
        elicitation is not None and getattr(elicitation, "url", None) is None
    )


class ApproveConfirmation(BaseModel):
    approve: bool = Field(description="Tick to approve these bills now.")


async def approve_bills(invoice_ids: list[str], ctx: Context, user_confirmed: bool = False) -> dict:
    """Approve one or more DRAFT bills (status becomes AUTHORISED, awaiting payment). Only use when
    the user has asked for approval. The user always confirms first: either the app shows a
    confirmation prompt, or this tool returns a summary that you must show the user, and you may only
    call again with user_confirmed=true after they explicitly say yes. Every line must have an account code."""
    if cfg.approve_mode == "disabled":
        raise ToolError("Approving bills is turned off in this server's settings. Approve them in Xero.")
    bills = await guard(acc.bills_for_approval(xero(), invoice_ids))
    lines = [f"Approve {len(bills)} bill{'s' if len(bills) != 1 else ''}?"]
    for b in bills:
        lines.append(f"• {b['contact']}  {b['number'] or ''}  {b['currency']} {b['total']}  due {b['due_date']}")
    question = "\n".join(lines)

    answer = None
    if _client_can_confirm(ctx):
        try:
            answer = await ctx.elicit(question, ApproveConfirmation)
        except Exception:
            answer = None
    if answer is not None:
        if answer.action != "accept" or not answer.data.approve:
            return {"approved": False, "reason": "The user did not confirm. The bills are still drafts."}
    elif not user_confirmed:
        return {
            "approved": False,
            "needs_confirmation": True,
            "summary": question,
            "next_step": "Show this summary to the user and ask whether to approve. Only if they clearly say yes, "
                         "call approve_bills again with the same invoice_ids and user_confirmed=true.",
        }
    return {"results": await guard(acc.approve_bills(xero(), invoice_ids))}


# Only offered to Claude when the user has turned approving on (`xero-mcp configure --approve-mode confirm`).
if cfg.approve_mode == "confirm":
    server.tool(annotations=APPROVE)(approve_bills)


def run() -> None:
    server.run()
