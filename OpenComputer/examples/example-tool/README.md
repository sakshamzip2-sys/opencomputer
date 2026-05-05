# opencomputer-example-tool

A reference third-party plugin for OpenComputer. Ships one tool —
`WordCount` — and demonstrates the full lifecycle: build, test,
publish, install.

**Use this as a template for your own plugin.**

## What it does

```python
WordCount(text="Hello world. How are you?")
# → {"chars": 25, "words": 5, "sentences": 2}
```

## Layout

```
opencomputer-example-tool/
├── plugin.json                    ← OC plugin manifest
├── pyproject.toml                 ← Python package metadata
├── README.md
├── LICENSE
├── example_tool/
│   ├── __init__.py                ← __version__
│   ├── plugin.py                  ← register(api) entry point
│   └── tools.py                   ← WordCount logic
├── tests/
│   └── test_word_count.py
└── .github/workflows/
    └── publish.yml                ← OIDC → PyPI on tag (template)
```

## Step 1 — Fork this template

```bash
# From the OpenComputer repo:
cp -r examples/example-tool ~/my-plugin
cd ~/my-plugin
git init
git add .
git commit -m "Initial commit: my-plugin from example-tool template"
```

## Step 2 — Edit metadata

In `pyproject.toml` and `plugin.json`:

- `name` / `id` — your plugin's id (lowercase, dashes ok)
- `description`, `authors`, `Homepage` — yours
- `version` — start at `0.1.0`

In `example_tool/plugin.py`: rename the class + tool, swap the logic.

## Step 3 — Test locally against OpenComputer

```bash
# Install your plugin into the active OC profile (local-dir mode):
oc plugin install ~/my-plugin

# Verify it loaded:
oc plugins | grep my-plugin

# Try the tool:
oc chat
> use WordCount on "Hello world."
```

## Step 4 — Publish to PyPI

Two options: manual or GitHub Actions OIDC.

### Manual (one-time)

```bash
pip install build twine
python -m build                      # wheels + sdist into dist/
twine upload dist/*                  # prompts for PyPI token
```

### Automated (recommended)

The `.github/workflows/publish.yml` template publishes on every git
tag matching `v*`. Configure once:

1. Create a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/)
   pointing at this repo.
2. Push a tag: `git tag v0.1.0 && git push --tags`.
3. CI builds + uploads via OIDC (no PyPI token in secrets needed).

## Step 5 — Add to a remote catalog

If you want users to install your plugin via
`oc plugin install --remote my-plugin`, add an entry to a JSON catalog
hosted at a URL of your choice:

```json
{
  "schema_version": 1,
  "plugins": [
    {
      "id": "my-plugin",
      "version": "0.1.0",
      "description": "...",
      "tarball_url": "https://github.com/YOU/my-plugin/releases/download/v0.1.0/my-plugin-0.1.0.tgz",
      "tarball_sha256": "<sha256 of the tarball>",
      "license": "MIT"
    }
  ]
}
```

Sign the catalog with `oc plugin catalog sign <catalog.json> --key <key.pem>`
and have users add your public key to their
`~/.opencomputer/trusted_catalog_keys.json`.

Users then run:

```bash
export OC_PLUGIN_CATALOG_URL=https://your.host/catalog.json
oc plugin install --remote my-plugin
```

## Tests

```bash
pip install -e ".[dev]"
pytest tests/
```
