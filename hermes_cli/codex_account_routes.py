"""Manual Codex provider routes reuse one profile-owned credential assignment."""
from __future__ import annotations

from dataclasses import replace

CODEX_PROVIDER = "openai-codex"
ACCOUNT_PREFIX = f"{CODEX_PROVIDER}:"


def codex_account_id(provider):
    name = str(provider or "").strip().lower()
    if name.startswith(ACCOUNT_PREFIX):
        account_id = name[len(ACCOUNT_PREFIX):]
        if account_id and all(char.isalnum() or char == "-" for char in account_id):
            return account_id
    return None


def canonical_codex_provider(provider):
    return CODEX_PROVIDER if codex_account_id(provider) else provider


def account_route_pool(provider):
    """A fresh pool view retains refresh/persistence ownership but cannot rotate to siblings."""
    account_id = codex_account_id(provider)
    if account_id is None:
        return None
    from agent.credential_pool import load_pool

    pool = load_pool(CODEX_PROVIDER)
    pool._entries = [entry for entry in pool.entries() if entry.id == account_id]
    pool.account_id = account_id
    return pool


def account_pool_matches(pool, provider):
    account_id = codex_account_id(provider)
    if account_id is None:
        return None
    return (getattr(pool, "provider", None) == CODEX_PROVIDER
            and (getattr(pool, "account_id", None) or getattr(pool, "id", None)) == account_id)


def primary_account_route(runtime):
    requested = runtime.get("requested_provider")
    return requested if codex_account_id(requested) else runtime.get("provider", "")


def account_provider_def(provider):
    if not codex_account_id(provider):
        return None
    from hermes_cli.providers import get_provider

    return replace(get_provider(CODEX_PROVIDER), id=provider.strip().lower())


def append_account_providers(rows, current_provider):
    from agent.credential_accounts import account_metadata
    from agent.credential_pool import load_pool

    base = next((row for row in rows if row.get("slug") == CODEX_PROVIDER), None)
    if base is None:
        return
    pool = load_pool(CODEX_PROVIDER)
    for entry in pool.entries():
        metadata = account_metadata(pool, entry)
        slug = ACCOUNT_PREFIX + entry.id
        if not metadata["enabled"] and slug != current_provider:
            continue
        row = {
            **base, "slug": slug, "name": f'{base["name"]} — {metadata["label"] or entry.id}',
            "is_current": slug == current_provider, "authenticated": metadata["enabled"],
            "account_id": entry.id,
        }
        if not metadata["enabled"]:
            row["models"] = []
        rows.append(row)


def resolve_account_runtime(provider, target_model=None):
    from hermes_cli import runtime_provider as runtime

    model_cfg = runtime._get_model_config()
    route = model_cfg.get("provider", "") if provider == "auto" else provider
    if not codex_account_id(route):
        return None
    pool = account_route_pool(route)
    entry = pool.select(model=target_model or None)
    if entry is None or not runtime.has_usable_secret(runtime._pool_entry_api_key(entry)):
        raise runtime.AuthError(
            "Selected Codex account is unavailable. Enable or connect this account in Providers → Accounts.",
            provider=route, code="account_unavailable",
        )
    return runtime._resolve_runtime_from_pool_entry(
        provider=CODEX_PROVIDER, entry=entry, requested_provider=route,
        model_cfg=model_cfg, pool=pool, target_model=target_model,
    )


def resolve_account_client(req):
    from agent import auxiliary_client as auxiliary

    runtime = resolve_account_runtime(req.provider, req.model)
    client = auxiliary._create_openai_client(
        api_key=runtime["api_key"], base_url=runtime["base_url"],
        default_headers=auxiliary._codex_cloudflare_headers(runtime["api_key"], base_url=runtime["base_url"]),
    )
    model = auxiliary._normalize_resolved_model(req.model, CODEX_PROVIDER)
    if req.raw_codex:
        return client, model
    return auxiliary._route_client(req, auxiliary.CodexAuxiliaryClient(client, model), model)
