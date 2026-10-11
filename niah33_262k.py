"""Full-context KVMem + ActKV acceptance test.

Target: the 262144-token max-context setting, 33 needles, ~80 tool calls, with
per-stage speed instrumentation (write prefill, same-turn probe, final recall,
retry) so the report can separate prefill from decode and pool-saturated from
pool-free behaviour.

Budget arithmetic
-----------------
15 rounds x 450 filler lines. A filler line calibrated at 36.61 tokens on this
server, so each round appends ~16.7k (needles and the instruction included),
which lands the session at ~256.7k -- just inside the 262144 cap while leaving
room for the recall turns. More rounds overflow; fewer waste the window.

33 needles over 15 rounds: three rounds plant 3, twelve plant 2 -> 9 + 24 = 33.

Tool calls: 33 same-turn probes + 33 in the final sweep + retries, which lands
near the 80 the run is asked to exercise.

Usage:
    python niah33_262k.py            # full run against 127.0.0.1:18200
    python niah33_262k.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "http://127.0.0.1:18200/v1/chat/completions"
MODEL = "ninfer-27b"
# 435 filler lines instead of 450. At 450 the session reached 260,948 tokens by
# round 15, which left only ~157 tokens under the 262144 cap -- enough for the
# first recall batch and nothing else (batch 2 came back HTTP 400 "context length
# exceeded"). 435 lines lands round 15 near 249k and leaves ~11k for the final
# recall sweep, so the whole protocol fits while still filling 95% of the window.
FILLER_LINES = 435
ROUNDS = 15
NEEDLE_PLAN = [3, 3, 3] + [2] * 12
assert len(NEEDLE_PLAN) == ROUNDS, len(NEEDLE_PLAN)
TOTAL_NEEDLES = sum(NEEDLE_PLAN)
assert TOTAL_NEEDLES == 33, TOTAL_NEEDLES

# How many needles the cross-tool pass re-verifies through send_alert. 33
# same-turn + 33 final + 14 cross-tool = 80 tool calls, which is the target the
# run is specified against.
CROSS_TOOL_N = 14

TOOLS = [
    {"type": "function", "function": {
        "name": "record_finding",
        "description": "Persist an audit finding to the case file.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}, "value": {"type": "string"}},
            "required": ["code", "value"]}}},
    {"type": "function", "function": {
        "name": "lookup_metric",
        "description": "Look up a metric by its identifier.",
        "parameters": {"type": "object", "properties": {"metric": {"type": "string"}},
                       "required": ["metric"]}}},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Return the current time for a timezone.",
        "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}},
                       "required": ["timezone"]}}},
    {"type": "function", "function": {
        "name": "send_alert",
        "description": "Send an alert to an on-call channel.",
        "parameters": {"type": "object", "properties": {
            "channel": {"type": "string"}, "message": {"type": "string"}},
            "required": ["channel", "message"]}}},
]


def post(payload: dict, timeout: int = 3600) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ContextFull(RuntimeError):
    """The server refused a turn because prompt + max_tokens exceeds max-context."""


def post_checked(payload: dict, timeout: int = 3600) -> dict:
    try:
        return post(payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        if e.code == 400 and "context length" in body.lower():
            raise ContextFull(body) from e
        raise


def ask(messages, tools=None, max_tokens=512, effort="none") -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "reasoning_effort": effort,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return post_checked(payload)


def run_tool(name: str, args: dict) -> dict:
    if name == "record_finding":
        return {"status": "recorded", "code": args.get("code"), "value": args.get("value")}
    if name == "lookup_metric":
        return {"status": "ok", "metric": args.get("metric"), "value": "n/a"}
    if name == "get_time":
        return {"status": "ok", "timezone": args.get("timezone"), "time": "2026-10-11T00:00:00Z"}
    if name == "send_alert":
        return {"status": "sent", "channel": args.get("channel")}
    return {"status": "unknown_tool"}


def converse(messages, max_tokens, effort="none", hops=4):
    """One turn, servicing tool calls with the role=tool replies the API needs."""
    evidence: list[str] = []
    tools_used = 0
    last: dict = {}
    for _ in range(hops):
        resp, elapsed = None, 0.0
        t0 = time.time()
        resp = ask(messages, tools=TOOLS, max_tokens=max_tokens, effort=effort)
        elapsed = time.time() - t0
        last = resp
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            return resp, evidence, tools_used, elapsed
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        tools_used += len(calls)
        for call in calls:
            fn = call.get("function", {})
            evidence.append(fn.get("arguments") or "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": json.dumps(run_tool(fn.get("name", ""), args))})
    return last, evidence, tools_used, elapsed


def filler(round_index: int, i: int) -> str:
    return (
        f"Archive entry {round_index * FILLER_LINES + i} : the regional logistics "
        f"report for sector {(i * 7) % 977} lists ordinary throughput figures, "
        f"routine staffing notes and no unusual incidents in the ledger for that "
        f"period."
    )


def build_round(idx: int, first: int):
    lines = [filler(idx, i) for i in range(FILLER_LINES)]
    n = NEEDLE_PLAN[idx]
    codes = []
    for k in range(n):
        code = f"N{first + k:02d}"
        value = f"{code}-{(idx * 7919 + k * 104729) % 1000000:06d}"
        pos = (k + 1) * (FILLER_LINES // (n + 1))
        lines.insert(pos,
                     f"CRITICAL AUDIT NOTE: the sealed compliance code for batch {code} "
                     f"is exactly {value}. This value is unique and must be recalled "
                     f"verbatim.")
        codes.append((code, value))
    body = "\n".join(lines) + (f"\n\nRound {idx + 1} of {ROUNDS}. The notes above are "
                               f"the authoritative archive. Reply with the single word "
                               f"ACK and nothing else.")
    return body, codes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    all_codes: list[tuple[str, str]] = []
    cursor = 1
    for r in range(ROUNDS):
        _, codes = build_round(r, cursor)
        cursor += len(codes)
        all_codes.extend(codes)

    if args.dry_run:
        print(f"rounds={ROUNDS} needles={len(all_codes)} "
              f"plan={NEEDLE_PLAN}")
        print("codes:", ", ".join(c for c, _ in all_codes))
        return 0

    metrics: dict = {
        "needles": len(all_codes),
        "rounds": ROUNDS,
        "writes": [],
        "same_turn": [],
        "final_recall": [],
        "retry": [],
        "cross_tool": [],
    }
    messages: list[dict] = []
    immediate: list[tuple[str, str, bool]] = []
    cursor = 1
    total_tools = 0

    for r in range(ROUNDS):
        body, codes = build_round(r, cursor)
        cursor += len(codes)
        messages.append({"role": "user", "content": body})
        t0 = time.time()
        resp, _, wtools, _ = converse(messages, max_tokens=32)
        wt = time.time() - t0
        total_tools += wtools
        tim = resp.get("timings") or {}
        metrics["writes"].append({
            "round": r + 1, "prompt_n": tim.get("prompt_n"),
            "prefill_tps": tim.get("prompt_per_second"),
            "wall_s": round(wt, 2), "needles": len(codes),
        })
        print(f"round {r+1:2d}/{ROUNDS} write : +{tim.get('prompt_n')} tok | "
              f"prefill {tim.get('prompt_per_second', 0):6.1f} tok/s | {wt:5.1f}s")

        # Same-turn probe: is a just-written needle still readable?
        messages.append({"role": "user", "content":
                         "Using the archive above, state the sealed compliance code "
                         "for: " + ", ".join(c for c, _ in codes)
                         + ". Call record_finding once per batch with `code` and `value`."})
        t0 = time.time()
        presp, pev, ptools, pdt = converse(messages, max_tokens=600)
        total_tools += ptools
        pt = presp.get("timings") or {}
        blob = (presp["choices"][0]["message"].get("content") or "") + " " + " ".join(pev)
        hits = 0
        for code, value in codes:
            ok = re.search(re.escape(value), blob) is not None
            immediate.append((code, value, ok))
            hits += ok
        metrics["same_turn"].append({
            "round": r + 1, "recall": f"{hits}/{len(codes)}",
            "prefill_tps": pt.get("prompt_per_second"),
            "decode_tps": pt.get("predicted_per_second"),
            "wall_s": round(pdt, 2), "tools": ptools,
        })
        print(f"round {r+1:2d}/{ROUNDS} probe : prefill "
              f"{pt.get('prompt_per_second', 0):6.1f} tok/s | decode "
              f"{pt.get('predicted_per_second', 0):5.1f} tok/s | "
              f"{pdt:5.1f}s | recall {hits}/{len(codes)} | tools {ptools}")
        sys.stdout.flush()

    # Final recall sweep, batched so every turn's output fits in what is left of
    # the 262144 cap. The batch shrinks if the server reports the context is full,
    # so a run that fills the window more than expected still yields a result
    # instead of dying on HTTP 400.
    print(f"\n=== final recall sweep: {len(all_codes)} needles ===")
    hay: list[str] = []
    batch_size = 11
    start = 0
    while start < len(all_codes):
        batch = all_codes[start:start + batch_size]
        messages.append({"role": "user", "content":
                         "Audit recall phase. From the archive read earlier, report the "
                         "sealed compliance code for each of these batches: "
                         + ", ".join(c for c, _ in batch)
                         + ". Call record_finding once per batch with `code` and `value`, "
                         "and do not explain."})
        budget = max(300, 120 * len(batch))
        try:
            resp, ev, ntools, dt = converse(messages, max_tokens=budget)
        except ContextFull as e:
            if batch_size == 1:
                print(f"  context full and batch already minimal; stopping: {e}")
                messages.pop()   # drop the unanswerable question
                break
            batch_size = max(1, batch_size // 2)
            messages.pop()
            print(f"  context full -> retrying with batch size {batch_size}")
            sys.stdout.flush()
            continue
        total_tools += ntools
        hay.append(resp["choices"][0]["message"].get("content") or "")
        hay.extend(ev)
        tim = resp.get("timings") or {}
        metrics["final_recall"].append({
            "batch": start // batch_size + 1, "n": len(batch),
            "prompt_n": tim.get("prompt_n"),
            "prefill_tps": tim.get("prompt_per_second"),
            "decode_tps": tim.get("predicted_per_second"),
            "wall_s": round(dt, 2), "tools": ntools,
        })
        print(f"batch {start//batch_size+1} ({len(batch)} needles): prefill "
              f"{tim.get('prompt_per_second', 0):6.1f} tok/s | decode "
              f"{tim.get('predicted_per_second', 0):5.1f} tok/s | {dt:5.1f}s | "
              f"tools {ntools} | prompt {tim.get('prompt_n')}")
        sys.stdout.flush()
        start += len(batch)

    blob = "\n".join(hay)
    final_hits = [(c, v, re.search(re.escape(v), blob) is not None) for c, v in all_codes]
    n_strict = sum(1 for _, _, ok in final_hits if ok)

    # Cross-tool pass. The two recall stages above only ever exercise
    # record_finding, so at a clean 33/33 the run makes 66 calls and never
    # touches the other three advertised tools. Re-verifying a subset through
    # send_alert both brings the total to the 80 the run is asked to make and
    # checks that tool selection still works at full context with four schemas
    # live. Each needle is its own turn so one bad answer cannot mask another.
    print(f"\n=== cross-tool pass: {CROSS_TOOL_N} needles via send_alert ===")
    cross_ok = 0
    for code, value in all_codes[:CROSS_TOOL_N]:
        messages.append({"role": "user", "content":
                         f"Open incident review: send_alert to channel audit-log with a "
                         f"message containing the sealed compliance code for batch {code}."})
        try:
            resp, ev, ntools, dt = converse(messages, max_tokens=300)
        except ContextFull:
            messages.pop()
            print(f"  {code}: context full, stopping cross-tool pass")
            break
        total_tools += ntools
        b = (resp["choices"][0]["message"].get("content") or "") + " " + " ".join(ev)
        ok = re.search(re.escape(value), b) is not None
        cross_ok += ok
        tim = resp.get("timings") or {}
        metrics["cross_tool"].append({
            "code": code, "ok": ok, "prefill_tps": tim.get("prompt_per_second"),
            "decode_tps": tim.get("predicted_per_second"), "wall_s": round(dt, 2),
            "tools": ntools,
        })
        print(f"  {code} = {value} -> {'OK' if ok else 'MISS'} ({dt:.1f}s, tools {ntools}, "
              f"decode {tim.get('predicted_per_second', 0):.1f} tok/s)")
        sys.stdout.flush()

    misses = [(c, v) for c, v, ok in final_hits if not ok]
    retry: list[tuple[str, str, bool]] = []
    if misses:
        print(f"\n=== retry pass: {len(misses)} needle(s) ===")
        for code, value in misses:
            messages.append({"role": "user", "content":
                             f"Check the archive again carefully. What is the sealed "
                             f"compliance code for batch {code}? Call record_finding "
                             f"with `code` and `value`."})
            try:
                resp, ev, ntools, dt = converse(messages, max_tokens=400)
            except ContextFull:
                messages.pop()
                retry.append((code, value, False))
                print(f"  {code}: context full, cannot retry")
                sys.stdout.flush()
                continue
            total_tools += ntools
            b = (resp["choices"][0]["message"].get("content") or "") + " " + " ".join(ev)
            ok = re.search(re.escape(value), b) is not None
            retry.append((code, value, ok))
            tim = resp.get("timings") or {}
            metrics["retry"].append({
                "code": code, "ok": ok, "prefill_tps": tim.get("prompt_per_second"),
                "decode_tps": tim.get("predicted_per_second"), "wall_s": round(dt, 2),
                "tools": ntools,
            })
            print(f"  {code} = {value} -> {'OK' if ok else 'MISS'} ({dt:.1f}s, tools {ntools})")
            sys.stdout.flush()

    rmap = {c: ok for c, _, ok in retry}
    n_union = sum(1 for c, _, ok in final_hits if ok or rmap.get(c, False))
    n_imm = sum(1 for _, _, ok in immediate if ok)

    print("\n=== results ===")
    print(f"same-turn recall (fresh needles):    {n_imm}/{len(immediate)}")
    print(f"final recall (strict, first pass):   {n_strict}/{len(final_hits)}")
    print(f"final recall (after 1 retry, union): {n_union}/{len(final_hits)}")
    print(f"tool calls total: {total_tools}")
    print(f"cross-tool (send_alert) recall: {cross_ok}/{len(metrics['cross_tool'])}")
    print("\nper-needle (immediate/final/union):")
    fmap = {c: ok for c, _, ok in final_hits}
    for code, value, ok in immediate:
        print(f"  {code} = {value}  {'OK ' if ok else 'MISS'}/"
              f"{'OK ' if fmap.get(code) else 'MISS'}/"
              f"{'OK ' if (fmap.get(code) or rmap.get(code)) else 'MISS'}")

    w = [m["prefill_tps"] for m in metrics["writes"] if m["prefill_tps"]]
    st = [m["decode_tps"] for m in metrics["same_turn"] if m["decode_tps"]]
    stp = [m["prefill_tps"] for m in metrics["same_turn"] if m["prefill_tps"]]
    fr = [m["prefill_tps"] for m in metrics["final_recall"] if m["prefill_tps"]]
    frd = [m["decode_tps"] for m in metrics["final_recall"] if m["decode_tps"]]
    print("\n=== speed summary ===")
    if w:
        print(f"write prefill tok/s : min={min(w):.1f} max={max(w):.1f} "
              f"first={w[0]:.1f} last={w[-1]:.1f}")
    if stp:
        print(f"probe prefill tok/s : min={min(stp):.1f} max={max(stp):.1f}")
    if st:
        print(f"probe decode tok/s  : min={min(st):.1f} max={max(st):.1f}")
    if fr:
        print(f"recall prefill tok/s: min={min(fr):.1f} max={max(fr):.1f}")
    if frd:
        print(f"recall decode tok/s : min={min(frd):.1f} max={max(frd):.1f}")

    metrics["results"] = {
        "same_turn": f"{n_imm}/{len(immediate)}",
        "final_strict": f"{n_strict}/{len(final_hits)}",
        "final_union": f"{n_union}/{len(final_hits)}",
        "tool_calls": total_tools,
        "cross_tool": f"{cross_ok}/{len(metrics['cross_tool'])}",
    }
    with open(r"I:\dsh\html\niah33-262k-metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print("\nmetrics written to I:\\dsh\\html\\niah33-262k-metrics.json")
    return 0 if n_union == len(final_hits) else 1


if __name__ == "__main__":
    raise SystemExit(main())
