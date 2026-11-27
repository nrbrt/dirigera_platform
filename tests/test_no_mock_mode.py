"""
The upstream mock mode is gone. Entering "mock" as the hub address skipped
pairing and stored the token "mock", but nothing loaded the mock devices any
more (their imports went in upstream f56b12b), so setup then tried to reach a
hub called "mock" and failed on the DNS lookup (upstream #157). The unit tests
with real hub payloads do what the mock mode was for.

"mock" is now an ordinary, invalid address: no code path may treat it as
special, and the mock devices must not come back as dead code.
"""
import ast
import os

COMPONENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "dirigera_platform",
)


def _sources():
    for root, _, files in os.walk(COMPONENT):
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_sources_are_scanned():
    # Positive control: the walk must reach the files that used to compare with "mock".
    names = {os.path.basename(p) for p in _sources()}
    assert {"config_flow.py", "__init__.py"} <= names


def test_no_code_treats_mock_as_an_address():
    hits = []
    for path in _sources():
        with open(path) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "mock":
                hits.append("%s:%d" % (os.path.relpath(path, COMPONENT), node.lineno))
    assert hits == []


def test_mock_devices_are_gone():
    assert not os.path.exists(os.path.join(COMPONENT, "mocks"))
