# Database Exports

This directory is for local-only development database snapshots created by:

```bash
python scripts/db_snapshot.py export --database agency
```

On Windows, run the same helper from PowerShell:

```powershell
py scripts\db_snapshot.py export --database agency
py scripts\db_snapshot.py import --database agency --yes
```

Snapshots can contain credentials, API tokens, memory records, conversations, and other local application data. Do not
commit raw exports; the repository ignores `*.dump` and `*.json` files here. Use sanitized fixtures elsewhere for any
artifact that must be shared in git.

Default snapshot files:

- `agency.dump` exports the main app Postgres database from the `postgres` Docker Compose service.
- `langfuse-postgres.dump` exports the Langfuse Postgres database from the `langfuse-postgres` service when requested
  with `--database langfuse` or `--database all`.
