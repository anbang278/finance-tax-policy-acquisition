from pydantic import Field

from ftr.knowledge.models import StrictModel


class Topic(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    name: str = Field(min_length=1)
    questions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    template_version: str = "1"


class Domain(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    name: str = Field(min_length=1)
    scope: str = ""
    topics: list[str]


class DomainImport(StrictModel):
    domains: list[Domain]
    topics: list[Topic]
