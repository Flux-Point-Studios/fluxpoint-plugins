---
name: fluxpoint-grill-me
description: Draft and stress-test a spec before implementation with researched options, recommended baselines and evidence. Use for spec-first design, grill-me requests, or the required decision pass before Flux Point loop and graph implementation.
user-invocable: false
disable-model-invocation: true
---

Read `../../commands/grill-me.md` relative to this skill and perform its
workflow. The user's request supplies the goal. Claude Code reaches that
command through `/fluxpoint:grill-me`; Codex reaches it through this skill.
The Claude-only frontmatter keys prevent duplicate menu entries there.

Use the same packet and decision protocol in either runtime. If an optional
question tool is unavailable, present the model's baseline and continue
authorized work. Genuine authorization requirements still need the user's
answer. Never infer approval from a default or a missing reply.

For any commit made by a Codex session without the Workflow tool, place
`Fluxpoint-Executor: codex` in the final Git trailer block. Direct repairs
remain unchecked until an independent repair run names their SHAs in a
FixCheckV1 PASS or a recorded DecisionV1 explicitly accepts them.
