from app.db.models import CredentialORM, ModelProfileORM, ModelProviderORM
from app.domain import ModelProfileDefinition, ModelProviderDefinition

from .catalog import (
    InMemoryCatalogRepository,
    InMemoryModelProfileCatalogRepository,
    ModelProfileCatalogRepository,
    MongoCatalogRepository,
)
from .sql import SQLAlchemyRepository


class ModelProviderRepository(SQLAlchemyRepository[ModelProviderORM]):
    def __init__(self, session):
        super().__init__(session, ModelProviderORM)


class ModelProfileRepository(SQLAlchemyRepository[ModelProfileORM]):
    def __init__(self, session):
        super().__init__(session, ModelProfileORM)


class CredentialRepository(SQLAlchemyRepository[CredentialORM]):
    def __init__(self, session):
        super().__init__(session, CredentialORM)


class MongoModelProviderRepository(MongoCatalogRepository[ModelProviderDefinition]):
    def __init__(self, db=None):
        super().__init__(ModelProviderDefinition, "model_provider_definitions", db)


class InMemoryModelProviderRepository(InMemoryCatalogRepository[ModelProviderDefinition]):
    def __init__(self):
        super().__init__(ModelProviderDefinition)


__all__ = [
    "CredentialRepository",
    "InMemoryCatalogRepository",
    "InMemoryModelProfileCatalogRepository",
    "InMemoryModelProviderRepository",
    "ModelProfileCatalogRepository",
    "ModelProfileDefinition",
    "ModelProfileRepository",
    "ModelProviderDefinition",
    "ModelProviderRepository",
    "MongoModelProviderRepository",
]
