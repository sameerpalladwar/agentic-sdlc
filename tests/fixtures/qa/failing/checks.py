"""Hand-written intentional failure for runner integration testing."""

from application import LinkStore


def test_short_link_lookup():
    store = LinkStore()
    code = store.create("https://example.com")
    assert store.resolve(code) == "https://wrong.example"
