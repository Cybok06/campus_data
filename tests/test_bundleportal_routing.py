import ast
from pathlib import Path


def _checkout_function(name):
    tree = ast.parse(Path("checkout.py").read_text(encoding="utf-8-sig"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_bundleportal_maps_both_at_services_to_airtel_tigo():
    node = _checkout_function("_resolve_bundleportal_network")
    namespace = {"_resolve_dataconnect_network": lambda service, item: None}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "checkout.py", "exec"), namespace)
    resolve = namespace["_resolve_bundleportal_network"]

    assert resolve({"name": "AT - iShare"}, {}) == "airteltigo"
    assert resolve({"name": "AT - BigTime"}, {}) == "airteltigo"
    assert resolve({"name": "MTN"}, {}) is None


def test_bundleportal_submit_normalizes_legacy_ishare_network():
    class Response:
        ok = True
        text = ""

        @staticmethod
        def json():
            return {"success": True, "data": {"order_id": "BP-1"}}

    class Requests:
        def __init__(self):
            self.body = None

        def post(self, url, **kwargs):
            self.body = kwargs["json"]
            return Response()

    fake_requests = Requests()
    namespace = {
        "requests": fake_requests,
        "_clean_api_key": lambda value: value,
        "BUNDLEPORTAL_API_KEY": "test-key",
        "BUNDLEPORTAL_BASE_URL": "https://api.bundleportal.test/v1",
        "BUNDLEPORTAL_TIMEOUT": 45,
    }
    node = _checkout_function("_bundleportal_submit_single")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "checkout.py", "exec"), namespace)

    result = namespace["_bundleportal_submit_single"]("0240000000", 2, "ishare", "ORDER-1")

    assert result["ok"] is True
    assert fake_requests.body["network"] == "airteltigo"


def test_store_checkout_uses_bundleportal_specific_network_resolver():
    source = Path("routes/store_page.py").read_text(encoding="utf-8-sig")

    assert source.count("_resolve_bundleportal_network") >= 3
    assert "portal_network = (_resolve_bundleportal_network(svc_doc, item)" in source
