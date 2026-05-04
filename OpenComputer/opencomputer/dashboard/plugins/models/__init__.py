"""Models-analytics dashboard plugin (Wave 6.D).

Per-model analytics dashboard backend: cost, latency, cache-hit rate,
session count over a sliding window. Mounted at
``/api/plugins/models/`` by the FastAPI dashboard host.

Hermes-equivalent: ``e6b05eaf6 feat: add Models dashboard tab with rich
per-model analytics`` + ``3c27efbb9 feat(dashboard): configure main +
auxiliary models from Models page``. We ship the analytics-read half;
the model-config-write half is deferred (it requires the same
write-token UX gate the plugin enable/disable endpoints need).
"""
