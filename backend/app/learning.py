"""Feedback learning: transparent idea ranking + controlled experiments.

Ranking is a weighted sum of the research agent's 0-10 criterion ratings, nudged by the latest
evidence-backed strategy adjustments. Experiments assign arms round-robin and are only concluded when
every arm has >= MIN_PER_ARM videos with 7-day data, using a two-sided permutation test on average
view percentage (a retention/satisfaction proxy) — never on a single video."""
from __future__ import annotations

import random
import statistics

from sqlalchemy.orm import Session

from app.enums import VideoFormat
from app.media.qa import text_similarity
from app.models import (AnalyticsSnapshot, ContentIdea, PublishedVideo, Story, StrategyExperiment, VideoProject,
                        YoutubeChannel)
from app.reliability.errors import PermanentError

WEIGHTS = {"topic_relevance": 1.2, "audience_fit": 1.3, "originality": 1.2, "historical_performance": 0.8,
           "production_feasibility": 1.0, "cost_efficiency": 0.6, "duplication_risk": -1.0}
MIN_PER_ARM = 5


def score_idea(idea: ContentIdea, adjustments: dict) -> tuple[float, dict]:
    s = idea.scores
    base = sum(WEIGHTS[k] * float(s.get(k, 5)) for k in WEIGHTS) / sum(abs(w) for w in WEIGHTS.values())
    text = f"{idea.title} {idea.title_en or ''} {idea.logline}".lower()
    bonus = 0.0
    for kw in adjustments.get("preferred_topics", []) or []:
        if str(kw).lower() in text:
            bonus += 0.4
    for kw in adjustments.get("avoid_topics", []) or []:
        if str(kw).lower() in text:
            bonus -= 0.8
    return round(base + bonus, 3), {"base": round(base, 3), "strategy_bonus": round(bonus, 2)}


def latest_adjustments(db: Session, channel: YoutubeChannel) -> dict:
    r = (db.query(StrategyExperiment).filter_by(channel_id=channel.id, kind="review")
         .filter(StrategyExperiment.confidence.in_(["medium", "high"])).order_by(StrategyExperiment.created_at.desc()).first())
    return r.adjustment if r else {}


def select_idea(db: Session, channel: YoutubeChannel, p: VideoProject) -> tuple[ContentIdea, list[dict]]:
    adj = latest_adjustments(db, channel)
    ideas = (db.query(ContentIdea).filter_by(channel_id=channel.id, format=p.format, status="NEW")
             .order_by(ContentIdea.created_at.desc()).limit(40).all())
    past = [f"{s.title} {s.script.get('synopsis_ta', '')}" for s in db.query(Story).filter_by(channel_id=channel.id).limit(300)]
    ranking = []
    for i in ideas:
        sc, parts = score_idea(i, adj)
        dup = max([text_similarity(f"{i.title} {i.logline}", x) for x in past] or [0.0])
        if dup > 0.5:
            i.status = "REJECTED"
            continue
        i.total_score = sc
        ranking.append({"id": str(i.id), "title": i.title, "score": sc, **parts, "history_similarity": round(dup, 3)})
    if not ranking:
        raise PermanentError("no eligible ideas (all used, rejected, or none researched)")
    ranking.sort(key=lambda r: -r["score"])
    best = next(i for i in ideas if str(i.id) == ranking[0]["id"])
    best.status = "SELECTED"
    assign_arms(db, channel, p)
    return best, ranking


# ---------------- experiments ----------------
DEFAULT_EXPERIMENTS = [
    {"variable": "short_target_seconds", "format": "SHORT", "arms": {"A": 40, "B": 55},
     "hypothesis": "Shorter Shorts (~40s) retain a higher share of viewers than ~55s Shorts"},
    {"variable": "thumbnail_layout", "format": "LONG", "arms": {"A": "bottom", "B": "left"},
     "hypothesis": "Thumbnail text placement affects long-form average view percentage"},
]


def ensure_experiments(db: Session, channel: YoutubeChannel):
    for e in DEFAULT_EXPERIMENTS:
        if not db.query(StrategyExperiment).filter_by(channel_id=channel.id, kind="experiment", variable=e["variable"]).first():
            db.add(StrategyExperiment(channel_id=channel.id, kind="experiment", variable=e["variable"], hypothesis=e["hypothesis"],
                                      arms={"values": e["arms"], "format": e["format"], "assigned": {"A": 0, "B": 0}},
                                      status="OPEN"))
    db.flush()


def assign_arms(db: Session, channel: YoutubeChannel, p: VideoProject):
    ensure_experiments(db, channel)
    arms = {}
    for e in db.query(StrategyExperiment).filter_by(channel_id=channel.id, kind="experiment", status="OPEN"):
        if e.arms.get("format") != p.format.value:
            continue
        assigned = e.arms.get("assigned", {"A": 0, "B": 0})
        arm = "A" if assigned.get("A", 0) <= assigned.get("B", 0) else "B"
        assigned[arm] = assigned.get(arm, 0) + 1
        e.arms = {**e.arms, "assigned": assigned}
        arms[e.variable] = {"arm": arm, "value": e.arms["values"][arm], "experiment_id": str(e.id)}
    p.metadata_ = {**(p.metadata_ or {}), "experiment_arms": arms}


def permutation_p(a: list[float], b: list[float], iters: int = 5000, seed: int = 7) -> float:
    rng = random.Random(seed)
    obs = abs(statistics.mean(a) - statistics.mean(b))
    pool = a + b
    hits = 0
    for _ in range(iters):
        rng.shuffle(pool)
        if abs(statistics.mean(pool[:len(a)]) - statistics.mean(pool[len(a):])) >= obs:
            hits += 1
    return (hits + 1) / (iters + 1)


def evaluate_experiments(db: Session, channel: YoutubeChannel) -> list[dict]:
    results = []
    rows = (db.query(PublishedVideo, AnalyticsSnapshot).join(AnalyticsSnapshot, AnalyticsSnapshot.published_video_id == PublishedVideo.id)
            .filter(PublishedVideo.channel_id == channel.id, AnalyticsSnapshot.window == "7d").all())
    for e in db.query(StrategyExperiment).filter_by(channel_id=channel.id, kind="experiment", status="OPEN"):
        data = {"A": [], "B": []}
        for pv, snap in rows:
            arm = (pv.features.get("experiment_arms") or {}).get(e.variable, {}).get("arm")
            v = snap.metrics.get("averageViewPercentage")
            if arm in data and v is not None:
                data[arm].append(float(v))
        n = min(len(data["A"]), len(data["B"]))
        e.sample_size = len(data["A"]) + len(data["B"])
        ev = {"n_A": len(data["A"]), "n_B": len(data["B"]), "metric": "averageViewPercentage (7d)"}
        if n >= MIN_PER_ARM:
            p = permutation_p(data["A"], data["B"])
            ev.update(mean_A=round(statistics.mean(data["A"]), 2), mean_B=round(statistics.mean(data["B"]), 2), p_value=round(p, 4))
            e.confidence = "high" if p < 0.01 else "medium" if p < 0.05 else "low"
            if p < 0.05:
                winner = "A" if ev["mean_A"] > ev["mean_B"] else "B"
                e.status = "CONCLUDED"
                e.adjustment = {e.variable: e.arms["values"][winner], "winner": winner}
        else:
            e.confidence = "insufficient"
        e.evidence = ev
        results.append({"variable": e.variable, **ev, "confidence": e.confidence, "status": e.status})
    db.flush()
    return results


def experiment_value(p: VideoProject, variable: str, default=None):
    return ((p.metadata_ or {}).get("experiment_arms", {}).get(variable) or {}).get("value", default)


_ = VideoFormat
