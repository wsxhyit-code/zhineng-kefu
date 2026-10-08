import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.core import llm
from app.core.llm import get_chat_model
from app.core.prompts import INTENT_CLASSIFY_PROMPT

logger = logging.getLogger(__name__)

INTENTS = ("物流", "订单", "商品咨询", "退款退货", "售后", "投诉", "人工", "闲聊", "其他")


class _Intent(BaseModel):
    intent: Literal["物流", "订单", "商品咨询", "退款退货", "售后", "投诉", "人工", "闲聊", "其他"] = Field(
        description="九类意图之一")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="判断把握 0-1")


async def classify(query: str, history: str = "") -> dict:
    """九类意图 + confidence(工具系统增「人工」:明确要求建工单/转人工)。扁平字段避开 glm 嵌套 502;
    解析失败/越界 → 归「其他」(最保守兜底,替换 工作流的回退闲聊)。
    模型默认走 settings.intent_model(空则回落 chat_model)——先求准。"""
    model = llm.structured(_Intent, model=settings.intent_model or None)
    try:
        r: _Intent = await (INTENT_CLASSIFY_PROMPT | model).ainvoke(
            {"query": query, "history": history or "(无)"})
    except Exception as e:
        # 兜底返回与上游故障共用同一个签名(其他+0.0),离线评估无法区分"模型判了其他"和
        # "调用没成功"。2026-09-22 实测:402 余额不足时 188/188 全部走这条路径,评估脚本
        # 差点把它读成"模型准确率 10.6%"。所以这里必须留下日志,否则故障在线上完全不可见。
        logger.warning("意图分类调用失败,兜底为'其他':%s: %s", type(e).__name__, e)
        return {"intent": "其他", "confidence": 0.0}
    intent = r.intent if r.intent in INTENTS else "其他"
    return {"intent": intent, "confidence": float(r.confidence)}
