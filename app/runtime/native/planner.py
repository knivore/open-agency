from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List

from app.domain import WorkflowDefinition, WorkflowNodeDefinition


class LinearWorkflowPlanner:
    def order_nodes(self, workflow: WorkflowDefinition) -> List[WorkflowNodeDefinition]:
        if not workflow.nodes:
            return []

        node_by_id = {node.id: node for node in workflow.nodes}
        indegree: Dict[str, int] = {node.id: 0 for node in workflow.nodes}
        outgoing: Dict[str, List[str]] = defaultdict(list)

        for edge in workflow.edges:
            outgoing[edge.source_node_id].append(edge.target_node_id)
            indegree[edge.target_node_id] = indegree.get(edge.target_node_id, 0) + 1

        queue = deque()
        if workflow.entrypoint in node_by_id:
            queue.append(workflow.entrypoint)

        for node in workflow.nodes:
            if indegree[node.id] == 0 and node.id != workflow.entrypoint:
                queue.append(node.id)

        ordered: List[WorkflowNodeDefinition] = []
        seen = set()
        while queue:
            node_id = queue.popleft()
            if node_id in seen or node_id not in node_by_id:
                continue
            seen.add(node_id)
            ordered.append(node_by_id[node_id])
            for target_id in outgoing.get(node_id, []):
                indegree[target_id] -= 1
                if indegree[target_id] <= 0:
                    queue.append(target_id)

        for node in workflow.nodes:
            if node.id not in seen:
                ordered.append(node)

        return ordered
