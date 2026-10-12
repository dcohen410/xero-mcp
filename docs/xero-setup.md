# Setting up the Xero app

xero-mcp signs in through an app that **you** register with Xero. It uses Xero's "Mobile or desktop
app" type (OAuth 2.0 with PKCE), so there is no client secret and no Custom Connection to pay for.
It takes about two minutes.

## 1. Create the app

1. Sign in at [developer.xero.com/app/manage](https://developer.xero.com/app/manage) with your Xero login.
2. Click **New app**.
3. Fill in:
   - **App name:** anything that doesn't contain the word "Xero" (Xero rejects those), e.g. `Claude Accounts (your name)`
   - **Integration type:** **Mobile or desktop app**
   - **Company or application URL:** your business website (any URL you own)
   - **Redirect URI:** `http://localhost:8765/callback` (exactly this)
4. Tick the terms and click **Create app**.

## 2. Turn on the permissions

Apps created from March 2026 use Xero's granular scopes. On the app's **Configuration** page, make
sure these are enabled (tick them if your portal shows a scopes list):

| Scope | Used for |
|---|---|
| `accounting.invoices` | Reading invoices and bills, creating and editing draft bills, approving bills |
| `accounting.contacts` | Finding, creating and updating contacts |
| `accounting.settings.read` | Organisation details, chart of accounts, tax rates, tracking categories |
| `accounting.attachments` | Listing, downloading and adding attachments |
| `accounting.payments.read` | Listing payments |
| `accounting.banktransactions.read` | Listing bank transactions |
| `accounting.reports.profitandloss.read` | Profit and loss report |
| `accounting.reports.balancesheet.read` | Balance sheet report |

If sign-in later fails with `invalid_scope`, one of these isn't enabled on the app yet.

## 3. Copy the Client ID

On the **Configuration** page, copy the **Client id**. Don't generate a client secret; it isn't used.

## 4. Configure and sign in

```bash
xero-mcp configure --client-id <client-id>
xero-mcp login
```

`login` opens Xero in your browser. Sign in and choose the organisation to connect. If you connect
more than one, pick the one to use with `xero-mcp login --organisation "<name>"`.

If something else on your computer already uses port 8765, choose another port with
`xero-mcp configure --port 8790` and change the app's redirect URI to match.

## Signing out

`xero-mcp logout` deletes the saved sign-in from your keychain. To cut access off completely,
also disconnect the app in Xero under **Settings → Connected apps**.
