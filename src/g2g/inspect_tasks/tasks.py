from __future__ import annotations

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ChatMessageAssistant, ChatMessageSystem, ChatMessageUser, get_model
from inspect_ai.solver import solver

from g2g.budget import request_cost
from g2g.compiler import compile_audit_request
from g2g.inspect_tasks.provider import REQUEST_CONTEXT
from g2g.models import IdentityBinding, Request, VisiblePacket
from g2g.util import digest


def compile_spec(spec: dict, context) -> tuple[Request, list[dict]]:
    if spec["purpose"] == "decision":
        compiled = compile_audit_request(
            VisiblePacket.model_validate(spec["visible"]),
            spec["arm"],
            IdentityBinding.model_validate(spec["binding"]),
            spec.get("history"),
            spec.get("swap", False),
            spec.get("ledger"),
        )
        messages = compiled["messages"]
        payload = context.codec.encode(messages, compiled["native_prefix"])
    else:
        messages = spec["messages"]
        payload = context.codec.encode(messages, spec.get("native_prefix"))
    planning, maximum = request_cost(payload, context.settings, context.price)
    request = Request(
        key=spec["key"],
        study_id=context.study_id,
        phase=spec["phase"],
        model_id=context.settings["model"],
        purpose=spec["purpose"],
        block_id=spec["block_id"],
        payload=payload,
        config_hash=digest({"model": context.settings, "price": context.price}),
        planning_microusd=planning,
        maximum_microusd=maximum,
        metadata=spec.get("metadata", {}),
        dependencies=spec.get("dependencies", []),
    )
    return request, messages


def audit_task(specs: list[dict], context):
    @solver
    def journal_solver():
        async def solve(state, generate):
            spec = specs[state.metadata["index"]]
            try:
                request, messages = compile_spec(spec, context)
            except BaseException as exc:
                context.coordinator.halted = True
                context.coordinator._fail(context.coordinator.expected[spec["block_id"]], exc)
                with context.journal.db:
                    context.journal.event(
                        context.study_id, spec["key"], "compile_audit_request", "failed", {"error": str(exc)}
                    )
                raise
            with context.journal.db:
                context.journal.event(
                    context.study_id,
                    request.key,
                    "compile_audit_request",
                    "compiled_once",
                    {"payload_hash": digest(request.payload), "config_hash": request.config_hash},
                )
            cls = {"system": ChatMessageSystem, "user": ChatMessageUser, "assistant": ChatMessageAssistant}
            state.messages = [cls[m["role"]](content=m["content"]) for m in messages]
            token = REQUEST_CONTEXT.set(request)
            try:
                state.output = await get_model().generate(state.messages, cache=False)
                state.messages.append(state.output.message)
                state.completed = True
            finally:
                REQUEST_CONTEXT.reset(token)
            return state

        return solve

    # Gold is absent from Sample, target, metadata and model API context.
    samples = [
        Sample(id=s["key"], input="Journal-backed evaluation", metadata={"index": i})
        for i, s in enumerate(specs)
    ]
    return Task(name="g2g_" + specs[0]["phase"], dataset=MemoryDataset(samples), solver=journal_solver())
