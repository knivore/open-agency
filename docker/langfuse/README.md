Langfuse is provisioned from the official upstream container images in `docker-compose.yml`.

There is intentionally no custom `Dockerfile` in this directory.

Reason:

- Langfuse v3 is a multi-service stack, not a single application image.
- The recommended local deployment uses the official `langfuse/langfuse` and `langfuse/langfuse-worker` images together
  with Postgres, Redis, ClickHouse, and MinIO.
- For this repository, Compose is the correct integration point because it links the backend container to the Langfuse
  web service without forking or rebuilding Langfuse itself.

Related services in `/Users/kehchinleong/Documents/Personal/Agency/agency/docker-compose.yml`:

- `langfuse-web`
- `langfuse-worker`
- `langfuse-postgres`
- `langfuse-redis`
- `langfuse-clickhouse`
- `langfuse-minio`
