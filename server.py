from frappe_microservice import create_microservice, setup_controllers
import os

# Initialize microservice
app = create_microservice(
    "expense-service",
    port=8000,
    sites_path="/app/sites",
    load_framework_hooks=['frappe', 'erpnext', 'hr']  # HR is where Expense Claim Type is
)

# Auto-discover and register controllers (including Journal Entry)
# This will pick up controllers/journal_entry.py from the local directory
controllers_dir = os.path.dirname(os.path.realpath(__file__)) + "/controllers"
setup_controllers(app, controllers_directory=controllers_dir)

# Register Resources for automatic CRUD via Resource API
# The Journal Entry controller logic handles the simplification
app.register_resource("Journal Entry")
app.register_resource("Expense Claim Type")

if __name__ == "__main__":
    app.run()
