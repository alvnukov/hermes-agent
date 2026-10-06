# Codex Guardian source attribution

These policy resources are from [openai/codex](https://github.com/openai/codex)
at commit `8e23d1836fd22d1c920533f39851db1c7d795744` (Apache-2.0).

- `policy.md` and `policy_template.md`: unchanged copies of
  `codex-rs/prompts/templates/guardian/`.
- `output_contract.md`: extracted unchanged from
  `codex-rs/ext/guardian-reviewer/src/assessment.rs:107-118`.
- `LICENSE` and `NOTICE`: upstream distribution notices.

`agent/guardian_protocol.py` adapts the request/schema/parser contract to Python.
It appends an explicit limitation because Hermes exposes no reviewer inspection
tools, uses account-advertised subscription capabilities only, and fails closed
instead of using upstream's ordinary-model or API-key fallback. The caller owns
authentication, action binding, complete evidence, cancellation and transport.
The appended environment override explains that this independent request does not
share Codex's execution environment or sandbox. The parser also intentionally
requires a complete JSON object: prose wrappers, duplicate/extra keys and explicit
null fields fail closed, while omitted optional fields retain upstream defaults.

Backend availability, entitlement and reviewer credit treatment remain server
decisions. Client metadata or a bundled model description cannot prove them.
