"""Shapes shared by every provider."""

from pydantic import BaseModel, Field


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class Completion(BaseModel):
    text: str
    model: str
    provider: str
    usage: Usage = Field(default_factory=Usage)


class PlanStep(BaseModel):
    agent: str
    action: str
    depends_on: list[int] = Field(default_factory=list)


class Plan(BaseModel):
    """What the supervisor decided. The backend validates this before acting."""

    intent: str
    requires_execution: bool
    agents: list[str]
    risk: str
    requires_approval: bool
    steps: list[PlanStep]
    direct_answer: str | None = None
