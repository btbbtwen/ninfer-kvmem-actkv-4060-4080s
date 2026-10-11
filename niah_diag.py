"""Focused diagnostic: 4 rounds of ~17k writes with 3 needles each, probing the
just-written needles after every round and printing the RAW model output.

Round 4 is the first that crosses the 53248-token pool (3 x ~17.2k + 17.2k), so
this reproduces the exact point where same-turn recall collapsed from 10/10 to
0/3 in the longer run -- in about a quarter of the time.

Run with the server up:
    python niah_diag.py
"""

from __future__ import annotations

import json
import re
import time
import urllib.request

ENDPOINT = "http://127.0.0.1:18200/v1/chat/completions"
MODEL = "ninfer-27b"
FILLER_LINES = 450
ROUNDS = 7
# Mirror the full harness exactly: round 1 plants four needles, the rest three,
# and all four tools are advertised so the tool schema matches.
NEEDLE_PLAN = [4, 3, 3, 3, 3, 3, 3]

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


def post(payload, timeout=1800):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(ENDPOINT, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run_tool(name, args):
    if name == "record_finding":
        return {"status": "recorded", "code": args.get("code"), "value": args.get("value")}
    if name == "lookup_metric":
        return {"status": "ok", "metric": args.get("metric"), "value": "n/a"}
    if name == "get_time":
        return {"status": "ok", "time": "2026-10-11T00:00:00Z"}
    if name == "send_alert":
        return {"status": "sent", "channel": args.get("channel")}
    return {"status": "unknown"}


def converse(messages, max_tokens, effort="none", hops=4):
    """Run one turn, servicing tool calls with proper role=tool replies."""
    evidence, tools_used = [], 0
    for _ in range(hops):
        resp = post({"model": MODEL, "messages": messages, "tools": TOOLS,
                     "tool_choice": "auto", "max_tokens": max_tokens,
                     "temperature": 0.0, "reasoning_effort": effort})
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            return resp, evidence, tools_used
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        tools_used += len(calls)
        for c in calls:
            fn = c.get("function", {})
            evidence.append(fn.get("arguments") or "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": json.dumps(run_tool(fn.get("name", ""), args))})
    return {}, evidence, tools_used


def build_round(idx, first):
    lines = [
        f"Archive entry {idx * FILLER_LINES + i} : the regional logistics report for "
        f"sector {(i * 7) % 977} lists ordinary throughput figures, routine staffing "
        f"notes and no unusual incidents in the ledger for that period."
        for i in range(FILLER_LINES)
    ]
    n = NEEDLE_PLAN[idx]
    codes = []
    for k in range(n):
        code = f"N{first + k:02d}"
        value = f"{code}-{(idx * 7919 + k * 104729) % 1000000:06d}"
        pos = (k + 1) * (FILLER_LINES // (n + 1))
        lines.insert(pos,
                     f"CRITICAL AUDIT NOTE: the sealed compliance code for batch {code} "
                     f"is exactly {value}. This value is unique and must be recalled verbatim.")
        codes.append((code, value))
    body = "\n".join(lines) + (f"\n\nRound {idx + 1} of {ROUNDS}. Reply with the "
                               f"single word ACK and nothing else.")
    return body, codes


def main():
    messages = []
    cursor = 1
    for r in range(ROUNDS):
        body, codes = build_round(r, cursor)
        cursor += len(codes)
        messages.append({"role": "user", "content": body})
        t0 = time.time()
        resp, _, wtools = converse(messages, max_tokens=32)
        wt = time.time() - t0
        tim = resp.get("timings") or {}
        print(f"\n=== round {r + 1} write ===")
        print(f"  prompt_tokens={tim.get('prompt_n')} prefill={tim.get('prompt_per_second', 0):.1f} tok/s "
              f"({wt:.1f}s) tools={wtools}")

        q = ("Using the archive above, state the sealed compliance code for: "
             + ", ".join(c for c, _ in codes)
             + ". Call record_finding once per batch with `code` and `value`.")
        messages.append({"role": "user", "content": q})
        t0 = time.time()
        presp, pev, ptools = converse(messages, max_tokens=600)
        pt_ = time.time() - t0
        pmsg = presp["choices"][0]["message"]
        content = (pmsg.get("content") or "").strip()
        pfin = (presp.get("timings") or {})
        evidence = content + " " + " ".join(pev)
        hits = [c for c, v in codes if re.search(re.escape(v), evidence)]
        print(f"=== round {r + 1} probe ===")
        print(f"  prompt_tokens={pfin.get('prompt_n')} prefill="
              f"{pfin.get('prompt_per_second', 0):.1f} tok/s decode="
              f"{pfin.get('predicted_per_second', 0):.1f} tok/s ({pt_:.1f}s) tools={ptools}")
        print(f"  recall {len(hits)}/{len(codes)}  expected={[c for c, _ in codes]}")
        print(f"  RAW CONTENT: {content[:600]!r}")
        if pev:
            print(f"  TOOL ARGS  : {pev}")


if __name__ == "__main__":
    main()
