import os

from frappe_microservice import create_microservice, setup_controllers
import frappe_microservice.controller as controller_module

# Initialize microservice
app = create_microservice(
    "expense_tracker",
    port=8000,
    sites_path="/app/sites",
    load_framework_hooks=['frappe', 'erpnext']
)

# Auto-discover and register controllers from the local directory.
controllers_dir = os.path.dirname(os.path.realpath(__file__)) + "/controllers"

# frappe_microservice.setup_controllers() registers controllers into the
# runtime registry returned by get_controller_registry(), but the hook
# dispatcher currently reads from controller_module._registry. Point both at
# the same object so Purchase Invoice lifecycle hooks actually execute.
if hasattr(controller_module, "get_controller_registry"):
    controller_module._registry = controller_module.get_controller_registry()
setup_controllers(app, controllers_directory=controllers_dir)

import expense_tracker.api as expense_tracker_api  # noqa: E402 — after app; registers API routes

# Purchase Invoice: custom delete cancels submitted invoices then deletes.
app.register_resource(
    "Purchase Invoice",
    custom_handlers={
        "get": expense_tracker_api.get_purchase_invoice,
        "post": expense_tracker_api.create_purchase_invoice,
        "delete": expense_tracker_api.delete_purchase_invoice,
    },
)
app.register_resource("Item Group")
app.register_resource("Item")

if __name__ == "__main__":
    app.run()