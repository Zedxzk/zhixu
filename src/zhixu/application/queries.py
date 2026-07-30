"""Typed read-only queries and deterministic query bus."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from zhixu.domain.errors import ConflictError, ValidationError
from zhixu.domain.policy import CommandContext


@dataclass(frozen=True, slots=True)
class AgendaBetween:
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True, slots=True)
class ListTasks:
    include_archived: bool = False


@dataclass(frozen=True, slots=True)
class SearchNotes:
    text: str
    limit: int = 20


Query = AgendaBetween | ListTasks | SearchNotes
QueryT = TypeVar("QueryT", bound=Query)
Handler = Callable[[Any, CommandContext], Any]


class QueryBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Any], Handler] = {}

    def register(self, query_type: type[QueryT], handler: Handler) -> None:
        if query_type in self._handlers:
            raise ConflictError(f"handler already registered for {query_type.__name__}")
        self._handlers[query_type] = handler

    def execute(self, query: QueryT, context: CommandContext) -> Any:
        handler = self._handlers.get(type(query))
        if handler is None:
            raise ValidationError(f"unregistered query: {type(query).__name__}")
        return handler(query, context)
