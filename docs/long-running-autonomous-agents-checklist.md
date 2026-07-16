# Long-Running Autonomous Agents Checklist

This checklist tracks the work needed for Agency to support long-running autonomous agents, goal-driven workflows, and a main-agent supervisor that can replace much of the human monitoring loop while preserving auditability, policy, and approval boundaries.

## Operating Model

Goals are durable objectives above individual workflow executions. Chat should select or steer goals, workflows should
advance goals, and the main-agent monitor should supervise goals by deciding when to inspect, repair, replan, request
approval, or escalate.

```text
Goal = durable objective
Workflow = repeatable execution plan or attempt
Execution = one run
Chat = operator interface
Main agent = supervisor
```

An `@goal` chat mention is different from a persona mention: `@persona` changes the assistant persona or behavioral
lens, while `@goal` selects, creates, inspects, or steers a durable objective. A workflow can run under a goal by
carrying `goal_id` into execution creation, trigger payload, or runtime input; that makes the execution an attempt under
the goal, not the workflow definition itself. Workflow definitions may provide goal defaults in metadata, but the
selected `goal_id` should remain run-specific so one workflow can advance different goals over time.

## Long-Running Workflow Runtime

A long-running agent and a long-running workflow are related but not interchangeable. The agent is the actor that
reasons, uses tools, and retains goal context. The workflow is the durable execution state machine that determines when
work runs, waits without consuming a worker, wakes, repeats, pauses, and terminates.

Agency should support three workflow forms:

- **Waitable workflow:** runs until it needs input, approval, an event, or a wake time, then releases compute and resumes
  from a persisted checkpoint.
- **Persistent monitor workflow:** runs bounded cycles under one execution until an operator cancels it. Each cycle must
  checkpoint progress and obey retry, budget, backoff, and no-progress limits.
- **Goal-driven recurring workflow:** performs finite execution attempts under a durable goal, with the scheduler or
  supervisor deciding when another attempt is needed. This is the preferred mode when one continuous execution is not
  required.

`always_on` by itself relaxes the default finite worker timeout; it does not make a completed workflow graph start
another cycle. Adding `execution_lifecycle.persistent_cycle.enabled=true` opts a native workflow into durable cycles
under one execution id. Each cycle releases compute through a persisted sleep wait, and isolated workers exit cleanly
until the wake reconciler starts the next worker.

The current durable-wait implementation can suspend input, event, and sleep waits at a persisted native node boundary,
discard process-local engine state, and resume the same execution from its recorded outputs. Approval-gated tool calls
also persist their agent transcript and remaining tool-call position, release their worker or container, and resume from
the approval checkpoint after a persisted decision without replaying earlier calls in the same model response.

### Lifecycle Foundation

- [x] Store execution status as an extensible string so new states do not require a database enum migration.
- [x] Represent `waiting_for_input`, `waiting_for_approval`, `waiting_for_event`, `sleeping`, and operator `paused` as
  distinct execution states.
- [x] Keep durable wait states active and exempt them from idle and total-run timeout findings.
- [x] Preserve `always_on` as an explicit run mode rather than inferring it from a long timeout.
- [x] Persist a structured wait record with kind, checkpoint, correlation key, wake deadline, policy, and resolution.
- [x] Distinguish worker-owned waits from suspended durable waits so a dead approval worker is still detected as stale.
- [x] Emit explicit `execution.waiting` and `execution.woken` audit events for durable wait-service and subagent wait
  transitions.
- [x] Emit the same explicit wait and wake audit events for the tool approval path.

### Suspend And Wake

- [x] Resume a suspended native execution at a persisted safe node boundary under the same execution id.
- [x] Release the worker/container for every wait type, including a tool call paused mid-execution for approval.
- [x] Add an input-response API that resolves one pending input wait idempotently and resumes from its checkpoint.
- [x] Resolve approval waits from persisted approval decisions without depending on an in-process future.
- [x] Add event subscriptions with correlation keys, deadlines, and single-consumer idempotency.
- [ ] Add optional structured filters to event subscriptions.
- [x] Add reconciler-backed sleep wakeups that survive backend restart.
- [x] Reconcile due waits on startup and on the periodic runtime reconciliation cadence.
- [ ] Detect and repair orphaned waits whose execution or continuation cannot be recovered.
- [x] Expire timed-out waits without resuming compute while preserving persisted outputs and metadata.

### Persistent Cycles

- [x] Add a persistent cycle policy with interval, backoff, jitter, maximum consecutive failures, and optional deadline.
- [x] Start the next cycle only after the prior cycle checkpoint and audit events are durable.
- [x] Keep cycle number, last progress signature, next wake time, and bounded recent outcomes in execution metadata.
- [ ] Add cumulative token, cost, and runtime budget accounting across persistent cycles.
- [ ] Stop persistent execution only on cancellation, terminal policy, deadline, exhausted budget, or unrecoverable failure.
- [x] Add no-progress and repeated-output loop guards that pause and emit an escalation event instead of spinning forever.
- [x] Ensure cancellation cooperatively interrupts active compute and removes scheduled wakeups or event subscriptions.
- [ ] Let the goal supervisor choose between continuing a persistent execution and starting a fresh finite attempt.

### Operator Experience

- [ ] Show the exact wait reason, requested input or approval, checkpoint, and expected wake condition.
- [ ] Show current cycle, next wake time, cumulative runtime/budget, and recent cycle outcomes.
- [ ] Let operators provide input, approve/reject, wake now, pause, resume, and cancel from execution and goal views.
- [ ] Require confirmation for cancelling high-priority persistent workflows or discarding unresolved waits.

### Runtime Acceptance Criteria

- [x] A waiting workflow consumes no dedicated worker and resumes after backend restart.
- [x] Duplicate input, approval, event, or timer resolutions cannot claim the same wait twice.
- [x] A persistent monitor completes multiple bounded cycles under one execution id until manually cancelled or a terminal policy is reached.
- [x] A dead active worker is stale, while a suspended durable wait is not.
- [x] Every wait, wake, cycle, retry, guard escalation, and termination decision is auditable.
- [x] Finite workflows retain their current behavior when no long-running policy is configured.

## Goal

- [x] Support durable goals that can outlive one workflow execution.
- [x] Allow workflows and agents to run, pause, resume, replan, and recover over long periods.
- [x] Let the main agent supervise active goals, executions, subagents, failures, stale runs, and completion evidence.
- [x] Give the main agent bounded autonomy to steer or repair work without requiring constant human monitoring.
- [x] Preserve human approval for high-risk mutations, external side effects, destructive actions, and physical-world actions.

## Target Architecture

- [x] Introduce `Goal` as a first-class runtime concept above workflow executions.
- [x] Treat workflows as execution attempts under goals, not as the top-level autonomous unit.
- [x] Link goals to plans, executions, subagents, artifacts, memory records, evaluations, approvals, and supervisor decisions.
- [x] Extend the existing main-agent workflow monitor into a goal supervisor.
- [x] Keep the execution event stream as the source of truth for runtime activity.
- [x] Keep durable memory and graph context as supporting context, not as the source of truth.
- [x] Ensure all autonomous decisions are inspectable through events, artifacts, and audit records.

## Goal Domain Model

- [x] Add a durable goal model with objective, status, priority, owner/actor string, created time, updated time, and completion time.
- [x] Add goal success criteria as structured data, not only prose.
- [x] Add goal constraints such as allowed tools, blocked tools, risk level, budget, deadline, and approval policy.
- [x] Add goal lifecycle statuses: `created`, `planning`, `active`, `waiting_for_input`, `waiting_for_approval`, `paused`, `completed`, `failed`, `cancelled`, `abandoned`.
- [x] Add parent/child goal relationships for decomposed goals.
- [x] Add links from goals to workflow executions.
- [x] Add links from goals to artifacts and evidence.
- [x] Add links from goals to evaluation results.
- [x] Add links from goals to supervisor decisions and approval requests.
- [x] Add APIs to create, list, inspect, update, pause, resume, cancel, and complete goals.

## Goal Planning

- [x] Add a goal-planning service that turns a goal into an initial plan.
- [x] Store the active plan separately from the original goal.
- [x] Version plans when the main agent replans.
- [x] Record why each replan happened.
- [x] Support plan steps that start workflows, inspect executions, retrieve memory, ask for input, request approval, or evaluate evidence.
- [x] Record the selected workflow, input payload, assigned agents, and expected evidence for each executable step.
- [x] Require every active goal to have at least one success criterion or explicit human-defined completion condition.

## Long-Running Execution Support

- [x] Confirm runtime timeout policy supports long-running workflows without accidental worker termination.
- [x] Ensure heartbeats are updated for active local and isolated workers.
- [x] Ensure stale execution detection distinguishes crashed, idle, paused, waiting-for-approval, and intentionally long-running executions.
- [x] Add resumable checkpoints for long-running agent steps where practical.
- [x] Persist subagent progress events with enough state for supervisor recovery.
- [x] Support safe restart or replacement of stale executions.
- [x] Preserve artifacts and partial outputs when runs are cancelled or replaced.
- [x] Ensure cancellation is cooperative where possible and forceful only when required.
- [x] Ensure worker/container metadata remains linked to the goal and execution.

## Main-Agent Supervisor

- [x] Extend the main-agent monitor to scan active goals, not only active executions.
- [x] Add a supervisor loop that periodically evaluates goal progress.
- [x] Detect stalled goals with no active execution and no planned next action.
- [x] Detect active executions that no longer match the current goal plan.
- [x] Detect repeated failures across executions for the same goal.
- [x] Detect missing evidence for supposedly completed work.
- [x] Detect subagents needing input, approval, or redirection.
- [x] Detect token budget, context health, and tool failure signals relevant to goal progress.
- [x] Record supervisor findings as durable events.
- [x] Record supervisor decisions as durable events.
- [x] Record supervisor actions as durable events.
- [x] Create human approval requests when policy requires escalation.

## Supervisor Autonomy Policy

- [x] Define which actions the main agent may perform automatically.
- [x] Allow automatic read-only inspection of goals, workflows, executions, events, artifacts, memory, and graph context.
- [x] Allow automatic summarization of current goal state.
- [x] Allow automatic stale-run repair when policy marks the workflow as monitorable.
- [x] Allow automatic low-risk replanning that does not mutate workflow definitions or external systems.
- [x] Allow automatic spawning of follow-up read-only investigation workflows.
- [x] Require approval before mutating workflow definitions.
- [x] Require approval before mutating tool definitions.
- [x] Require approval before running shell commands with side effects.
- [x] Require approval before external writes, messages, purchases, deletes, or physical-world actions.
- [x] Require approval before cancelling high-priority or user-created goals.
- [x] Make autonomy level configurable per workflow and per goal.
- [x] Support at least `off`, `advisory`, `guarded`, and `high_autonomy` supervision modes.

## Completion And Evaluation

- [x] Add a goal evaluator that checks success criteria against evidence.
- [x] Require evidence before marking a goal completed.
- [x] Store completion rationale.
- [x] Store evaluator confidence.
- [x] Store missing evidence when completion fails.
- [x] Allow the main agent to request more work when evidence is insufficient.
- [x] Avoid letting the same worker agent be the only authority for declaring success on high-risk goals.
- [x] Support human final approval for selected goal types.

## Memory And Context

- [x] Store goal summaries as durable memory when useful.
- [x] Attach relevant memory records to goals and workflow executions.
- [x] Use Agency Graph context for lineage, prior attempts, failures, decisions, and next actions.
- [x] Add goal-aware graph relationships for goal, plan, execution, artifact, approval, and evaluation nodes.
- [x] Prevent unbounded context growth by summarizing long-running goal history.
- [x] Preserve important constraints, approvals, and unresolved blockers during compaction.

## APIs And Tools

- [x] Add system tools for goal list, get, create, update, pause, resume, cancel, and complete.
- [x] Add system tools for goal planning and replanning.
- [x] Add system tools for goal evidence attachment.
- [x] Add system tools for goal evaluation.
- [x] Add system tools for supervisor decisions and goal findings.
- [x] Expose goal state through backend APIs.
- [x] Ensure main-agent tools can operate on goal IDs directly.
- [x] Ensure workflow-run tools accept optional goal context.
- [x] Ensure schedule triggers can create or continue goals when configured.
- [x] Add first-class local-first `agency.voice.generate` for reusable voice artifacts. See [Voice And Media Tools](tools.md#voice-and-media-tools).
- [x] Keep `agency.media.publish` and `agency.media.send` provider-agnostic so media can be generated, stored, and delivered through independent workflow steps.
- [ ] Confirm workflow-native tool bridging records real `tool_invocations` for generated voice and media delivery tasks, not only tool-shaped agent output.

## UI And Operator Experience

- [x] Add a goals view showing status, objective, priority, deadline, current plan, active executions, and next supervisor action.
- [x] Add goal detail with timeline, evidence, artifacts, approvals, memory, and evaluation results.
- [x] Show which actions were taken automatically by the main agent.
- [x] Show which actions are pending human approval.
- [x] Show why a goal is blocked.
- [x] Show stale or failing goals prominently.
- [x] Allow operators to pause, resume, cancel, reassign, or approve supervisor actions.
- [x] Allow operators to adjust autonomy level per goal.
- [x] Allow operators to add or modify success criteria before completion.

## Safety And Governance

- [x] Add policy checks before every supervisor action.
- [x] Add audit events for all autonomous supervisor decisions.
- [x] Add risk labels to supervisor actions.
- [x] Add approval boundaries for destructive or external side effects.
- [x] Add budget controls for long-running goals.
- [x] Add maximum retry and maximum replan limits.
- [x] Add loop guards to prevent repeated unproductive replanning.
- [x] Add escalation when the same goal repeatedly fails.
- [x] Add retention rules for goal findings and supervisor events.
- [x] Ensure secrets and sensitive data are redacted in supervisor prompts, events, and artifacts.

## Scheduling And Continuous Operation

- [x] Allow schedules to start new goal runs.
- [x] Allow schedules to continue existing goals.
- [x] Allow event triggers to create goals.
- [x] Allow event triggers to wake the supervisor for a specific goal.
- [x] Add a background supervisor cadence for active goals.
- [x] Ensure the supervisor can resume after backend restart.
- [x] Ensure active goals are reconciled with active executions on startup.
- [x] Ensure orphaned executions can be linked back to goals or flagged for review.

## Testing

- [x] Unit test durable wait creation, one-pending-wait enforcement, policy persistence, and atomic resolution.
- [x] Unit test input, event, sleep, deadline-expiration, and repeated approval wait behavior.
- [x] Integration test native checkpoint resume under the same execution id after process-local state loss.
- [x] Regression test generic start/resume commands cannot bypass a pending wait.
- [x] Integration test workerless approval continuation after the original worker exits.
- [x] Integration test persistent monitor cycles, loop guards, suspended isolated workers, and cancellation of scheduled wakeups.
- [x] Unit test goal lifecycle transitions.
- [x] Unit test goal-to-execution linking.
- [x] Unit test supervisor finding creation.
- [x] Unit test autonomy policy decisions.
- [x] Unit test approval-required supervisor actions.
- [x] Unit test automatic stale-run repair.
- [x] Unit test repeated failure escalation.
- [x] Unit test goal evaluation with sufficient evidence.
- [x] Unit test goal evaluation with missing evidence.
- [x] Integration test long-running workflow heartbeat and stale detection.
- [x] Integration test backend restart with active goals.
- [x] Integration test main-agent replanning after failed execution.
- [x] Integration test schedule-created goal execution.
- [x] Integration test graph context retrieval for goal supervision.
- [x] Regression test that high-risk tool, workflow, shell, external, and physical-world mutations still require approval.

## Acceptance Criteria

- [x] A goal can be created and tracked independently from any single workflow execution.
- [x] A goal can launch one or more workflow executions.
- [x] Active goals survive backend restart.
- [x] The main agent can inspect active goals and identify blocked, stale, failed, or incomplete work.
- [x] The main agent can automatically repair low-risk stale executions under guarded autonomy.
- [x] The main agent can propose or perform replans according to policy.
- [x] The main agent cannot perform high-risk mutations without approval.
- [x] Goal completion requires recorded evidence and evaluation.
- [x] Operators can see what the main agent did, why it did it, and what remains blocked.
- [ ] Waitable and persistent long-running workflows meet all runtime acceptance criteria above.
