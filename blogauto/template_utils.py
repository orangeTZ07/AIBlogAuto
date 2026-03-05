from __future__ import annotations


def render_template_placeholders(template: str, **values: object) -> str:
    """Replace known placeholders like {title} without evaluating expressions."""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered
