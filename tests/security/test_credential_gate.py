from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from zhixu.adapters.storage.sqlite import (
    AgendaRepository,
    Database,
    NoteRepository,
    PendingPlanStore,
    ReminderRepository,
    SQLiteLLMUsage,
    TaskRepository,
    UserRepository,
)
from zhixu.application import (
    AssistantEngine,
    LLMGateway,
    ModelIntentClassifier,
    RuleIntentRouter,
    ZhixuServices,
)
from zhixu.application.queries import SearchNotes
from zhixu.domain import (
    Action,
    CommandContext,
    PolicyEngine,
    ResourceRef,
    User,
    UserStatus,
)
from zhixu.ports import FrozenClock, LLMBudgetLimit, LLMRequest, LLMResponse
from zhixu.security import LLMEgressPolicy

NOW = datetime(2026, 6, 1, 8, tzinfo=UTC)

# Synthetic throughout; the release gate scans for anything resembling a secret.
FINANCIAL_MESSAGE = "记录一下我的银行卡密码是 000000"


class ExhaustedLLM:
    """Any call is a failure, so the test proves the model was never reached."""

    provider_ref = "fake"
    is_local = True

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: LLMRequest, *, timeout_seconds: float) -> LLMResponse:
        del request, timeout_seconds
        self.calls += 1
        raise AssertionError("the model must not be called for a financial credential")


@pytest.fixture
def engine_parts(
    tmp_path: Path,
) -> tuple[AssistantEngine, ZhixuServices, CommandContext, ExhaustedLLM]:
    database = Database(tmp_path / "zhixu.sqlite3")
    database.migrate()
    clock = FrozenClock(NOW)
    policy = PolicyEngine()
    context = CommandContext(actor_user_id="user_test")
    UserRepository(database).create(
        User("user_test", "Synthetic User", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="user_test", now=NOW),
            Action.CREATE,
            ResourceRef("user", "user_test", "user_test"),
        ),
    )
    services = ZhixuServices(
        agenda=AgendaRepository(database),
        tasks=TaskRepository(database),
        notes=NoteRepository(database),
        reminders=ReminderRepository(database),
        policy=policy,
        clock=clock,
    )
    client = ExhaustedLLM()
    llm = LLMGateway(
        client=client,
        usage=SQLiteLLMUsage(database, clock),
        clock=clock,
        egress=LLMEgressPolicy(allow_confidential_to_local=True),
        limits=(
            LLMBudgetLimit("day", calls=100, input_units=100_000, output_units=100_000),
        ),
    )
    engine = AssistantEngine(
        services=services,
        router=RuleIntentRouter(clock),
        classifier=ModelIntentClassifier(llm, model="fake-model"),
        llm_gateway=llm,
        llm_model="fake-model",
        pending_plans=PendingPlanStore(database),
    )
    return engine, services, context, client


@pytest.mark.parametrize(
    "message",
    [
        FINANCIAL_MESSAGE,
        # The deterministic prefix writes a note with no model involved, so a
        # gate placed in the classifier would miss exactly this case.
        "/记 银行卡密码是 000000",
        "/备忘 支付密码是 111111",
    ],
)
def test_a_financial_credential_reaches_neither_the_model_nor_the_database(
    engine_parts: tuple[AssistantEngine, ZhixuServices, CommandContext, ExhaustedLLM],
    message: str,
) -> None:
    engine, services, context, client = engine_parts

    reply = engine.handle(message, context, target_ref="qqc_synthetic")

    assert reply.code == "financial_credential_blocked"
    assert client.calls == 0
    assert services.query_bus().execute(SearchNotes("密码", limit=10), context) == []
    assert services.notes.list_for_owner("user_test") == []


def test_the_refusal_names_the_admin_page_and_never_echoes_the_value(
    engine_parts: tuple[AssistantEngine, ZhixuServices, CommandContext, ExhaustedLLM],
) -> None:
    engine, _services, context, _client = engine_parts

    reply = engine.handle(FINANCIAL_MESSAGE, context, target_ref="qqc_synthetic")

    assert "敏感数据仓" in reply.text
    assert "000000" not in reply.text
