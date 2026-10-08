"""补全 tests/data/eval_intent.jsonl 的 note 字段(为什么这条算难题)。

背景:该字段原本 188 条里只有 1 条有值,而文档写了"逐条标注歧义度"——只标了
ambiguous 布尔位,没写**为什么**。空字段比没有字段更糟:它是一个看起来有信息、
实际不可用的信号,而且评估集是给别人看的证据。

本脚本只加 note,不动任何标签、不改样例、不重排字段(仅将 note 追加到行尾)。
用法:python .patch_notes.py
"""
import json
import pathlib

EVALSET = pathlib.Path(__file__).resolve().parent / "tests/data/eval_intent.jsonl"

BOUNDARY = {
    "W11": "无主语('谁的件'),单看像泛问时效;判物流要假设用户在问自己在途的件",
    "W12": "请求式措辞('发我')易被吸到'人工';实为查运单号,归物流",
    "O07": "问的是订单编号(数据)而不是状态,易被判成'人工'代办",
    "O17": "'怎么还是'带抱怨,易被判'投诉';'待发货'又属物流环节;按查询主体归订单",
    "O19": "'能改吗'是可行性问句,像商品咨询;但操作对象是订单,归订单",
    "C09": "问的是'政策是什么',不是'我要修';'保修'关键词易被吸到'售后'",
    "C16": "问的是运费规则(咨询);含'退货'关键词,极易被吸到'退款退货'——与 R20 构成同一易混对的反向样例",
    "R08": "'流程怎么走'是问规则,像商品咨询;但主体是退货动作,归退款退货——与 C09/C16 反向",
    "R10": "同句含'售后'与'退款'两个类目词;按诉求(要退款)定类,不按首个关键词",
    "R20": "'需要什么条件'是问规则,像商品咨询;但主体是申请退货,归退款退货",
    "S10": "无宾语('换什么'),单看像闲聊;靠'换新=售后处置'归类",
    "S14": "疑问句问质量,像商品咨询;隐含不满又像投诉;期望为售后(要处置质量问题)",
    "T14": "无'投诉'字样,是情绪表达,易被当成'要求重新处理'归人工;考以追责意图判投诉",
    "H14": "问的是一个信息(电话),像商品咨询;但问的是人工入口,归人工",
    "H17": "同句含'人工'与'退款';明确要求转人工优先于诉求类目",
    "K07": "是'要求做一件事',像'其他';与 X19 成对,标注'闲聊 vs 其他'的边界",
    "X19": "'帮我写诗'是明确的服务请求,像'人工';但超出客服范畴,归其他——与 K07 成对",
    "X20": "寒暄式提问,像'闲聊';但内容超出客服范畴,归其他",
}

DRIFT = {
    "D01": "历史先'到哪了'(物流)后'想退了'(退款),当前句'它'指代包裹:考'看当前句本身',不是跟着最近一句走",
    "D02": "历史在问价格(商品咨询),当前句转为'能退吗':考新诉求覆盖旧话题",
    "D03": "历史'坏了/看保修'→'要修多久';'多久'是时效词,只看字面会漂到物流",
    "D04": "历史'查订单/已付款'→'发货了吗';'发货'可归订单也可归物流,考上下文下的归属",
    "D05": "历史在问退货运费(规则),当前句落到'我要退这一单'的具体动作",
    "D06": "历史里用户先说'转人工'又说'算了'(干扰项),当前句再次要求建工单:考反悔后重提仍归人工",
    "D07": "历史是服务开场,当前句是寒暄:考上下文不该把闲聊吸走;与无历史的 K02 同句,可对照'有无上下文是否影响判定'",
    "D08": "历史'查订单/已发货'→'什么时候能到';无宾语,靠上下文补全为物流时效",
}

NOTES = {**BOUNDARY, **DRIFT}


def main():
    lines = EVALSET.read_text(encoding="utf-8").splitlines()
    out, added, touched = [], 0, []
    for ln in lines:
        if not ln.strip():
            continue
        d = json.loads(ln)
        note = NOTES.get(d["id"])
        if note:
            d["note"] = note          # 追加到行尾,不改动其它字段与顺序
            added += 1
            touched.append(d["id"])
        out.append(json.dumps(d, ensure_ascii=False))

    EVALSET.write_text("\n".join(out) + "\n", encoding="utf-8")

    print(f"写入 {EVALSET.relative_to(EVALSET.parent.parent)}")
    print(f"  总行数 {len(out)} · 本次补 note {added} 条")
    print(f"  补的 id: {touched}")

    # 复核:不改标签、不改样例、结构完好
    items = [json.loads(l) for l in EVALSET.open(encoding="utf-8") if l.strip()]
    import collections
    print(f"  复核:共 {len(items)} 条 · "
          f"有 note {sum(1 for i in items if i.get('note'))} 条 · "
          f"ambiguous {sum(1 for i in items if i.get('ambiguous'))} 条 · "
          f"drift {sum(1 for i in items if 'drift' in i.get('tags', []))} 条")
    print(f"  九类分布: {dict(sorted(collections.Counter(i['intent'] for i in items).items(), key=lambda kv: -kv[1]))}")
    print(f"  恰好等于 188/26/18/8 则为正确")


if __name__ == "__main__":
    main()
