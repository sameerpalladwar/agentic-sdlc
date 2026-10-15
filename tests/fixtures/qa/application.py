"""Trusted hand-written fixture application for QA runner integration only."""
class LinkStore:
    def __init__(self):
        self.links: dict[str, str] = {}

    def create(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise ValueError("Expected HTTP or HTTPS URL")
        code = f"link{len(self.links) + 1}"
        self.links[code] = url
        return code

    def resolve(self, code: str) -> str | None:
        return self.links.get(code)
