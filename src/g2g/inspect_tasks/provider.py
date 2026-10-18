from __future__ import annotations

from contextvars import ContextVar

from inspect_ai.model import ModelAPI, ModelCall, ModelOutput, ModelUsage

REQUEST_CONTEXT = ContextVar("g2g_request")
ACTIVE_COORDINATOR = None


class JournalModelAPI(ModelAPI):
    def should_retry(self, ex):
        return False

    async def generate(self, input, tools, tool_choice, config):
        if ACTIVE_COORDINATOR is None:
            raise RuntimeError("Use the g2g CLI to initialise the journal-backed runner")
        request = REQUEST_CONTEXT.get()
        observation, origin = await ACTIVE_COORDINATOR.generate(request)
        output = ModelOutput.from_content(
            self.model_name,
            observation["final_text"],
            stop_reason="max_tokens"
            if observation["status"] == "truncated"
            else "content_filter"
            if observation["status"] == "refused"
            else "stop",
        )
        usage = observation["usage"]
        if usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
            output.usage = ModelUsage(
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["input_tokens"] + usage["output_tokens"],
            )
        output.metadata = {
            "origin": origin,
            "observation_id": observation["observation_id"],
            "batch_id": observation["batch_id"],
            "original_collected_at": observation["collected_at"],
            "usage_is_replayed": origin == "journal_replay",
            "status": observation["status"],
        }
        return output, ModelCall.create(request=request.payload, response=observation["raw"])
