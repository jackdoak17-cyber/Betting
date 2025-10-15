#!/usr/bin/env python3
if price < MIN_DEC_PRICE: return
row = {
"league_id": lid,
"fixture": f"{home} vs {away}",
"team": team,
"opp": opp,
"side": ev_side,
"stat": stat,
"safe_min": int(T),
"line": float(line),
"price": float(price),
"market": market_name or "",
"book": "Bet365",
}
if bucket == "p80": p80_rows.append(row)
else: p100_rows.append(row)


maybe_add(w.get("p80"), "p80")
maybe_add(w.get("p100"), "p100")


# 6) Sort and render
p80_rows.sort(key=lambda r: (-r["price"], r["fixture"], r["team"], r["stat"]))
p100_rows.sort(key=lambda r: (-r["price"], r["fixture"], r["team"], r["stat"]))


header = (
f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}\n"
f"Min price: {MIN_DEC_PRICE:.2f} | Capture≥{CAPTURE_FLOOR:.2f} | Bookmaker: {BOOKMAKERS} | WINDOW_DAYS={WINDOW_DAYS}\n"
)


def fmt_row(r: dict) -> str:
stat_label = {
"shots": "Shots",
"shots_on_target": "SOT",
"corners": "Corners",
"tackles": "Tackles",
}.get(r["stat"], r["stat"])
return (f" • {r['team']} — {stat_label} Over {r['line']:.1f} @ {r['price']:.3f} | {r['fixture']} | "
f"min={r['safe_min']} | {r['market']}")


lines = [header]
lines.append("===== TEAM MIN 80 — Over candidates =====")
if p80_rows:
for r in p80_rows: lines.append(fmt_row(r))
else:
lines.append("No matches found (no qualifying Over lines at or above min price).")
lines.append("")


lines.append("===== TEAM MIN 100 — Over candidates =====")
if p100_rows:
for r in p100_rows: lines.append(fmt_row(r))
else:
lines.append("No matches found (no qualifying Over lines at or above min price).")
lines.append("")


OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


with OUT_NDJSON.open("w", encoding="utf-8") as f:
for r in p80_rows:
rr = dict(r); rr["bucket"] = "p80"; f.write(json.dumps(rr, ensure_ascii=False) + "\n")
for r in p100_rows:
rr = dict(r); rr["bucket"] = "p100"; f.write(json.dumps(rr, ensure_ascii=False) + "\n")


print(header)
print(f"[RESULT] p80={len(p80_rows)} p100={len(p100_rows)} (written to {OUT_TXT})")


if __name__ == "__main__":
try:
main()
except KeyboardInterrupt:
pass
