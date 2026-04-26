# Releasing OpenComputer

One-time setup happens once; every release after that is ~60 seconds of work.

## One-time setup (first release only)

### 1. Reserve the package name on PyPI

1. Go to https://pypi.org/account/register/ and create an account if you don't have one
2. Enable 2FA — required for publishing
3. Before the first release, manually upload a minimal sdist to claim the name:

```bash
cd OpenComputer
python -m pip install --upgrade build twine
python -m build
python -m twine upload dist/* --username __token__
# (paste an API token from pypi.org/manage/account/#api-tokens)
```

### 2. Configure PyPI trusted publishing (OIDC)

After the first upload, set up trusted publishing so the `release.yml` workflow
can publish without an API token:

1. Visit https://pypi.org/manage/project/opencomputer/settings/publishing/
2. Click **Add a new publisher → GitHub**
3. Fill in:
   - Owner: `sakshamzip2-sys`
   - Repository: `opencomputer`
   - Workflow name: `release.yml`
   - Environment: (leave blank)
4. Save

From now on, tagging `vX.Y.Z` and pushing the tag automatically publishes.

## Cutting a release

### 3. Bump the version (date-stamped)

OpenComputer uses **date-versioned releases** (`YYYY.M.D`, no zero-padding). Pick today's date as the version. Bump only the one place that tracks it:

- `OpenComputer/pyproject.toml` — `version = "YYYY.M.D"`

(`opencomputer.__version__` is auto-derived from `importlib.metadata` and stays in sync.)

```bash
python -c "from opencomputer.release.version import today_version; print(today_version())"
# → 2026.4.27   ← copy this into pyproject.toml
```

Two-or-more releases on the same day → append `.postN` (e.g. `2026.4.27.post1`). Don't reuse a date+post combo.

**Stability commitment:** the `plugin_sdk/*` surface is the contract. Date versioning does not weaken that — any breaking change to `plugin_sdk` is announced explicitly in `CHANGELOG.md` regardless of date.

### 4. Update CHANGELOG

Append a section to `CHANGELOG.md` describing what changed. Short punchy bullets.

### 5. Tag, push, release

```bash
# Verify tests pass locally first
cd OpenComputer
source .venv/bin/activate
pytest tests/

# From the PARENT repo root
cd ..
VERSION=$(python -c "from opencomputer.release.version import today_version; print(today_version())")
git add OpenComputer/pyproject.toml \
        OpenComputer/CHANGELOG.md
git commit -m "Release v$VERSION"
git push

# Tag and push the tag (triggers the release.yml workflow)
git tag "v$VERSION"
git push origin "v$VERSION"
```

GitHub Actions will:
1. Verify the tag matches `pyproject.toml:version`
2. Build sdist + wheel
3. Import-test the wheel
4. Publish to PyPI via trusted publishing

Check the Actions tab on GitHub to watch it. Within ~1 minute, `pip install opencomputer==X.Y.Z` will work on any machine.

## Testing against TestPyPI before PyPI

For risky releases, publish to TestPyPI first:

```bash
python -m build
python -m twine upload --repository-url https://test.pypi.org/legacy/ dist/*
# install from TestPyPI to verify:
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple \
            opencomputer==X.Y.Z
```

Only promote to PyPI once TestPyPI looks good.

## If a release goes bad

1. Yank it on PyPI (not delete — "yank" prevents new installs but leaves
   existing installs working):
   https://pypi.org/manage/project/opencomputer/releases/
2. Bump patch version, fix the issue, tag a new release.
3. Never re-release with the same version number. PyPI refuses.

## Version strategy

- **`YYYY.M.D`** (date-stamped): every release. Cadence is ship-when-ready.
- **`plugin_sdk/*` is the only stability commitment.** Breaking changes there are called out explicitly in CHANGELOG so plugin authors can pin pre-break versions if needed.
- **Two releases on one day:** suffix the second with `.postN` (e.g. `2026.4.27.post1`).
- **Yanking:** if a release is bad, yank on PyPI (don't delete) and tag a new date version. Never reuse a date+post combo.
