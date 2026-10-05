"""Codex auxiliary client construction, including explicit account routes."""
from hermes_cli.codex_account_routes import codex_account_id, resolve_account_client


def resolve_codex_client(req):
    from agent import auxiliary_client as auxiliary

    if codex_account_id(req.provider):
        from hermes_cli.auth import AuthError
        try:
            return resolve_account_client(req)
        except AuthError as exc:
            if exc.code != "account_unavailable":
                raise
            return None, None
    model = req.model
    if not model:
        auxiliary.logger.warning("resolve_provider_client: openai-codex requested without a "
                                 "model; pass model explicitly (e.g. model.model in config.yaml "
                                 "or auxiliary.<task>.model for per-task aux routing).")
        return None, None
    no_token_msg = "resolve_provider_client: openai-codex requested but no Codex OAuth token found (run: hermes model)"
    if req.raw_codex:
        # Raw OpenAI client for callers needing responses.stream() (main agent loop).
        token, base_url = auxiliary._resolve_codex_credential_and_base()
        if not token:
            auxiliary.logger.warning(no_token_msg)
            return None, None
        client = auxiliary._create_openai_client(
            api_key=token, base_url=base_url,
            default_headers=auxiliary._codex_cloudflare_headers(token, base_url=base_url),
        )
        return client, auxiliary._normalize_resolved_model(model, req.provider)
    client, default = auxiliary._build_codex_client(model)
    return auxiliary._route_or_warn(req, client, default, no_token_msg)
