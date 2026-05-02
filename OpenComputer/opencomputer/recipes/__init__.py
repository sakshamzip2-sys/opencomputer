"""OpenCLI-style recipe layer for browser scraping commands.

Public API:
    load_recipe(site)       -> Recipe
    run_recipe(site, verb, *, args, fetcher, fmt) -> str
    list_recipes()          -> list[str]
"""
