import os
import tempfile

# Keep tests away from any real settings or saved sign-in.
os.environ["XERO_MCP_CONFIG_DIR"] = tempfile.mkdtemp(prefix="xeromcp-test-")

import httpx
import keyring
import pytest
import respx
from keyring.backend import KeyringBackend

from xero_mcp.api import BASE, Xero
from xero_mcp.config import Config


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring():
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    return kr


@pytest.fixture
def mock():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def xero():
    async def token():
        return "test-token"
    x = Xero(token, "TENANT", httpx.AsyncClient())
    yield x
    await x.aclose()


@pytest.fixture
def cfg(tmp_path):
    return Config(client_id="cid", tenant_id="TENANT", local_dirs=[str(tmp_path)])


BILL_ID = "11111111-2222-3333-4444-555555555555"


def bill(status="DRAFT", lines=None, total=115.0, **extra):
    return {
        "InvoiceID": BILL_ID, "Type": "ACCPAY", "InvoiceNumber": "INV-1", "Status": status,
        "Contact": {"Name": "Supplier Pty Ltd"}, "Date": "/Date(1760227200000+0000)/",
        "DueDate": "/Date(1760313600000+0000)/", "Total": total, "AmountDue": total, "CurrencyCode": "AUD",
        "LineItems": lines if lines is not None else [
            {"LineItemID": "L1", "Description": "Pens", "Quantity": 1, "UnitAmount": 72, "LineAmount": 72,
             "AccountCode": None, "TaxType": "INPUT"},
            {"LineItemID": "L2", "Description": "Snacks", "Quantity": 1, "UnitAmount": 43, "LineAmount": 43,
             "AccountCode": None, "TaxType": "INPUT"},
        ],
        **extra,
    }
