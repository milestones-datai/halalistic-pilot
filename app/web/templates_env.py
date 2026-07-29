"""Jinja2 env for the consumer + owner portal (Stage 11).

Tailwind via Play CDN (no build step). HTMX 1.9 via CDN. We keep the
env separate from `app.admin.templates_env` so the consumer app can
evolve its look independently of the admin console.
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


def render(template_name: str, **context) -> str:
    template = env.get_template(template_name)
    return template.render(**context)
