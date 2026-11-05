# Pinned upstream files

Source: Meta Platforms, Inc. and affiliates, [HyperAgents](https://github.com/facebookresearch/HyperAgents), commit `59a68f672dfb92c74aeb7e61535d776fb36e172d`.

The five files listed in `source-lock.json` are copied from that revision, with CRLF normalized to LF and one final LF appended during import. They retain the upstream **CC BY-NC-SA 4.0** license in `LICENSE.md`; do not present them as MIT/Apache code. No upstream source logic is changed. File hashes are checked before the adapter runs.

The lab's adjacent adapter, worker, model transport, file tools, score-store bridge and macOS execution boundary are integration code. `select_next_parent` runs its unchanged function AST using the lab public score store and a local seeded RNG. `MetaAgent`, `AgentSystem` and `chat_with_agent` run in the isolated worker. SDK transport and tool loading are supplied by the lab. Neither the original Docker runner, multi-domain harness, ensemble behavior nor all upstream editable files are reproduced by this bounded profile. There is no upstream endorsement.
