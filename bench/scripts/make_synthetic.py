"""Generate the fictional demo dataset in bench/data/synthetic (fixed seed, byte-for-byte reproducible).

Everything is invented: fictional peers (peer-A, peer-B, peer-C), a fictional workspace, hobby-themed "conclusions"
and queries. No real conversation, person or deployment is involved. The dataset exists so the whole pipeline can
run offline; it is NOT the data behind any published number.

How the ground truth works
* A conclusion has a topic (one of twelve hobbies) and a facet (one of four aspects of that hobby).
* A call asks about one (topic, facet) of one peer pair. For that call every conclusion of the pair is graded
  2 if topic and facet match, 1 if only the topic matches, 0 otherwise. ``relevance.jsonl`` lists the grades above 0.
* Embeddings (16 dimensions) are topic centroid + a weak facet offset + noise, so vector search finds the right
  topic but mixes up facets; the query text names its facet, which a word-overlap reranker can exploit. That is by
  construction: the demo shows the machinery, it does not show how a real reranker behaves on real memory.

Files: corpus.jsonl, calls.jsonl, embeddings.jsonl, relevance.jsonl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import write_jsonl  # noqa: E402
from retrieval import embedding_key  # noqa: E402

SEED = 20260101
DIM = 16
MODEL = "synthetic-16"
WORKSPACE = "workspace-1"
TOPICS = {
    "gardening": ["tomatoes", "compost", "irrigation", "seedlings"],
    "cycling": ["gravel", "helmet", "cadence", "punctures"],
    "sourdough": ["starter", "hydration", "proofing", "scoring"],
    "chess": ["openings", "endgames", "clocks", "tournaments"],
    "astronomy": ["telescope", "nebulae", "eclipses", "charts"],
    "knitting": ["wool", "cables", "gauge", "scarves"],
    "hiking": ["ridges", "boots", "maps", "bivouac"],
    "jazz": ["trumpet", "improvisation", "standards", "vinyl"],
    "coffee": ["espresso", "grinder", "roast", "pourover"],
    "woodworking": ["chisels", "dovetails", "oak", "finishing"],
    "birdwatching": ["warblers", "binoculars", "migration", "feeders"],
    "pottery": ["kiln", "glaze", "wheel", "stoneware"],
}
FILLER = ["lately", "again", "twice", "slowly", "carefully", "early", "weekly", "gladly", "quietly", "mostly"]
VERBS = ["keeps notes on", "talks about", "is learning about", "often comes back to", "wrote down thoughts on"]
LEVELS = [("explicit", 0.50), ("deductive", 0.30), ("inductive", 0.15), ("contradiction", 0.05)]
# (observer, observed, number of conclusions, share of calls). A large, a medium and a small peer pair.
PAIRS = [("peer-A", "peer-A", 360, 0.60), ("peer-B", "peer-B", 150, 0.25), ("peer-C", "peer-A", 60, 0.15)]
CALLERS = [  # (caller_type, calls, top_k choices, calls per run or session)
    ("dream.deduction", 30, (10, 20), 3),
    ("dream.induction", 30, (10, 20), 3),
    ("dialectic", 15, (10, 25), 3),
    ("dialectic_prefetch", 15, (10, 25), 3),
]
TEMPLATES = {
    "dream.deduction": "What does {subject} imply about {facet} in {topic}?",
    "dream.induction": "Which evidence shows how {subject} approaches {facet} in {topic}?",
    "dialectic": "What does {subject} say about {facet} when it comes to {topic}?",
    "dialectic_prefetch": "{subject} {facet} {topic}",
}


def _unit(rng: random.Random, scale: float = 1.0) -> list[float]:
    v = [rng.gauss(0, 1) for _ in range(DIM)]
    norm = math.sqrt(sum(x * x for x in v))
    return [scale * x / norm for x in v]


def _round(vector: list[float]) -> list[float]:
    return [round(x, 4) for x in vector]


def _pick_level(rng: random.Random) -> str:
    r, acc = rng.random(), 0.0
    for level, weight in LEVELS:
        acc += weight
        if r < acc:
            return level
    return LEVELS[-1][0]


def generate(seed: int = SEED) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    centroid = {t: _unit(rng) for t in TOPICS}
    offset = {(t, f): _unit(rng, 0.8) for t, facets in TOPICS.items() for f in facets}

    corpus: list[dict] = []
    serial = 0
    for observer, observed, size, _share in PAIRS:
        for _ in range(size):
            serial += 1
            topic = rng.choice(sorted(TOPICS))
            facet = rng.choice(TOPICS[topic])
            noise = _unit(rng, 0.75)
            vec = [c + o + n for c, o, n in zip(centroid[topic], offset[(topic, facet)], noise)]
            content = (f"{observed} {rng.choice(VERBS)} {facet} for {topic}, {rng.choice(FILLER)} "
                       f"(ref {serial:04d}).")
            corpus.append(dict(id=f"doc-{serial:04d}", workspace_name=WORKSPACE, observer=observer, observed=observed,
                               level=_pick_level(rng), content=content, embedding=_round(vec), topic=topic, facet=facet))

    # Within one caller every call asks about a distinct (pair, topic, facet), so query texts are unique.
    calls, vectors, relevance = [], [], []
    n = 0
    for caller, count, top_ks, group in CALLERS:
        available = {i: [(t, f) for t in sorted(TOPICS) for f in TOPICS[t]] for i in range(len(PAIRS))}
        for choices in available.values():
            rng.shuffle(choices)
        for j in range(count):
            pair_i = rng.choices(range(len(PAIRS)), weights=[p[3] for p in PAIRS])[0]
            if not available[pair_i]:
                pair_i = next(i for i in available if available[i])
            topic, facet = available[pair_i].pop()
            observer, observed, _size, _share = PAIRS[pair_i]
            n += 1
            call_id = f"call-{n:03d}"
            query = TEMPLATES[caller].format(subject=observed, facet=facet, topic=topic) + f" (asked by {observer})"
            group_id = j // group
            call = dict(call_id=call_id, caller_type=caller, query=query, top_k=top_ks[j % 2], workspace_name=WORKSPACE,
                        observer=observer, observed=observed, filters=None, max_distance=None,
                        replay_eligible=True, scope_approximated=caller.startswith("dialectic"),
                        run_id=f"run-{caller.split('.')[-1][:3]}-{group_id:02d}" if caller.startswith("dream") else None,
                        session_id=None if caller.startswith("dream") else f"session-{pair_i}-{group_id % 2}")
            if caller == "dialectic_prefetch":
                call["filters"] = {"level": {"in": ["explicit"]}}
            calls.append(call)
            qvec = [c + 0.35 * o + q for c, o, q in zip(centroid[topic], offset[(topic, facet)], _unit(rng, 0.5))]
            vectors.append(dict(cache_key=embedding_key(MODEL, DIM, query), embedding=_round(qvec), model=MODEL))
            for doc in corpus:
                if doc["observer"] == observer and doc["observed"] == observed and doc["topic"] == topic:
                    relevance.append(dict(call_id=call_id, document_id=doc["id"], grade=2 if doc["facet"] == facet else 1))
    return dict(corpus=corpus, calls=calls, embeddings=vectors, relevance=relevance)


def write_all(out_dir: Path, seed: int = SEED) -> dict[str, str]:
    data = generate(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    digests = {}
    for name, rows in data.items():
        path = out_dir / f"{name}.jsonl"
        write_jsonl(path, rows)
        digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "synthetic")
    p.add_argument("--seed", type=int, default=SEED)
    args = p.parse_args(argv)
    print(json.dumps(write_all(args.out_dir, args.seed), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
