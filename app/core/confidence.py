"""观测证据置信度(README 四信号):量化「这批证据到底符不符合用户问题」。

信号(全部来自检索/精排结果,零额外模型调用):
  top1_score     精排 Top1 的 rerank_score(证据里最强的一条有多强)
  valid_count    有效证据数:rerank_score >= VALID_SCORE_FLOOR 的条数(强证据是孤证还是成群)
  margin         Top1 - Top2 分差(证据聚焦度:断崖式领先 vs 一堆似是而非;单条时取 top1 自身)
  key_clause_hit Top3 文本是否命中关键条款词表(复用 知识库入库同款 KEY_TERMS 启发式——
                 hit 里没有 is_key_clause 字段,按同词表现算,口径与入库标记一致)

组合:固定权重线性加权归一到 0-1。权重是模块常量不进 settings——评估集校准的是
阈值(settings.evidence_confidence_threshold),不是权重(YAGNI,别造两层可调)。

2026-09-22 修正:第 4 个信号 key_clause_hit 的权重由 0.1 置 0(信号仍计算、仍落 trace,
只是不参与打分)。依据是消融实验(.exp_signal.py → data/flywheel/reports/signal_replacement.txt):
该信号与目标是**反相关**的——单信号 AUC 仅 0.3083(小于 0.5,即它把"应拒"排在"可答"前面),
应拒桶的词表命中率 96.7% 反而高于可答桶 58.3%(那张词表是通用电商政策词,而问"库外知识"的
问题恰恰更爱用它们);5 折留出评估下去掉它 J 由 0.8833 升到 0.8889,而四个候选替代信号
在留出折上全部更差。权重和因此变为 0.9,分数上界降到 0.9,阈值已随之重新校准。
"""
from dataclasses import dataclass

from app.kb.documents import KEY_TERMS

VALID_SCORE_FLOOR = 0.3   # 有效证据线(沿用 RAG rerank_min_score 的经验位)
VALID_COUNT_CAP = 3       # 有效证据数归一封顶:3 条及以上算满分
# W_KEY 由 0.1 置 0:见文件头"2026-09-22 修正"。信号仍计算(测试与 trace 依赖),
# 但不再参与打分;权重和因此是 0.9 而非 1.0,阈值另行校准。
W_TOP1, W_VALID, W_MARGIN, W_KEY = 0.5, 0.2, 0.2, 0.0


@dataclass
class EvidenceConfidence:
    score: float    # 0-1 总分
    signals: dict   # 各原始信号,落池 reason 与 trace 留痕用


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_evidence_confidence(hits: list[dict]) -> EvidenceConfidence:
    if not hits:
        return EvidenceConfidence(0.0, {"top1_score": 0.0, "valid_count": 0,
                                        "margin": 0.0, "key_clause_hit": False})
    scores = [float(h.get("rerank_score", 0.0)) for h in hits]
    top1 = scores[0]
    margin = top1 - scores[1] if len(scores) > 1 else top1
    valid_count = sum(1 for s in scores if s >= VALID_SCORE_FLOOR)
    key_hit = any(
        any(t in f"{h.get('question', '')}{h.get('answer', '')}" for t in KEY_TERMS)
        for h in hits[:3]
    )
    score = (W_TOP1 * _clip01(top1)
             + W_VALID * min(valid_count, VALID_COUNT_CAP) / VALID_COUNT_CAP
             + W_MARGIN * _clip01(margin)
             + W_KEY * (1.0 if key_hit else 0.0))
    return EvidenceConfidence(round(_clip01(score), 4),
                              {"top1_score": top1, "valid_count": valid_count,
                               "margin": round(margin, 4), "key_clause_hit": key_hit})


def snapshot_from_hits(hits: list[dict], top_n: int = 3) -> list[dict]:
    """落池召回快照的统一出口:Top N 的原文与得分(审核页给人看的那份)。"""
    return [{"question": h.get("question", ""), "answer": h.get("answer", ""),
             "rerank_score": float(h.get("rerank_score", 0.0)),
             "section_path": h.get("section_path", "")}
            for h in hits[:top_n]]
