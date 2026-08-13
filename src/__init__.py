"""Groom — an agent that curates ML training datasets.

Exposes `root_agent` so that `adk run ./src` and `adk deploy cloud_run ./src`
can discover the agent.
"""

from src.agent import root_agent

__all__ = ["root_agent"]
