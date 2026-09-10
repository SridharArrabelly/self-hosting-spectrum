# self-hosting-spectrum

**Four ways to host a model on Azure. One API Management gateway. One client, one payload, four runtimes.**

This repository is a working demo *and* an implementation guide for the four model-hosting patterns in
*Microsoft — Self-hosted model options*. Each option hands a different amount of the runtime to the customer,
from "Foundry runs the GPUs for you" to "the model runs on your laptop". Every option is published through a
**single Azure API Management gateway** as an OpenAI-compatible `/chat/completions` route, so the same client
code and the same request body work against all four.

That is the whole point: **the hosting decision should not leak into your application code.**

---

## The four options

| # | Option | Who owns the runtime | Where inference happens | Route |
|---|---|---|---|---|
| 1 | **Foundry Managed Compute** | Microsoft Foundry | Azure, dedicated GPUs | `/v1/managed-compute/chat/completions` |
| 2 | **Fireworks on Foundry** | Fireworks AI (partner) | Azure, shared serverless | `/v1/fireworks/chat/completions` |
| 3 | **Azure VM** | You | Azure, your VM, your server | `/v1/azure-vm/chat/completions` |
| 4 | **Foundry Local** | You | Your own machine | `/v1/foundry-local/chat/completions` |

<!-- SECTIONS TO BE COMPLETED DURING BUILD (see plan §6):
  - Decision tree
  - Architecture
  - Prerequisites
  - Quickstart
  - Per-option technical design (x4)
  - The APIM gateway layer
  - Foundry Agent Service BYOM
  - Demo script
  - Cost and teardown
  - Troubleshooting
  - References
-->

---

## Status

🚧 Under construction. See the build phases in the repository history.

---

## Behind a corporate package feed

`uv` does not read `pip.ini`. If your machine routes PyPI through an internal proxy, create a **gitignored**
`uv.toml` in the repo root:

```toml
[[index]]
url = "https://<your-feed>/pypi/simple/"
default = true
```

Or set `UV_INDEX_URL` in your shell. The repository itself defaults to public PyPI.
