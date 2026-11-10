"""Draw docs/img/heldout-{light,dark}.svg from the two test-split summaries (no dependencies).

    uv run python -m tools.chart_heldout
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = [("Dawnmere 0.1.7", "results/reference/dawnmere/20261110T161902Z-test-s0/summary.json"),
        ("Mem0", "results/reference/mem0/20261110T184704Z-test-s0/summary.json")]
THEMES = {"light": dict(ink="#0b0b0b", soft="#52514e", grid="#e4e3df", series=["#2a78d6", "#eb6834"]),
          "dark": dict(ink="#f0efec", soft="#c3c2b7", grid="#3a3a38", series=["#3987e5", "#d95926"])}
W, LEFT, RIGHT, BAR, GAP, GROUP = 760, 250, 60, 14, 2, 30


def measures(s):
    h, leak = s["headline"], s["headline"]["leak_rates"]
    worst = max(leak, key=lambda k: leak[k]["rate"])
    return [("Overall score", "mean of 14 area pass rates", 100 * h["overall"], ""),
            ("Needed facts found", "share that reached the context", 100 * s["context"]["recall_mean"], ""),
            ("Worst leak rate", "lower is better", 100 * leak[worst]["rate"], f" ({worst})")]


def draw(theme):
    t = THEMES[theme]
    data = [(name, measures(json.loads((ROOT / path).read_text()))) for name, path in RUNS]
    span = W - LEFT - RIGHT
    rows = len(data[0][1])
    top = 34
    height = top + rows * (len(data) * (BAR + GAP) + GROUP) + 26
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" height="{height}" '
         f'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif" role="img" '
         f'aria-label="Held-out test split, 273 cases: overall score, needed facts found and worst leak rate for '
         f'Dawnmere 0.1.7 and Mem0">']
    for i, (name, _) in enumerate(data):  # legend
        x = LEFT + i * 150
        o.append(f'<rect x="{x}" y="6" width="12" height="12" rx="3" fill="{t["series"][i]}"/>'
                 f'<text x="{x + 18}" y="16" font-size="13" fill="{t["ink"]}">{name}</text>')
    for v in (0, 25, 50, 75, 100):  # recessive grid
        x = LEFT + span * v / 100
        o.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{height - 22}" stroke="{t["grid"]}" stroke-width="1"/>'
                 f'<text x="{x:.1f}" y="{height - 6}" font-size="11" fill="{t["soft"]}" text-anchor="middle">{v}%</text>')
    for r in range(rows):
        y0 = top + r * (len(data) * (BAR + GAP) + GROUP)
        label, note = data[0][1][r][0], data[0][1][r][1]
        o.append(f'<text x="{LEFT - 14}" y="{y0 + 12}" font-size="14" font-weight="600" fill="{t["ink"]}" '
                 f'text-anchor="end">{label}</text>'
                 f'<text x="{LEFT - 14}" y="{y0 + 28}" font-size="11.5" fill="{t["soft"]}" text-anchor="end">{note}</text>')
        for i, (_, ms) in enumerate(data):
            _, _, value, extra = ms[r]
            y, w = y0 + i * (BAR + GAP), span * value / 100
            o.append(f'<path d="M{LEFT},{y} h{w - 4:.1f} a4,4 0 0 1 4,4 v{BAR - 8} a4,4 0 0 1 -4,4 h-{w - 4:.1f} z" '
                     f'fill="{t["series"][i]}"/>'
                     f'<text x="{LEFT + w + 7:.1f}" y="{y + 11}" font-size="12.5" fill="{t["ink"]}">{value:.1f}%'
                     f'<tspan fill="{t["soft"]}">{extra}</tspan></text>')
    o.append("</svg>")
    return "\n".join(o) + "\n"


if __name__ == "__main__":
    for theme in THEMES:
        (ROOT / f"docs/img/heldout-{theme}.svg").write_text(draw(theme))
        print(f"docs/img/heldout-{theme}.svg")
