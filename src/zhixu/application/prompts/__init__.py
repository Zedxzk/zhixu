"""Packaged model prompts, kept out of the code that applies them.

Prompts are data, not logic: keeping them in their own files makes them
reviewable on their own and keeps the routing code readable. Templates use
``$name`` placeholders so a prompt may contain JSON braces safely.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from string import Template


@cache
def _template(name: str) -> Template:
    text = files(__name__).joinpath(name).read_text(encoding="utf-8")
    return Template(text.strip())


def render_prompt(name: str, /, **values: str) -> str:
    """Return a packaged prompt with every placeholder substituted.

    Raises KeyError when the template needs a placeholder that was not given,
    so a missing value can never reach the model as literal ``$name`` text.
    """

    return _template(name).substitute(values)
