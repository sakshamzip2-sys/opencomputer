"""Weather Example provider plugin — entry module.

The plugin loader imports this file and calls ``register(api)``. The
provider class lives in ``provider.py`` next to this file; the loader
puts the plugin root on ``sys.path`` so the plain ``from provider import ...``
form works when the plugin is loaded at runtime. The fallback import
path is used when the plugin is imported as a package during testing.
"""

from __future__ import annotations

try:
    from provider import WeatherExampleProvider  # plugin-loader mode
except ImportError:  # pragma: no cover
    from weather_example.provider import WeatherExampleProvider  # package mode


def register(api) -> None:  # PluginAPI is duck-typed
    """Register this provider under a short, unique name."""
    api.register_provider("weather_example", WeatherExampleProvider)
