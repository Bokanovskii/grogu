---
name: apple-silicon-memory-ceiling
description: Measure what an Apple Silicon Mac will actually give a local LLM (Metal working-set limit, real pressure, swap-in rate) before planning or debugging around it
---

# Measuring what a Mac will actually give an LLM, before planning around it

Apply this whenever work depends on how much memory a local model may use on an
Apple Silicon Mac — planning a runtime, choosing a model, or debugging a machine
that becomes unusable during inference. Every number below is obtainable in under
a minute, and every one of them is routinely guessed wrong from memory.

## Procedure

1. **Physical memory, in bytes, not marketing gigabytes.**
   `sysctl -n hw.memsize` — a "16GB" Mac reports 17,179,869,184.

2. **The real ceiling for GPU work.** This is the number that matters, and it is
   *not* `hw.memsize`. Compile and run a three-line probe:
   ```swift
   import Metal
   let d = MTLCreateSystemDefaultDevice()!
   print(d.recommendedMaxWorkingSetSize, d.hasUnifiedMemory)
   ```
   `swiftc probe.swift -o probe && ./probe`. On a 16GB M5 this is 12,713,115,648
   bytes (11.84 GiB, about 74% of RAM). Read it at runtime; it varies by machine
   and by OS version, so a hardcoded fraction will be wrong somewhere.

3. **How much pressure the machine is under right now.**
   `sysctl kern.memorystatus_level` is the free-memory percentage — the same number
   `memory_pressure` prints as "System-wide memory free percentage". `memory_pressure`
   also gives lifetime swapins/swapouts and compressor state; `sysctl
   vm.compressor_bytes_used` gives compressed bytes now. Take this reading on the
   machine *in its normal state*, not freshly booted: a real working Mac is often at
   35–45% free with several GB already compressed, and any budget that only works on
   an idle machine is fiction.

4. **Swap-in rate, which is what "the Mac beachballs" actually is.**
   Sample `vm_stat`'s "Swapins" twice and divide by the interval. Rising swap-ins
   during inference is the signal to act on; free percentage alone lags.

5. **Write the numbers down with how you got them.** Put the command beside the
   value in whatever artifact you produce. The next agent cannot tell a measured
   number from a remembered one, and remembered ones are wrong about this hardware
   in particular.

## Checks

* Predicted resident size must always be **≥** what actually loads. Verify by
  loading the model and reading `size_vram` from `GET /api/ps` (Ollama), then compare.
  Optimism here is the failure that makes the whole machine unusable; pessimism only
  costs context length.
* If `ollama ps` / `/api/ps` reports a `PROCESSOR` that is CPU or a split, the
  configuration does not fit, whatever the arithmetic said. Treat that observation as
  authoritative and remember it.
