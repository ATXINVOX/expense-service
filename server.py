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


def _purchase_invoice_get(user, name):
    """GET one — body from frappe.as_json() so timedelta/date never hit Flask jsonify()."""
    import frappe
    from flask import Response

    try:
        doc = app.tenant_db.get_doc("Purchase Invoice", name)
        body = frappe.as_json(doc.as_dict())
        return Response(body, mimetype="application/json", status=200)
    except frappe.PermissionError:
        return {"error": "Access denied"}, 403
    except frappe.DoesNotExistError:
        return {"error": "Purchase Invoice not found"}, 404


# Register resources for this service. Item Group is used for expense category grouping.
app.register_resource(
    "Purchase Invoice",
    custom_handlers={"get": _purchase_invoice_get},
)
app.register_resource("Item Group")
app.register_resource("Item")

import expense_tracker.api  # Register whitelisted custom API method after app is ready

if __name__ == "__main__":
    app.run()