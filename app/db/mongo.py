from __future__ import annotations

import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.server_api import ServerApi

# Legacy Mongo compatibility for the still-supported Mongo repositories.
load_dotenv()


def get_mongodb_uri() -> str:
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI must be configured")
    return uri


def get_mongodb_db_name() -> str:
    db_name = os.getenv("MONGODB_DB_NAME")
    if not db_name:
        raise RuntimeError("MONGODB_DB_NAME must be configured")
    return db_name


async def ping_server():
    client = AsyncIOMotorClient(get_mongodb_uri(), server_api=ServerApi("1"))
    await client.admin.command("ping")
    return client


def mongo_db_connect() -> AsyncIOMotorClient:
    return AsyncIOMotorClient(get_mongodb_uri(), server_api=ServerApi("1"))
