# Integrations

This directory is the mutable runtime boundary for user-extensible tools and integrations.

## Purpose

Code under `integrations/` is treated differently from built-in application code under `app/tools/implementations/**`.

This directory is the source boundary used for:

- integration discovery
- manifest validation
- runtime packaging
- runtime fingerprinting in later phases

## Ownership Split

- `app/tools/implementations/**`
    - app-owned built-in tools
- `integrations/**`
    - user-extensible runtime integrations

## Required Structure

Each integration should live in its own directory:

- `integrations/<integration_name>/manifest.yaml`
- `integrations/<integration_name>/tools.py`
- `integrations/<integration_name>/requirements.txt`

## Manifest Contract

Each `manifest.yaml` must define at least:

- `id`
- `name`
- `module_root`
- `tool_modules`

Example:

```yaml
id: sample-integration
name: Sample Integration
version: 0.1.0
enabled: true
module_root: integrations.sample_integration
tool_modules:
  - integrations.sample_integration.tools
requirements_file: requirements.txt
env:
  - SAMPLE_API_KEY
capabilities:
  allow_network: false
  allow_filesystem: false
metadata:
  owner: example
```

## Rules

- `tool_modules` must resolve to real Python module files.
- Discovery is deterministic and sorted by integration name and id.
- Invalid manifests are skipped by default unless discovery is invoked in strict mode.
- New mutable runtime code should be added here rather than under app-owned built-in tool directories.
