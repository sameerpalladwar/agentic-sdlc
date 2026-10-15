"""Small existing application used as read-only input to the Brownfield scenario."""


class LinkStore:
    """Store URL mappings in memory without fetching user-supplied destinations."""

    def __init__(self) -> None:
        self.links: dict[str, str] = {}
        self.next_id = 1

    def create(self, url: str) -> str:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("Use an HTTP or HTTPS URL.")
        code = f"link{self.next_id}"
        self.next_id += 1
        self.links[code] = url
        return code

    def resolve(self, code: str) -> str | None:
        return self.links.get(code)
