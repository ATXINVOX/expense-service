import pytest
from unittest.mock import MagicMock, patch
import sys

# 1. Mock the entire dependencies before importing JournalEntry
class MockDocumentController:
    def __init__(self, d):
        for k, v in d.items():
            setattr(self, k, v)

mock_frappe = MagicMock()
mock_microservice = MagicMock()
mock_microservice_controller = MagicMock()
mock_microservice_controller.DocumentController = MockDocumentController

sys.modules["frappe"] = mock_frappe
sys.modules["frappe_microservice"] = mock_microservice
sys.modules["frappe_microservice.controller"] = mock_microservice_controller

# 2. Now import the actual controller
from controllers.journal_entry import JournalEntry

@pytest.fixture
def mock_app():
    app = MagicMock()
    # Mock db.get_value for Expense Claim Type
    app.db.get_value.return_value = {"default_account": "5100 - Travel Expense"}
    
    # Ensure frappe_microservice.get_app() returns this mock_app
    import frappe_microservice
    frappe_microservice.get_app.return_value = app
    
    return app

def test_journal_entry_transformation(mock_app):
    # Simplified data from mobile app
    doc_data = {
        "doctype": "Journal Entry",
        "category": "Travel",
        "amount": 150.0,
        "posting_date": "2024-03-29",
        "user_remark": "Taxi to airport",
        "company": "My Company"
    }
    
    # Create controller instance (mocking the library's base DocumentController)
    doc = JournalEntry(doc_data)
    doc.app = mock_app  # Inject mock app
    
    # Mock external helpers that will be in the same module
    import controllers.journal_entry
    controllers.journal_entry.get_default_cash_account = MagicMock(return_value="1110 - Cash")
    controllers.journal_entry.get_default_cost_center = MagicMock(return_value="Main - CC")
    
    # Run hook
    doc.before_insert()
    
    # Assertions
    assert doc.voucher_type == "Cash Entry"
    assert len(doc.accounts) == 2
    
    # Debit row (Expense)
    assert doc.accounts[0]["account"] == "5100 - Travel Expense"
    assert doc.accounts[0]["debit_in_account_currency"] == 150.0
    
    # Credit row (Cash)
    assert doc.accounts[1]["account"] == "1110 - Cash"
    assert doc.accounts[1]["credit_in_account_currency"] == 150.0
    
    # Verify cleanup
    assert not hasattr(doc, 'category')
    assert not hasattr(doc, 'amount')
