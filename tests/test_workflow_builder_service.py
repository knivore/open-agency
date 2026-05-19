from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.domain import WorkflowDefinition
from app.services.workflow_builder import WorkflowBuilderService


class WorkflowBuilderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.service = WorkflowBuilderService(self.context)

    async def test_build_workflow_definition_adds_recommendation_to_code_pipeline(self) -> None:
        task_drafts = {
            "assistant_message": "Draft ready.",
            "tasks": [
                {
                    "name": "Review recommendations",
                    "description": "Analyze repo findings and rank recommendation candidates.",
                    "expected_output": "Top recommendations with rationale.",
                }
            ],
        }
        agent_drafts = {
            "agents": [
                {
                    "name": "Recommendation Agent",
                    "role": "Repository analyzer",
                    "instructions": "Review agency and agency-fe and recommend improvements.",
                    "backstory": "Specialized in practical repository analysis.",
                }
            ]
        }
        workflow_draft = {
            "workflow": {
                "name": "Repo Improvement Workflow",
                "description": "Reviews recommendations and moves to implementation.",
            }
        }

        with patch(
            "app.services.workflow_builder.WorkflowBuilderService.generate_draft",
            new=AsyncMock(side_effect=[task_drafts, agent_drafts, workflow_draft]),
        ):
            workflow = await self.service.build_workflow_definition(
                goal=(
                    "Create a workflow that finds recommendations for agency and agency-fe, then implement coding "
                    "improvements and verify the patch."
                )
            )

        self.assertEqual(workflow.metadata.get("workflow_builder_enhancement"), "recommendation_to_code_pipeline")
        self.assertIn("agency.command.run", [tool.id for tool in workflow.tool_definitions])
        task_names = [task.name.lower() for task in workflow.task_definitions]
        self.assertTrue(any("implement" in name for name in task_names))
        self.assertTrue(any("verify" in name for name in task_names))

    async def test_recommendation_pipeline_detection_uses_workflow_context(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Repo Ideas Workflow",
            description="Find ideas for repository improvement.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Gather recommendations from agency and agency-fe scans.",
                    "instructions": "Prioritize recommendations by impact and effort.",
                    "agent_id": "agent-1",
                }
            ],
            agent_definitions=[
                {
                    "id": "agent-1",
                    "name": "Recommendation Agent",
                    "instructions": "Inspect both repositories and produce concrete recommendations.",
                }
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="Enhance this workflow to apply coding improvements from the output.",
        )

        self.assertEqual(enhanced.metadata.get("workflow_builder_enhancement"), "recommendation_to_code_pipeline")
        self.assertIn("agency.command.run", [tool.id for tool in enhanced.tool_definitions])
        task_names = [task.name.lower() for task in enhanced.task_definitions]
        self.assertTrue(any("implement" in name for name in task_names))
        self.assertTrue(any("verify" in name for name in task_names))

    async def test_recommendation_pipeline_uses_existing_coder_agent_for_todo_work(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Repo Ideas Workflow",
            description="Find ideas for repository improvement.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Gather recommendations from agency and agency-fe scans.",
                    "agent_id": "agent-1",
                }
            ],
            agent_definitions=[
                {
                    "id": "agent-1",
                    "name": "Recommendation Agent",
                    "instructions": "Inspect both repositories and produce concrete recommendations.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="Enhance this workflow so the coder agent performs the todo from recommendations.",
        )

        self.assertEqual(enhanced.metadata.get("workflow_builder_enhancement"), "recommendation_to_code_pipeline")
        implementation_tasks = [task for task in enhanced.task_definitions if "implement" in task.name.lower()]
        self.assertTrue(implementation_tasks)
        self.assertEqual(implementation_tasks[0].agent_id, "coder")
        coder = next(agent for agent in enhanced.agent_definitions if agent.id == "coder")
        self.assertIn("agency.command.run", coder.tool_ids)

    async def test_recommendation_pipeline_prefers_coder_over_generic_implementer(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Repo Ideas Workflow",
            description="Find ideas for repository improvement.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Gather recommendations from agency and agency-fe scans.",
                    "agent_id": "agent-1",
                }
            ],
            agent_definitions=[
                {
                    "id": "agent-1",
                    "name": "Recommendation Agent",
                    "instructions": "Inspect repos and suggest how to implement improvements.",
                },
                {
                    "id": "agent-2",
                    "name": "Coder Agent",
                    "role": "Handles coding patches and TODO execution.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="Enhance this workflow so recommendations are converted to todo items and implemented.",
        )

        implementation_tasks = [task for task in enhanced.task_definitions if "implement" in task.name.lower()]
        self.assertTrue(implementation_tasks)
        self.assertEqual(implementation_tasks[0].agent_id, "agent-2")

    async def test_recommendation_pipeline_reassigns_existing_implementation_tasks_to_coder(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Repo Ideas Workflow",
            description="Find ideas for repository improvement.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                },
                {
                    "id": "node-2",
                    "name": "Implement selected TODO improvements",
                    "node_type": "task",
                    "task_id": "task-2",
                },
            ],
            edges=[{"source_node_id": "node-1", "target_node_id": "node-2"}],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Gather recommendations from agency and agency-fe scans.",
                    "agent_id": "analyst",
                },
                {
                    "id": "task-2",
                    "name": "Implement selected TODO improvements",
                    "description": "Apply the recommended code changes.",
                    "agent_id": "analyst",
                    "tool_ids": [],
                    "depends_on_task_ids": ["task-1"],
                },
            ],
            agent_definitions=[
                {
                    "id": "analyst",
                    "name": "Recommendation Agent",
                    "instructions": "Inspect repos and suggest improvements.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="Enhance this workflow so the coder agent performs the todo from recommendations.",
        )

        implementation_task = next(task for task in enhanced.task_definitions if task.id == "task-2")
        self.assertEqual(implementation_task.agent_id, "coder")
        self.assertIn("agency.command.run", implementation_task.tool_ids)

    async def test_recommendation_pipeline_handles_direct8000_goal_and_assigns_coder(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Agency Repo Improvement Review",
            description="Looks for ideas and improvements in agency and agency-fe.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Collect improvement ideas from the repo scan.",
                    "agent_id": "agent-1",
                }
            ],
            agent_definitions=[
                {
                    "id": "agent-1",
                    "name": "Recommendation Agent",
                    "instructions": "Review the repository and propose improvements.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="i want to have the coder agent to work on the workflow to perform the todo direct8000",
        )

        implementation_tasks = [task for task in enhanced.task_definitions if "implement" in task.name.lower()]
        self.assertTrue(implementation_tasks)
        self.assertEqual(implementation_tasks[0].agent_id, "coder")
        coder = next(agent for agent in enhanced.agent_definitions if agent.id == "coder")
        self.assertIn("agency.command.run", coder.tool_ids)

    async def test_recommendation_pipeline_handles_llmfirst_goal_and_assigns_coder(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Agency Repo Improvement Review",
            description="Looks for ideas and improvements in agency and agency-fe.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Collect improvement ideas from the repo scan.",
                    "agent_id": "agent-1",
                }
            ],
            agent_definitions=[
                {
                    "id": "agent-1",
                    "name": "Recommendation Agent",
                    "instructions": "Review the repository and propose improvements.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="i want to have the coder agent to work on the workflow to perform the todo llmfirst",
        )

        implementation_tasks = [task for task in enhanced.task_definitions if "implement" in task.name.lower()]
        self.assertTrue(implementation_tasks)
        self.assertEqual(implementation_tasks[0].agent_id, "coder")
        coder = next(agent for agent in enhanced.agent_definitions if agent.id == "coder")
        self.assertIn("agency.command.run", coder.tool_ids)

    async def test_recommendation_pipeline_reassigns_todo_task_from_instructions(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Repo Ideas Workflow",
            description="Find ideas for repository improvement.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                },
                {
                    "id": "node-2",
                    "name": "Apply recommendations",
                    "node_type": "task",
                    "task_id": "task-2",
                },
            ],
            edges=[{"source_node_id": "node-1", "target_node_id": "node-2"}],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Gather recommendations from agency and agency-fe scans.",
                    "agent_id": "analyst",
                },
                {
                    "id": "task-2",
                    "name": "Apply recommendations",
                    "description": "Turn selected suggestions into concrete changes.",
                    "instructions": "Implement todo items for selected recommendations in agency-fe.",
                    "expected_output": "Completed TODO checklist and code patch summary.",
                    "agent_id": "analyst",
                    "tool_ids": [],
                    "depends_on_task_ids": ["task-1"],
                },
            ],
            agent_definitions=[
                {
                    "id": "analyst",
                    "name": "Recommendation Agent",
                    "instructions": "Inspect repos and suggest improvements.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="Enhance this workflow so the coder agent performs the todo from recommendations.",
        )

        todo_task = next(task for task in enhanced.task_definitions if task.id == "task-2")
        self.assertEqual(todo_task.agent_id, "coder")
        self.assertIn("agency.command.run", todo_task.tool_ids)

    async def test_recommendation_pipeline_handles_exact_coder_todo_goal_and_assigns_coder(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Agency Repo Improvement Review",
            description="Looks for ideas and improvements in agency and agency-fe.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Collect improvement ideas from the repo scan.",
                    "agent_id": "agent-1",
                }
            ],
            agent_definitions=[
                {
                    "id": "agent-1",
                    "name": "Recommendation Agent",
                    "instructions": "Review the repository and propose improvements.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="i want to have the coder agent to work on the workflow to perform the todo",
        )

        implementation_tasks = [task for task in enhanced.task_definitions if "implement" in task.name.lower()]
        self.assertTrue(implementation_tasks)
        self.assertEqual(implementation_tasks[0].agent_id, "coder")
        coder = next(agent for agent in enhanced.agent_definitions if agent.id == "coder")
        self.assertIn("agency.command.run", coder.tool_ids)

    async def test_recommendation_pipeline_handles_exact_coder_todo_goal_without_repo_keywords(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-general",
            name="General Workflow",
            description="Process workflow outputs.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Initial analysis",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Initial analysis",
                    "description": "Summarize current findings.",
                    "agent_id": "agent-1",
                }
            ],
            agent_definitions=[
                {
                    "id": "agent-1",
                    "name": "Analyst Agent",
                    "instructions": "Summarize findings.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="i want to have the coder agent to work on the workflow to perform the todo",
        )

        implementation_tasks = [task for task in enhanced.task_definitions if "implement" in task.name.lower()]
        self.assertTrue(implementation_tasks)
        self.assertEqual(implementation_tasks[0].agent_id, "coder")
        coder = next(agent for agent in enhanced.agent_definitions if agent.id == "coder")
        self.assertIn("agency.command.run", coder.tool_ids)

    async def test_recommendation_pipeline_adds_fourth_coding_task_after_brief_task(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-daily-review",
            name="Daily Agency Repo Improvement Review",
            description="Daily workflow that proposes one new Agency repo improvement idea.",
            entrypoint="node-scan",
            nodes=[
                {"id": "node-scan", "name": "Inspect repository signals", "node_type": "task", "task_id": "task-scan"},
                {"id": "node-risk", "name": "Identify improvement and risk", "node_type": "task", "task_id": "task-risk"},
                {
                    "id": "node-report",
                    "name": "Prepare daily repo improvement brief",
                    "node_type": "task",
                    "task_id": "task-report",
                },
            ],
            edges=[
                {"source_node_id": "node-scan", "target_node_id": "node-risk"},
                {"source_node_id": "node-risk", "target_node_id": "node-report"},
            ],
            task_definitions=[
                {
                    "id": "task-scan",
                    "name": "Inspect repository signals",
                    "description": "Review recent repository structure, TODOs, tests, runtime scripts, and error-prone areas.",
                    "agent_id": "reviewer",
                },
                {
                    "id": "task-risk",
                    "name": "Identify improvement and risk",
                    "description": "Choose one high-leverage improvement idea and identify vulnerabilities or fixes worth addressing.",
                    "agent_id": "reviewer",
                    "depends_on_task_ids": ["task-scan"],
                },
                {
                    "id": "task-report",
                    "name": "Prepare daily repo improvement brief",
                    "description": "Produce the final daily brief for the human.",
                    "instructions": "Include a concrete TODO list suitable for direct coding.",
                    "expected_output": "A short daily brief with recommended next actions and a concrete TODO list for implementation.",
                    "agent_id": "reviewer",
                    "depends_on_task_ids": ["task-risk"],
                },
            ],
            agent_definitions=[
                {
                    "id": "reviewer",
                    "name": "Agency Repo Improvement Reviewer",
                    "instructions": "Review the repository and propose improvements.",
                    "tool_ids": ["agency.command.run"],
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal=(
                "Looking at the workflow Daily Agency Repo Improvement Review, include coder agent into the "
                "workflow and have it perform a 4th task which is a coding task that is taken from the output "
                "of the 3rd task and work on it."
            ),
        )

        report_task = next(task for task in enhanced.task_definitions if task.id == "task-report")
        self.assertEqual(report_task.agent_id, "reviewer")
        implementation_tasks = [
            task
            for task in enhanced.task_definitions
            if "implement" in task.name.lower() or "todo" in task.name.lower()
        ]
        self.assertTrue(implementation_tasks)
        self.assertEqual(implementation_tasks[0].agent_id, "coder")
        self.assertEqual(implementation_tasks[0].depends_on_task_ids, ["task-report"])
        self.assertEqual(len(enhanced.task_definitions), 4)
        self.assertFalse(any("verify" in task.name.lower() for task in enhanced.task_definitions))

    async def test_recommendation_pipeline_adds_coder_qa_collaboration_loop(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Agency Repo Improvement Review",
            description="Looks for ideas and improvements in agency and agency-fe.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Collect improvement ideas from the repo scan.",
                    "agent_id": "agent-1",
                }
            ],
            agent_definitions=[
                {
                    "id": "agent-1",
                    "name": "Recommendation Agent",
                    "instructions": "Review the repository and propose improvements.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal=(
                "i want to have a QA agent that can perform the QA of what the code change is done and also "
                "if there's errors, i want the coding agent to fix, and the 2 agents will have to work "
                "together to code and qa"
            ),
        )

        qa_agent = next((agent for agent in enhanced.agent_definitions if "qa" in (agent.name or "").lower()), None)
        self.assertIsNotNone(qa_agent)
        assert qa_agent is not None
        self.assertIn("agency.command.run", qa_agent.tool_ids)

        coder_agent = next(agent for agent in enhanced.agent_definitions if agent.id == "coder")
        self.assertIn("agency.command.run", coder_agent.tool_ids)
        self.assertIn(qa_agent.id, coder_agent.handoff_agent_ids)
        self.assertIn(coder_agent.id, qa_agent.handoff_agent_ids)

        verify_task = next(task for task in enhanced.task_definitions if "verify" in task.name.lower())
        self.assertEqual(verify_task.agent_id, qa_agent.id)
        self.assertIn("agency.command.run", verify_task.tool_ids)

        fix_task = next(task for task in enhanced.task_definitions if "fix qa findings" in task.name.lower())
        self.assertEqual(fix_task.agent_id, coder_agent.id)
        self.assertEqual(fix_task.depends_on_task_ids, [verify_task.id])

        recheck_task = next(task for task in enhanced.task_definitions if "qa recheck" in task.name.lower())
        self.assertEqual(recheck_task.agent_id, qa_agent.id)
        self.assertEqual(recheck_task.depends_on_task_ids, [fix_task.id])
        self.assertEqual(enhanced.metadata.get("workflow_builder_collaboration"), "coder_qa")

    async def test_recommendation_pipeline_creates_dedicated_qa_agent_when_reviewer_mentions_tests(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-repo-ideas",
            name="Agency Repo Improvement Review",
            description="Looks for ideas and improvements in agency and agency-fe.",
            entrypoint="node-1",
            nodes=[
                {
                    "id": "node-1",
                    "name": "Review recommendations",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            task_definitions=[
                {
                    "id": "task-1",
                    "name": "Review recommendations",
                    "description": "Collect improvement ideas from the repo scan.",
                    "agent_id": "reviewer",
                }
            ],
            agent_definitions=[
                {
                    "id": "reviewer",
                    "name": "Agency Repo Improvement Reviewer",
                    "instructions": "Prefer concrete file paths, testable claims, and small actionable next steps.",
                },
                {
                    "id": "coder",
                    "name": "Coder Agent",
                    "role": "Handles repository implementation tasks.",
                    "tool_ids": [],
                },
            ],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

        enhanced = self.service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal=(
                "i want to have a QA agent that can perform the QA of what the code change is done and also "
                "if there's errors, i want the coding agent to fix, and the 2 agents will have to work together"
            ),
        )

        qa_agent = next((agent for agent in enhanced.agent_definitions if agent.name == "QA Agent"), None)
        self.assertIsNotNone(qa_agent)
        assert qa_agent is not None
        verify_task = next(task for task in enhanced.task_definitions if "verify" in task.name.lower())
        recheck_task = next(task for task in enhanced.task_definitions if "qa recheck" in task.name.lower())
        self.assertEqual(verify_task.agent_id, qa_agent.id)
        self.assertEqual(recheck_task.agent_id, qa_agent.id)
