# Runtime Adapters

## Overview

Runtime adapters isolate framework-specific execution engines from the canonical app model.

This document focuses on adapter contracts and adapter inventory. For the current isolated execution operating model,
worker lifecycle, reconciler behavior, metrics, and operator runtime surface,
see [runtime.md](./runtime.md).

The active adapter surface is:

- `app/runtime/registry.py`
- `app/runtime/adapters/base.py`
- `app/runtime/adapters/native_adapter.py`
- `app/runtime/adapters/crewai/`

## Base Interface

`app/runtime/adapters/base.py` defines:

- `BaseRuntimeAdapter`
- `RuntimeAdapterStatus`
- `RuntimeAdapterCapability`
- `RuntimeAdapterUnavailableError`
- `RuntimeAdapterUnsupportedError`

The base layer owns only shared adapter contracts and status reporting. It does not contain framework-specific execution
logic.

## Registry

`app/runtime/registry.py` owns runtime selection and dispatch.

Stable adapter keys:

- `native`
- `crewai`

Routes, services, and the control plane should resolve execution through the registry instead of importing adapter
implementations directly.

## Native Adapter

`app/runtime/adapters/native_adapter.py` is a thin wrapper over the native execution engine.

Properties:

- no CrewAI imports
- full control-plane support for pause, resume, and cancel
- canonical execution events emitted directly by the native runtime
- the only adapter currently supported for isolated container-hosted execution
- supports runtime Agency Graph context retrieval and graph working-set state when graph context is enabled

## CrewAI Adapter

`app/runtime/adapters/crewai/adapter.py` is the only active CrewAI runtime adapter class.

Supporting modules:

- `mapper.py`
    - CrewAI payload and object mapping
    - LLM selection helpers used by compatibility schemas
- `tools.py`
    - wraps app-owned tools for CrewAI
- `events.py`
    - callback log formatting and internal event replay
- `availability.py`
    - installed/unavailable checks
- `errors.py`
    - CrewAI-specific adapter errors

Rules:

- CrewAI imports must stay inside `app/runtime/adapters/crewai/`
- canonical domain models stay in `app/domain`
- app services, routes, and the native runtime do not instantiate CrewAI classes directly
- app-owned tool implementations remain under `app/tools`

## Unsupported Operations

CrewAI is treated as an optional compatibility adapter, not the primary runtime.

Current behavior:

- `start_execution`: supported
- `get_execution_state`: supported
- `pause_execution`: unsupported
- `resume_execution`: unsupported
- `cancel_execution`: unsupported
- isolated container-hosted execution: unsupported
- automatic Agency Graph runtime-context retrieval: unsupported

Unsupported operations raise clear adapter errors rather than pretending the framework supports them.

## Agency Graph Context Boundary

Agency Graph context is a native-runtime capability. The graph context tools are app-owned tool contracts, so any adapter
can expose them as ordinary read-only tools when the tool is assigned to an agent. Automatic runtime retrieval, loop
guards, graph working sets, and graph-context prompt injection live in `app/runtime/native/` and are not implemented by
the CrewAI compatibility adapter.
