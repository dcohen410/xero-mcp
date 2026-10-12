from types import SimpleNamespace

import httpx
import pytest
import respx

from xero_mcp import auth, server
from tests.conftest import BILL_ID, bill


async def test_refresh_rotates_and_saves_token(cfg):
    auth.save_refresh_token(cfg, "old-refresh")
    with respx.mock() as router:
        route = router.post(auth.TOKEN_URL).respond(json={"access_token": "A1", "refresh_token": "new-refresh",
                                                         "expires_in": 1800})
        async with httpx.AsyncClient() as client:
            provider = auth.TokenProvider(cfg, client)
            assert await provider() == "A1"
            assert await provider() == "A1"  # cached, no second refresh
        assert route.call_count == 1
        assert b"refresh_token=old-refresh" in route.calls[0].request.content
        assert b"client_secret" not in route.calls[0].request.content
    assert auth.load_refresh_token(cfg) == "new-refresh"


async def test_not_signed_in(cfg):
    with pytest.raises(auth.NotSignedIn, match="xero-mcp login"):
        await auth.TokenProvider(cfg)()


async def test_expired_refresh_token_asks_to_log_in(cfg):
    auth.save_refresh_token(cfg, "dead")
    with respx.mock() as router:
        router.post(auth.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
        async with httpx.AsyncClient() as client:
            with pytest.raises(auth.NotSignedIn, match="60 days"):
                await auth.TokenProvider(cfg, client)()


def test_pkce_challenge_matches_verifier():
    import base64
    import hashlib
    verifier, challenge = auth._pkce_pair()
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class FakeCtx:
    def __init__(self, caps=None, answer=None):
        self.client_capabilities = caps
        self._answer = answer
        self.asked = None

    async def elicit(self, message, schema):
        self.asked = message
        return self._answer


CODED = bill(lines=[{"LineItemID": "L1", "Description": "Pens", "AccountCode": "453", "TaxType": "INPUT"}])
FORM = SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace(), url=None))


@pytest.fixture(autouse=True)
def use_test_xero(xero, monkeypatch):
    monkeypatch.setattr(server, "_xero", xero)
    monkeypatch.setattr(server.cfg, "approve_mode", "confirm")


async def test_approve_asks_in_conversation_without_prompt_support(mock):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [CODED]})
    approve = mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [{**CODED, "Status": "AUTHORISED"}]})
    out = await server.approve_bills([BILL_ID], FakeCtx())
    assert out["needs_confirmation"] and "Supplier Pty Ltd" in out["summary"] and not approve.called
    out = await server.approve_bills([BILL_ID], FakeCtx(), user_confirmed=True)
    assert out["results"][0]["approved"] is True


async def test_declined_prompt_overrides_model_flag(mock):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [CODED]})
    approve = mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [CODED]})
    out = await server.approve_bills([BILL_ID], FakeCtx(FORM, SimpleNamespace(action="decline", data=None)),
                                     user_confirmed=True)
    assert out["approved"] is False and not approve.called


async def test_accepted_prompt_approves(mock):
    mock.get(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [CODED]})
    approve = mock.post(f"/Invoices/{BILL_ID}").respond(json={"Invoices": [{**CODED, "Status": "AUTHORISED"}]})
    ctx = FakeCtx(FORM, SimpleNamespace(action="accept", data=SimpleNamespace(approve=True)))
    out = await server.approve_bills([BILL_ID], ctx)
    assert approve.called and out["results"][0]["approved"]
    assert ctx.asked.startswith("Approve 1 bill?")


async def test_approving_can_be_disabled(monkeypatch):
    monkeypatch.setattr(server.cfg, "approve_mode", "disabled")
    with pytest.raises(Exception, match="turned off"):
        await server.approve_bills([BILL_ID], FakeCtx(), user_confirmed=True)
