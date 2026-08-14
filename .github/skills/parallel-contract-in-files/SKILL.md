---
name: parallel-contract-in-files
description: When parallel agents agree an interface between their paths, write it into a file before building against it.
---

Two engineers on disjoint paths need an interface between them. The interface
gets agreed in a message, and an hour later each context remembers a slightly
different version of it — or is compacted and remembers none.

1. Agree the shape peer-to-peer, in as few messages as it takes.
2. Before building against it, write it into a file that one of you owns: the
   field name, every state it can take, and what each state means to the reader.
3. Cite the test that pins it, by path, in that same comment.
4. Only then build.

The test to apply: could a third agent, starting from an empty context and
owning neither of your paths, build against this contract without asking either
of you? If not, it is still a conversation, not a contract.

This is not about mistrust. Both engineers here were careful and the contract
was correct. It is that a message is not a durable artifact, and the agent who
needs the contract most is usually the one who was not in the conversation.
