"""Task storage + authoring.

Interview tasks moved from the filesystem (`tasks/<id>/`) into the DB. This
package is the DB source of truth:

- `store` — `LoadedTask` + resolution loaders + access predicate (the runtime seam).
- `images` — the shipped `runtime_image` whitelist.
- `seed` — idempotent import of the repo's reference tasks into template rows.
- `schemas` / `rest` — REST DTOs + CRUD endpoints.
"""
