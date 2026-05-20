# Release Process

## Normal release flow

1. Bump version in `pyproject.toml` and `README.md` status line.
2. Update `CHANGELOG.md`.
3. Commit: `git commit -m "vX.Y.Z — <summary>"`
4. Tag: `git tag vX.Y.Z`
5. Push tag: `git push origin vX.Y.Z`

The [publish-pypi workflow](../.github/workflows/publish-pypi.yml) fires automatically on any `v*.*.*` tag push.
It builds the wheel + sdist, then publishes to PyPI via **OIDC Trusted Publisher** — no API token in repository secrets.

## One-time PyPI setup (required before first publish)

The workflow uses OIDC, so PyPI must trust this repository before any tag-push will succeed.

1. Log in to https://pypi.org as `mosandlt`.
2. Go to **Account settings → Publishing → Add a new pending publisher**.
3. Fill in:
   - **PyPI project name:** `bosch-smart-home-camera-mcp`
   - **Owner:** `mosandlt`
   - **Repository name:** `Bosch-Smart-Home-Camera-Tool-MCP`
   - **Workflow filename:** `publish-pypi.yml`
   - **Environment name:** `pypi`
4. Save. No further action needed — the next tag-push will publish automatically.

Also create a GitHub environment named `pypi` in the repo settings
(Settings → Environments → New environment → name it `pypi`).
The environment is optional for PyPI's side but matches the `environment: pypi` field in the workflow,
which enables GitHub's optional deployment approval gate if you want one in the future.

## Verifying a release

```bash
# Check PyPI version (allow ~2 min for CDN propagation)
pip index versions bosch-smart-home-camera-mcp

# Or:
pip install bosch-smart-home-camera-mcp==X.Y.Z --dry-run
```

## Manual fallback (only if CI is broken)

```bash
pip install build twine
python -m build
twine upload dist/*
```
You will be prompted for a PyPI API token (generate one at https://pypi.org/manage/account/token/).
