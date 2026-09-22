"""R07: re-split the contemporary set into dev and test only (in place).

Annotation only: every sample keeps its rows, its news_quality, its order;
only the `split` field changes, and dev.jsonl / test.jsonl / manifest.json
are rewritten. train.jsonl is removed. The rule is build_r04_assemble's
assign_splits: per class, chronological by window.end, the earlier 60% dev.

Usage: python projects/credit_risk/dataset/builders/build_r07_resplit.py
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_r04_assemble import DEV_SHARE, SPLITS, assign_splits  # noqa: E402

CONTEMPORARY = Path(__file__).resolve().parent.parent / "contemporary"


def main() -> int:
    path = CONTEMPORARY / "samples.jsonl"
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    samples = [json.loads(l) for l in lines]
    before = Counter((s["split"], s["type"]) for s in samples)
    assign_splits(samples)
    assert len(samples) == len(lines)
    path.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples), encoding="utf-8")
    for split in SPLITS:
        (CONTEMPORARY / f"{split}.jsonl").write_text(
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples if s["split"] == split),
            encoding="utf-8")
    stale = CONTEMPORARY / "train.jsonl"
    if stale.exists():
        stale.unlink()
    after = Counter((s["split"], s["type"]) for s in samples)
    manifest_path = CONTEMPORARY / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["splits"] = {f"{t[:3]}_{sp}": n for (sp, t), n in sorted(after.items())}
    manifest["split_rule"] = (f"per class, chronological by window.end; earliest {DEV_SHARE:.0%} dev, "
                              f"rest test; no train split (R07)")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("before:", dict(sorted(before.items())))
    print("after: ", dict(sorted(after.items())))
    for split in SPLITS:
        ends = [s["window"]["end"] for s in samples if s["split"] == split]
        print(f"{split}: {len(ends)} samples, window.end {min(ends)} .. {max(ends)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
