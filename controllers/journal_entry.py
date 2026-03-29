from frappe_microservice.controller import DocumentController
import frappe

class JournalEntry(DocumentController):
    def before_insert(self):
        """
        Intercepts simplified input and expands it into a double-entry Journal Entry.
        Expects: { category, amount, posting_date, user_remark, company }
        """
        # 1. Resolve accounting details
        # Using self.get to safely access fields that may be present
        category_name = getattr(self, 'category', None)
        amount = getattr(self, 'amount', 0.0)
        
        if not category_name:
            # If no category, assume it's a standard Journal Entry being submitted directly
            return

        # Fetch Category details using the app's tenant-aware DB
        # The 'app' instance is accessible via the library's local reference
        from frappe_microservice import get_app
        app = get_app()
        
        category = app.db.get_value("Expense Claim Type", category_name, ["default_account"])
        if not category or not category.get("default_account"):
            frappe.throw(f"Default account not found for expense category: {category_name}")
            
        expense_account = category.get("default_account")
        cash_account = get_default_cash_account(self.company)
        cost_center = get_default_cost_center(self.company)
        
        # 2. Re-construct the 'accounts' table for Journal Entry
        self.voucher_type = "Cash Entry"
        self.accounts = [
            {
                "account": expense_account,
                "debit_in_account_currency": amount,
                "cost_center": cost_center
            },
            {
                "account": cash_account,
                "credit_in_account_currency": amount,
                "cost_center": cost_center
            }
        ]
        
        # Add a remark if description is set (comes in as user_remark from mobile)
        if not self.user_remark and getattr(self, 'description', None):
            self.user_remark = self.description
            
        # Clean up temporary fields used by mobile app to prevent DB errors
        # if they don't exist in the Journal Entry schema.
        for field in ['category', 'amount', 'description']:
            if hasattr(self, field):
                delattr(self, field)

def get_default_cash_account(company):
    """Fallback to Default Cash Account in Company."""
    account = frappe.db.get_value("Company", company, "default_cash_account")
    if not account:
        # Emergency fallback if setup is incomplete
        account = frappe.db.get_value("Account", {"account_type": "Cash", "company": company})
    return account

def get_default_cost_center(company):
    """Common erpnext helper logic."""
    import erpnext
    return erpnext.get_default_cost_center(company)
