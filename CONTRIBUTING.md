# Contributing

Contributions are welcome.

If you want to fix a bug, improve documentation, propose a feature, or add a new integration, open an issue first when
the change is non-trivial. Small fixes and typo corrections can go straight to a pull request.

Use the issue tracker for:

- bug reports
- feature requests
- scoped design discussions tied to implementation work

Use pull requests for proposed code or documentation changes.

## Before You Start

- Read [README.md](./README.md) for the product overview.
- Read [docs/architecture.md](./docs/architecture.md) for the system shape.
- Read [docs/development.md](./docs/development.md) for local setup.
- Read [docs/testing.md](./docs/testing.md) before opening a PR.

## Development Workflow

Bootstrap the local environment:

```bash
./agency bootstrap
```

Run the main checks before opening a PR:

```bash
make test
make check-architecture
make check-tool-registry
```

If you changed docs or setup behavior, also review the relevant files under `docs/`.

## What Good Contributions Look Like

- keep route handlers thin and move behavior into services
- treat `app/domain` contracts as canonical
- add concise comments where orchestration, approval, retry, adapter, or guardrail logic is not obvious
- prefer extending the existing architecture over adding a parallel subsystem
- update docs when behavior or setup changes

## Feature Requests

Feature requests are welcome. Open an issue with:

- the problem you are trying to solve
- the current workaround, if any
- the user or operator workflow affected
- a rough proposal, if you already have one

The best feature requests are concrete about outcome, constraints, and tradeoffs.

## Pull Requests

For larger changes:

1. Open an issue first so the design direction is clear.
2. Keep the PR scoped to one problem when possible.
3. Include tests or explain why tests are not practical.
4. Update docs when contracts, setup, or operator behavior changes.

## Questions

If you are unsure where a change belongs, open an issue and ask before implementing it.

## Community Standards

Please follow [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md).

## Security

For security-sensitive reports, do not open a public issue. See [SECURITY.md](./SECURITY.md).
