from app.db.models import ToolORM
from app.domain import ToolDefinition

from .catalog import InMemoryCatalogRepository, MongoCatalogRepository
from .sql import SQLAlchemyRepository


class ToolRepository(SQLAlchemyRepository[ToolORM]):
    def __init__(self, session):
        super().__init__(session, ToolORM)


class MongoToolRepository(MongoCatalogRepository[ToolDefinition]):
    def __init__(self, db=None):
        super().__init__(ToolDefinition, "tool_definitions", db)


class InMemoryToolRepository(InMemoryCatalogRepository[ToolDefinition]):
    def __init__(self):
        super().__init__(ToolDefinition)
