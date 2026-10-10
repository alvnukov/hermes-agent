"""Native Relay boundary for iteration-summary requests."""


def managed_summary_call(agent, api_request_id: str, request, callback, *, retry_count: int):
    from agent import relay_llm

    return relay_llm.execute_current(
        request, callback,
        name=str(getattr(agent, "provider", "") or "provider"), model_name=str(getattr(agent, "model", "") or ""),
        metadata={"api_mode": str(getattr(agent, "api_mode", "") or "chat_completions"),
                  "api_request_id": api_request_id, "call_role": "iteration_summary", "retry_count": retry_count},
        defer_logical_completion=True,
    )
