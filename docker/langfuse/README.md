Langfuse is provisioned from the official upstream container images in `docker-compose.yml`.

There is intentionally no custom `Dockerfile` in this directory.

Reason:

- Langfuse v3 is a multi-service stack, not a single application image.
- The recommended local deployment uses the official `langfuse/langfuse` and `langfuse/langfuse-worker` images together
  with Postgres, Redis, ClickHouse, and MinIO.
- For this repository, Compose is the correct integration point because it links the backend container to the Langfuse
  web service without forking or rebuilding Langfuse itself.

Related services in [`../../docker-compose.yml`](../../docker-compose.yml):

- `langfuse-web`
- `langfuse-worker`
- `langfuse-postgres`
- `langfuse-redis`
- `langfuse-clickhouse`
- `langfuse-minio`

Agency does not write directly to the Langfuse databases. When `OBSERVABILITY_EXPORTERS` includes `langfuse`, the
backend Langfuse SDK sends redacted execution observations to `LANGFUSE_BASE_URL`/`LANGFUSE_HOST`. In local Compose this
is `http://langfuse-web:3001` from the backend container and `http://localhost:3001` from the host.

The event mapping is:

- LLM responses become Langfuse `generation` observations with prompt input, response output, model name, token usage,
  execution id, agent id, task id, sequence, and redaction metadata.
- Tool calls become Langfuse `tool` observations with redacted arguments/output, risk labels, execution id, agent id,
  task id, and tool call id.
- Approval decisions, runtime lifecycle events, marketplace/import actions, and other execution events become spans.

Keep `OBSERVABILITY_REDACT_SECRETS=true` unless you are running an isolated redaction test with fake data.
