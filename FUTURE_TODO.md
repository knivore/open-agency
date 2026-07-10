# Future TODO

## Task: Add Model Gateway + OpenRouter Adapter to Agency

### Objective

Implement a provider-agnostic Model Gateway for Agency that allows workflows and agents to call LLMs through a common
internal interface.

Agency should own model policy, routing intent, usage tracking, fallback visibility, and governance. OpenRouter should be
added as one external adapter to provide multi-model and multi-provider routing.

### Rationale

Agency should not hardcode model calls directly into agents or workflows. We need a central Model Gateway so that future
providers such as Ollama, vLLM, OpenAI, Anthropic, Bedrock, and OpenRouter can be swapped or routed based on task policy.

OpenRouter should be used for external model/provider fallback, price-based routing, latency/throughput sorting, and
quick access to many model families. However, Agency should still retain its own policy layer for privacy, cost budget,
local-vs-cloud decisions, audit trail, and long-running workflow observability.

### Required Analysis

1. Search the current Agency repo for any existing LLM, model, provider, adapter, agent execution, workflow execution, or
   tool-calling code.
2. Identify where model calls are currently made or should be made.
3. Identify the best location to introduce a Model Gateway module.
4. Identify existing database models or logging tables that can store model usage, token usage, cost, latency, and route
   metadata.
5. Identify config/env handling patterns for API keys and provider settings.

### Target Design

Create or propose the following internal concepts:

- `ModelGateway`
- `ModelRequest`
- `ModelResponse`
- `ModelPolicy`
- `ModelRoute`
- `ModelAttempt`
- `ModelUsage`
- `ProviderAdapter` interface
- `OpenRouterAdapter`

### ModelRequest Should Include

- `workflow_run_id`
- `agent_id`
- `step_id`
- `task_type`
- `messages`
- `tools`, if applicable
- `response_format`, if applicable
- `model_policy`
- `preferred_models`
- `privacy_level`
- `max_cost`
- `max_latency`
- `allow_fallbacks`
- `metadata`

### ModelResponse Should Include

- `content`
- `raw_response`
- `selected_model`
- `selected_provider`
- `adapter`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `estimated_cost`
- `latency_ms`
- `fallback_attempts`
- `finish_reason`
- `route_receipt`
- `error`, if any

### OpenRouter Adapter Requirements

Implement an OpenRouter adapter that supports:

- chat completions
- streaming if the current architecture supports streaming
- model list fallback
- provider order
- provider allow/ignore list
- provider sort by price, latency, or throughput
- `allow_fallbacks` flag
- `max_price` setting
- `data_collection` deny option for sensitive tasks
- ZDR option where applicable
- structured output pass-through if supported
- tool calling pass-through if supported

### Routing Policy Examples

Add initial policies:

- `cheap_background`
- `fast_interactive`
- `high_reasoning`
- `private_local`
- `enterprise_controlled`

The policy resolver should decide whether to route to OpenRouter, local model, direct provider, or Bedrock.

### Important Constraint

Do not make OpenRouter the only model interface. OpenRouter must be one adapter behind Agency's Model Gateway.

Agency must remain capable of supporting local models and direct provider APIs.

### Observability Requirements

Every model call should record a route receipt containing:

- `workflow_run_id`
- `agent_id`
- `step_id`
- `policy`
- `adapter`
- `requested_models`
- `selected_model`
- `selected_provider`
- `fallback_attempts`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `estimated_cost`
- `latency_ms`
- `status`
- `error details`, if any
- `timestamp`

### Deliverables

1. Repo analysis summary.
2. Proposed file/module structure.
3. Implementation checklist.
4. Minimal Model Gateway interface.
5. OpenRouter adapter implementation.
6. Example config file or env variables.
7. Unit tests for:
   - policy resolution
   - OpenRouter request construction
   - fallback settings
   - usage tracking
   - error handling
8. Documentation showing how an agent or workflow should call the Model Gateway.

### Acceptance Criteria

- Agents do not call OpenRouter directly.
- All model calls go through `ModelGateway`.
- OpenRouter can be enabled/disabled by config.
- Local/private policies can block cloud routing.
- Usage and route receipt are recorded for every call.
- Future adapters can be added without changing agent logic.
