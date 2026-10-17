"""Sync system categories from taxonomy.yml into Postgres (idempotent upsert by slug).

Runs with the admin role (system rows have user_id NULL and are not writable by the app
role under RLS). Called at app startup and by the test bootstrap.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import create_engine, text

from finpilot.categories.taxonomy import load_taxonomy
from finpilot.config import get_settings

log = logging.getLogger(__name__)

_UPSERT = text("""
INSERT INTO categories (user_id, slug, name, flow_type, nature, color, icon, description,
                        examples_json, is_system)
VALUES (NULL, :slug, :name, :flow_type, :nature, :color, :icon, :description,
        CAST(:examples AS jsonb), true)
ON CONFLICT (slug) WHERE user_id IS NULL DO UPDATE SET
  name = EXCLUDED.name, flow_type = EXCLUDED.flow_type, nature = EXCLUDED.nature,
  color = EXCLUDED.color, icon = EXCLUDED.icon, description = EXCLUDED.description,
  examples_json = EXCLUDED.examples_json, archived = false
""")


def sync_system_categories(url: str | None = None) -> int:
    entries = load_taxonomy()
    engine = create_engine(url or get_settings().database_admin_url)
    try:
        with engine.begin() as c:
            for e in entries:
                c.execute(
                    _UPSERT,
                    {**e.model_dump(exclude={"examples"}), "examples": json.dumps(e.examples)},
                )
            # Retire system categories removed from the YAML (keep rows: transactions may reference them).
            c.execute(
                text(
                    "UPDATE categories SET archived = true WHERE user_id IS NULL AND NOT (slug = ANY(:s))"
                ),
                {"s": [e.slug for e in entries]},
            )
    finally:
        engine.dispose()
    log.info("system categories synced: %d", len(entries))
    return len(entries)
