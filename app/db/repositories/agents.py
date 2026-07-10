"""Repository adapters for agent definitions across SQL, Mongo, and memory stores."""

from app.db.models import AgentORM
from app.domain import AgentDefinition

from .catalog import InMemoryCatalogRepository, MongoCatalogRepository
from .sql import SQLAlchemyRepository


class AgentRepository(SQLAlchemyRepository[AgentORM]):
    def __init__(self, session):
        super().__init__(session, AgentORM)


class MongoAgentRepository(MongoCatalogRepository[AgentDefinition]):
    def __init__(self, db=None):
        super().__init__(AgentDefinition, "agent_definitions", db)


class InMemoryAgentRepository(InMemoryCatalogRepository[AgentDefinition]):
    def __init__(self):
        super().__init__(AgentDefinition)
