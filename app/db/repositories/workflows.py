from app.db.models import WorkflowORM, WorkflowVersionORM
from app.domain import WorkflowDefinition

from .catalog import InMemoryWorkflowCatalogRepository, WorkflowCatalogRepository
from .sql import SQLAlchemyRepository


class WorkflowRepository(SQLAlchemyRepository[WorkflowORM]):
    def __init__(self, session):
        super().__init__(session, WorkflowORM)


class WorkflowVersionRepository(SQLAlchemyRepository[WorkflowVersionORM]):
    def __init__(self, session):
        super().__init__(session, WorkflowVersionORM)


__all__ = [
    "InMemoryWorkflowCatalogRepository",
    "WorkflowCatalogRepository",
    "WorkflowDefinition",
    "WorkflowRepository",
    "WorkflowVersionRepository",
]
