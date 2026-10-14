from pydantic import BaseModel


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenRequest(BaseModel):
    username: str
    password: str


class AdminOut(BaseModel):
    username: str
    is_sudo: bool = True

    model_config = {"from_attributes": True}
