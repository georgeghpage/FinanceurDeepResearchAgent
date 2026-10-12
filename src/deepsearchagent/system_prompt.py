"""Shared research instructions composed with each stage's JSON contract."""

import json
from importlib.resources import files

from pydantic import BaseModel

SYSTEM_PROMPT = files("deepsearchagent").joinpath("prompts", "system.md").read_text(encoding="utf-8").strip()


def build_system_prompt(instructions: str, schema: type[BaseModel]) -> str:
    return (
        f"{SYSTEM_PROMPT}\n\nCurrent stage instructions:\n{instructions}\n\n"
        "Return only a JSON object matching this JSON Schema:\n"
        + json.dumps(schema.model_json_schema())
    )
