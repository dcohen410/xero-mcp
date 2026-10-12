# xero-mcp

A local [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) work with
your Xero organisation: look things up, run reports, and create and code **draft** bills with their
PDFs attached.

It runs on your own computer and signs in through a free Xero app that you register yourself
("Mobile or desktop app", OAuth 2.0 with PKCE). There's no client secret, no paid Custom Connection
and no third-party service in the middle. Your sign-in is kept in your operating system's keychain.

## What it does

- **Read:** organisation details, contacts, bills and sales invoices (with line items), outstanding
  and overdue invoices, chart of accounts, tax rates, tracking categories, bank transactions,
  payments, profit and loss, balance sheet, and attachments.
- **Draft bills:** create a draft bill with itemised, coded line items; recode or replace lines on a
  draft; attach the supplier's PDF.
- **Contacts:** create a contact (refuses duplicates) and update email, phone, account number or ABN.
- **Files:** download attachments to, and upload from, folders on your computer that you allow
  (default `~/Downloads`).

## What it won't do

- Delete or void anything, or record payments.
- Change bank details on contacts.
- Approve bills, unless you turn that on (`--approve-mode confirm`). Even then, you confirm every
  approval.

## Setup

You need [uv](https://docs.astral.sh/uv/) (or pipx) and a Xero login.

1. **Register a Xero app** (about two minutes): follow [docs/xero-setup.md](docs/xero-setup.md).
2. **Install:**
   ```bash
   uv tool install git+https://github.com/dcohen410/xero-mcp
   ```
3. **Configure and sign in:**
   ```bash
   xero-mcp configure --client-id <your-app-client-id>
   xero-mcp login
   xero-mcp status
   ```

Xero sign-ins stay valid as long as the server is used at least once every 60 days. If it lapses,
run `xero-mcp login` again.

## Connect it to Claude

**Claude Code:**
```bash
claude mcp add xero -- xero-mcp serve
```

**Claude Desktop:** quit Claude Desktop first (it rewrites its config file when it quits), then add
this to `claude_desktop_config.json` (Settings → Developer → Edit Config), inside the top-level
braces:
```json
"mcpServers": {
  "xero": { "command": "/full/path/to/xero-mcp", "args": ["serve"] }
}
```
Use the full path from `which xero-mcp` (macOS/Linux) or `where xero-mcp` (Windows). If you already
have an `mcpServers` section, add the `"xero"` entry inside it.

## Options

| Command | Effect |
|---|---|
| `xero-mcp configure --local-dir ~/Documents/Invoices` | Folders the server may read and write (repeat to add more; replaces the default) |
| `xero-mcp configure --port 8790` | Different sign-in port (update your Xero app's redirect URI to match) |
| `xero-mcp configure --approve-mode confirm` | Let Claude approve draft bills, with your confirmation each time |
| `xero-mcp login --organisation "Name"` | Pick an organisation when you've connected more than one |
| `xero-mcp logout` | Delete the saved sign-in |

Settings can also come from environment variables prefixed `XERO_MCP_` (for example
`XERO_MCP_CLIENT_ID`).

## Tools

| Tool | What it does |
|---|---|
| `organisation_info` | Name, country, currency, financial year |
| `search_contacts` | Find contacts by name |
| `list_bills` / `list_invoices` | Bills (ACCPAY) or sales invoices, filtered by status, contact and date |
| `outstanding` | Unpaid invoices or bills with days overdue |
| `get_invoice` | One invoice or bill with its line items, by ID or number |
| `list_accounts` / `list_tax_rates` / `list_tracking_categories` | Codes to use on line items |
| `list_bank_transactions` / `list_payments` | Recent transactions and payments |
| `profit_and_loss` / `balance_sheet` | Reports, flattened into rows |
| `list_attachments` / `download_attachment` | Attachments on an invoice or bill |
| `create_draft_bill` | New draft bill with coded line items |
| `update_bill_lines` | Recode or replace lines on a draft bill |
| `attach_file` | Attach a local file (e.g. the supplier PDF) to a bill |
| `create_contact` / `update_contact` | Add or edit a contact |
| `approve_bills` | Only when approve mode is `confirm`; always asks you first |

Example: *"Create a draft bill from the Officeworks invoice in my Downloads folder, code the
stationery to Office Expenses, and attach the PDF."*

## Works well with

[m365-mcp](https://github.com/dcohen410/m365-mcp) for Outlook and OneDrive, so Claude can save an
emailed invoice as a PDF and turn it into a draft bill in one go.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
```

MIT licensed.
