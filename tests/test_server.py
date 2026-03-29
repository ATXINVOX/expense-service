from importlib import import_module
from types import ModuleType
from unittest.mock import MagicMock
import sys
from urllib.parse import quote


def _mock_frappe_microservice(app):
    fake_ms = ModuleType("frappe_microservice")
    def mock_secure_route(rule, **options):
        def decorator(f):
            return f
        return decorator

    fake_ms.create_microservice = MagicMock(return_value=app)
    fake_ms.setup_controllers = MagicMock()
    fake_ms.get_app = MagicMock(return_value=app)
    app.secure_route.side_effect = mock_secure_route
    fake_controller = ModuleType("frappe_microservice.controller")
    fake_controller.DocumentController = object

    return fake_ms, fake_controller


def test_server_registers_purchase_invoice_and_item_group_resources():
    original_ms = sys.modules.get("frappe_microservice")
    original_controller = sys.modules.get("frappe_microservice.controller")
    original_server = sys.modules.pop("server", None)

    app = MagicMock()
    fake_ms, fake_controller = _mock_frappe_microservice(app)
    sys.modules["frappe_microservice"] = fake_ms
    sys.modules["frappe_microservice.controller"] = fake_controller

    try:
        import_module("server")
        fake_ms.create_microservice.assert_called()
        fake_ms.setup_controllers.assert_called()
        app.register_resource.assert_any_call("Purchase Invoice")
        app.register_resource.assert_any_call("Item Group")
    finally:
        if original_ms is not None:
            sys.modules["frappe_microservice"] = original_ms
        else:
            sys.modules.pop("frappe_microservice", None)

        if original_controller is not None:
            sys.modules["frappe_microservice.controller"] = original_controller
        else:
            sys.modules.pop("frappe_microservice.controller", None)

        if original_server is None:
            sys.modules.pop("server", None)
        else:
            sys.modules["server"] = original_server


def _import_server_with_mocks():
    original_ms = sys.modules.get("frappe_microservice")
    original_controller = sys.modules.get("frappe_microservice.controller")
    original_server = sys.modules.pop("server", None)

    app = MagicMock()
    fake_ms, fake_controller = _mock_frappe_microservice(app)
    sys.modules["frappe_microservice"] = fake_ms
    sys.modules["frappe_microservice.controller"] = fake_controller

    return app, original_ms, original_controller, original_server


def _restore_server_mocks(original_ms, original_controller, original_server):
    if original_ms is not None:
        sys.modules["frappe_microservice"] = original_ms
    else:
        sys.modules.pop("frappe_microservice", None)

    if original_controller is not None:
        sys.modules["frappe_microservice.controller"] = original_controller
    else:
        sys.modules.pop("frappe_microservice.controller", None)

    if original_server is None:
        sys.modules.pop("server", None)
    else:
        sys.modules["server"] = original_server


def test_item_group_resource_has_expected_endpoint_contract():
    app, original_ms, original_controller, original_server = _import_server_with_mocks()
    try:
        import_module("server")

        registered = [
            quote(call.args[0]) for call in app.register_resource.call_args_list
        ]
        assert "Item%20Group" in registered
    finally:
        _restore_server_mocks(original_ms, original_controller, original_server)
