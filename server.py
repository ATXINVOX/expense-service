from frappe_microservice import create_microservice, setup_controllers
import os

# Initialize microservice
app = create_microservice(
    "expense_tracker",
    port=8000,
    sites_path="/app/sites",
    load_framework_hooks=['frappe', 'erpnext']
)

# Auto-discover and register controllers from the local directory.
controllers_dir = os.path.dirname(os.path.realpath(__file__)) + "/controllers"
setup_controllers(app, controllers_directory=controllers_dir)

# Register resources for this service. Item Group is used for expense category grouping.
app.register_resource("Purchase Invoice")
app.register_resource("Item Group")

import expense_tracker.api  # Register whitelisted custom API method after app is ready

if __name__ == "__main__":
    app.run()
