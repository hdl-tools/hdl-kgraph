# Releasing hdl-kgraph to PyPI

Releases are published automatically by `.github/workflows/release.yml` using
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC) —
no API tokens are stored anywhere. Pushing a version tag (`v*`) triggers the
workflow, which runs the test suite, builds the sdist + wheel, validates them
with `twine check`, and uploads them to PyPI (publish only runs when tests and
the build both pass). Creating a GitHub Release in the web UI also works: it
creates and pushes the tag, which fires the same trigger.

The package version is single-sourced from `__version__` in
`src/hdl_kgraph/__init__.py` (hatchling `[tool.hatch.version]`).

## One-time setup

1. Create an account on [pypi.org](https://pypi.org), verify the email
   address, and enable 2FA (mandatory on PyPI).
2. On PyPI: **Account settings → Publishing → Add a new pending publisher**
   (GitHub tab) with exactly:
   - PyPI project name: `hdl-kgraph`
   - Owner: `chuanseng-ng`
   - Repository: `hdl-kgraph`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   A *pending* publisher reserves the trust relationship before the project
   exists; the first successful publish creates the PyPI project and converts
   it into a regular trusted publisher.
3. On GitHub: **Settings → Environments → New environment** named `pypi`.
   Optionally add yourself as a required reviewer so every publish needs a
   manual approval click.

## Cutting a release

1. Roll the changelog: in `CHANGELOG.md`, rename the `[Unreleased]` section to a
   new dated `## [<version>] - YYYY-MM-DD` section (leaving a fresh, empty
   `[Unreleased]` above it), and add a corresponding compare link at the bottom.
2. Make sure `__version__` in `src/hdl_kgraph/__init__.py` is the version you
   want to ship and that CI is green on the target commit.
3. Sanity-check the distribution locally (optional but recommended):

   ```sh
   python -m pip install build twine
   python -m build
   twine check dist/*
   ```

4. Tag and push:

   ```sh
   git tag v<version>        # e.g. v0.1.0, on the release commit
   git push origin v<version>
   ```

   or create a GitHub Release for the new tag via **Releases → Draft a new
   release** (recommended) and paste that version's `CHANGELOG.md` section as
   the release notes. Either way the workflow builds and uploads to PyPI.
5. Verify from a clean environment:

   ```sh
   python -m venv /tmp/venv && /tmp/venv/bin/pip install hdl-kgraph==<version>
   /tmp/venv/bin/hdl-kgraph --version
   ```

## Manual fallback (twine)

If GitHub Actions is unavailable:

1. PyPI → Account settings → API tokens → create a token. Before the project
   exists this must be scoped to the **entire account** (project-scoped
   tokens require an existing project); replace it with a project-scoped
   token or trusted publishing afterwards.
2. Build and upload:

   ```sh
   python -m pip install build twine
   python -m build
   twine check dist/*
   twine upload dist/*   # username: __token__, password: pypi-...
   ```
