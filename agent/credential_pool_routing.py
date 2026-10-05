"""Match configured endpoint identities against durable credential pool keys."""
import logging

logger = logging.getLogger(__name__)


def keyed_custom_pool_matches(pool_provider, provider_norm, base_url):
    from agent import credential_pool as pool

    runtime_url = pool._norm_url(base_url)
    if not runtime_url:
        return False
    try:
        for normalized_name, entry in pool._iter_custom_providers():
            provider_key = pool._normalize_custom_pool_name(str(entry.get("provider_key") or ""))
            if provider_key != pool_provider:
                continue
            aliases = pool._custom_entry_name_aliases(normalized_name, entry)
            aliases.add(f"{pool.CUSTOM_POOL_PREFIX}{normalized_name}")
            if provider_key:
                aliases.add(f"{pool.CUSTOM_POOL_PREFIX}{provider_key}")
            configured_url = pool._norm_url(entry.get("base_url"))
            if provider_norm == "custom":
                return runtime_url == configured_url
            runtime_aliases = pool._requested_custom_name_aliases(provider_norm)
            return bool(runtime_aliases & aliases) and runtime_url == configured_url
    except Exception:
        logger.debug("Could not match custom credential pool identity", exc_info=True)
        return False
    return False
