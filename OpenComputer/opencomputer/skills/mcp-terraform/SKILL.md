---
name: mcp-terraform
description: Use Terraform MCP for module discovery, registry search, provider docs, state inspection, and IaC scaffolding. Use when the user is writing or debugging Terraform / OpenTofu, asks for a module to do X, needs the latest aws_s3_bucket arguments, wants to inspect tfstate, or is migrating between provider versions. Read-only by default — applies are still done via terraform CLI.
version: 0.1.0
---

# Terraform MCP

The Terraform MCP server gives the agent typed access to:

- Module discovery in the public registry (`registry.terraform.io`)
- Provider documentation lookup (every resource + data source)
- Schema introspection for the current state
- Read-only inspection of `terraform.tfstate`

It does NOT run `terraform apply` — that always stays under your
control via the `terraform` CLI (or `tofu` for OpenTofu).

## Install (one-time)

```bash
oc mcp add terraform \
  --command npx \
  --args "-y,@modelcontextprotocol/server-terraform"

# OR if you prefer the HashiCorp-maintained server:
oc mcp add terraform-hashicorp \
  --transport http \
  --url https://mcp.terraform.io/mcp
```

Confirm: `oc mcp list | grep terraform`.

For OpenTofu users, the same server works because the registry +
provider schemas are compatible.

## Common operations

| User asks | Tool to call |
|-----------|--------------|
| "Find a module to provision Cloudfront with ACM" | `terraform.search_modules(query="cloudfront acm")` |
| "What are the new args for `aws_s3_bucket` v5?" | `terraform.provider_docs(provider="aws", resource="s3_bucket")` |
| "What's in my current state?" | `terraform.state_inspect(file="terraform.tfstate")` |
| "Diff a tfstate against another revision" | `terraform.state_diff(file_a, file_b)` |
| "Resolve the canonical version of `terraform-aws-modules/vpc/aws`" | `terraform.module_metadata(...)` |

## Companion CLI commands (NOT MCP — you still run these)

```bash
terraform init
terraform plan -out=plan.bin
terraform show plan.bin              # inspect what apply would do
terraform apply plan.bin             # ← always human-attended

# OpenTofu users
tofu init / plan / apply
```

The MCP server is for **research, lookup, and inspection**. Apply
operations stay in human-attended CLI scope because they're
hard-to-reverse and affect shared infrastructure (per OC's "risky
actions need confirmation" rule).

## When to use Terraform MCP (vs alternatives)

| Question | Best path |
|----------|-----------|
| "What does this resource do?" | `terraform.provider_docs` (Terraform MCP) |
| "Find a community module for X" | `terraform.search_modules` (Terraform MCP) |
| "Show me my current state" | `terraform.state_inspect` (Terraform MCP) |
| "Will this plan break things?" | `terraform plan` CLI + `terraform show plan.bin` |
| "Apply these changes" | `terraform apply` CLI (you, not the agent) |
| "What does AWS X cost?" | AWS MCP / `mcp__plugin_deploy-on-aws_awspricing` |
| "Check CloudFormation compliance" | `mcp__plugin_deploy-on-aws_awsiac` |

## Safety

- This server is **read-only over Terraform state**. No mutations.
- The agent should never invoke `terraform apply` itself — it should
  ALWAYS show the plan and ask the user to apply.
- Sensitive variables (api keys, certs) in state should not be quoted
  back to the user — the server returns them by default; redact in
  responses.

## See also

- `mcp__plugin_deploy-on-aws_awsiac` — AWS-specific IaC tools (CDK,
  CloudFormation)
- `mcp__plugin_deploy-on-aws_awspricing` — pricing for resources you
  plan to provision
- `oc mcp list` / `oc mcp remove`
