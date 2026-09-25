# Working on cARPaccio

## Development

The logo and icon art live in `docs/images/` (full-size originals, not shipped).
The browser icons in `src/carpaccio/static/` and the README logo are generated
from them — after changing the art, run `python3 scripts/make_icons.py`
(needs Pillow) and commit the output.

```sh
uv sync --all-groups
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The test fixtures under `tests/fixtures/unifi/` are **synthetic**: generated
by `tests/fixtures/sanitise.py` from real controller responses that never
entered the repo. They keep the exact structure and counts of the real API
payloads (87 clients, 17 reservations, 8 active randomised MACs, an empty
firewall-policy list) with every MAC, address, name, token and coordinate
replaced. The real data lives only in the owner's S3 bucket.

