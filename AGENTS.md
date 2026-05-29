# tunnelterm

Python 3.12+ with [uv](https://github.com/astral-sh/uv).

## Verify every change

After any modification, run all three before committing or marking done:

```bash
uv run ruff check .    # lint
uv run pyright src/    # type-check
uv run pytest tests/   # test
```

Lint is also auto-formatted: `uv run ruff format .` if ruff reports format issues.

## Package layout

- `src/tunnelterm/` — main package; entry point is `tunnelterm.__main__:main`
- `tests/` — pytest (asyncio_mode=auto, excluded from ruff)
- Vendored static assets (themes, fonts) in `src/tunnelterm/static/`

## Version management

Version is derived from git tags by setuptools-scm:
- Tagged `v1.2.3` → `1.2.3`
- Untagged HEAD → `1.2.4.dev3+g<sha>` (post-release scheme)
- CI strips the `+g<sha>` suffix because PyPI rejects local versions

## Linter conventions

Ruff configured with: `line-length = 100`, `target-version = "py312"`.

Notable ignores: `D100`, `D104` (docstrings not required on modules/packages), `E501` (line length handled by formatter).

## Security model (do not weaken)

- Session token in `HttpOnly; Secure; SameSite=Strict` cookie — JS must never see it
- Server **refuses to start** on non-loopback bind without `--allowed-origin`
- Rate limits: 5 failed auth/IP/15min → 5min lockout; verify endpoint 60/IP/min