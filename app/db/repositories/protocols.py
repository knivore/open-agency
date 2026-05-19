from app.db.models import A2AAgentORM, MCPServerORM, RuntimeAdapterORM
from app.domain import MCPServerDefinition, RuntimeAdapterDefinition

from .catalog import InMemoryCatalogRepository, MongoCatalogRepository
from .sql import SQLAlchemyRepository


class RuntimeAdapterRepository(SQLAlchemyRepository[RuntimeAdapterORM]):
    def __init__(self, session):
        super().__init__(session, RuntimeAdapterORM)


class MCPServerRepository(SQLAlchemyRepository[MCPServerORM]):
    def __init__(self, session):
        super().__init__(session, MCPServerORM)


class A2AAgentRepository(SQLAlchemyRepository[A2AAgentORM]):
    def __init__(self, session):
        super().__init__(session, A2AAgentORM)


class MongoRuntimeAdapterRepository(MongoCatalogRepository[RuntimeAdapterDefinition]):
    def __init__(self, db=None):
        super().__init__(RuntimeAdapterDefinition, "runtime_adapter_definitions", db)


class InMemoryRuntimeAdapterRepository(InMemoryCatalogRepository[RuntimeAdapterDefinition]):
    def __init__(self):
        super().__init__(RuntimeAdapterDefinition)


class MongoMCPServerRepository(MongoCatalogRepository[MCPServerDefinition]):
    def __init__(self, db=None):
        super().__init__(MCPServerDefinition, "mcp_server_definitions", db)


class InMemoryMCPServerRepository(InMemoryCatalogRepository[MCPServerDefinition]):
    def __init__(self):
        super().__init__(MCPServerDefinition)
