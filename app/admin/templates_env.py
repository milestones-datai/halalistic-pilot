"""Jinja2 environment for the admin UI.

Kept in a single place so templates can do `{% extends "base.html" %}`
without per-route setup. Auto-escaping is on by default — we use
Jinja2's default `select_autoescape(["html"])` to prevent XSS via
restaurant names / review bodies that come from the DB.

HTMX is loaded from a CDN in base.html. We don't bundle it to keep the
build step-free; offline deployments can swap to a local copy later.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_template(template_name: str, **context) -> str:
    """Render a template with the given context. Standard `Template.render`
    plus auto-escaping already enabled.
    """
    template = env.get_template(template_name)
    return template.render(**context)
