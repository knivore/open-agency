from pydantic import BaseModel, Field
from pydantic.functional_validators import BeforeValidator
from typing import List, Optional
from typing_extensions import Annotated

PyObjectId = Annotated[str, BeforeValidator(str)]


class CreateMessage(BaseModel):
    id: int
    sender: str
    message: str


class CreateConversation(BaseModel):
    id: Optional[PyObjectId] = Field(alias="_id", default=None)
    conv_id: int = Field(default=None)
    name: str = Field(default=None)
    messages: List[CreateMessage] = Field(default=None)
    owned_by: str = Field(default=None)
