    const SESSION_KEY = "mewhelp_session_id";      // 稳定用户标识,作 user_id
    const CONV_KEY = "mewhelp_conversation_id";     // 当前会话 id,由后端 done 帧回传
    const SUGGESTIONS = ["退换货政策是怎样的？", "订单 1001 的物流到哪了？", "猫粮怎么选？", "会员有什么权益？"];
    const CAT_SVG = '<span class="agent-symbol" aria-hidden="true">✦</span>';
    // 点赞/点踩大拇指(矢量填充,fill=currentColor:平时 ink、点亮时随按钮变白)
    const THUMB_UP_SVG =
      '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/></svg>';
    const THUMB_DOWN_SVG =
      '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/></svg>';
    const messagesEl = document.getElementById("messages");
    const inputEl = document.getElementById("input");
    const sendBtn = document.getElementById("send");
    const newChatBtn = document.getElementById("new-chat");

    for (const el of document.querySelectorAll("[data-cat]")) el.innerHTML = CAT_SVG;

    function getUserId() {
      let id = localStorage.getItem(SESSION_KEY);
      if (!id) {
        id = crypto.randomUUID();
        localStorage.setItem(SESSION_KEY, id);
      }
      return id;
    }

    function getConversationId() {
      const v = localStorage.getItem(CONV_KEY);
      return v ? Number(v) : null;
    }
    function setConversationId(id) {
      if (id != null) localStorage.setItem(CONV_KEY, String(id));
      updateSessionLabel();
    }
    function clearConversationId() {
      localStorage.removeItem(CONV_KEY);
      updateSessionLabel();
    }

    function renderEmptyState() {
      messagesEl.innerHTML = "";
      const wrap = document.createElement("div");
      wrap.className = "empty";
      wrap.innerHTML =
        '<div class="mascot">' + CAT_SVG + "</div>" +
        '<span class="welcome-eyebrow">YOUR SERVICE COPILOT</span>' +
        "<h1>每一次提问，都有据可依</h1>" +
        "<p>查订单、问政策、处理售后。<br>从一个问题开始，体验完整的智能服务流程。</p>" +
        '<div class="chips"></div>';
      const chips = wrap.querySelector(".chips");
      for (const s of SUGGESTIONS) {
        const c = document.createElement("button");
        c.className = "chip";
        c.type = "button";
        c.textContent = s;
        c.addEventListener("click", () => { if (!sendBtn.disabled) send(s); });
        chips.appendChild(c);
      }
      messagesEl.appendChild(wrap);
    }

    function clearEmptyState() {
      const e = messagesEl.querySelector(".empty");
      if (e) e.remove();
    }

    function addRow(role) {
      clearEmptyState();
      const row = document.createElement("div");
      row.className = "row " + role;
      if (role === "bot") {
        const av = document.createElement("div");
        av.className = "avatar sm";
        av.innerHTML = CAT_SVG;
        row.appendChild(av);
      }
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      row.appendChild(bubble);
      messagesEl.appendChild(row);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      return bubble;
    }

    function showTyping(bubble) {
      bubble.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    }

    function setBusy(busy) {
      newChatBtn.disabled = busy;
      sendBtn.disabled = busy;
      inputEl.disabled = busy;
      if (!busy) inputEl.focus();
    }

    // ---------- 轻量 markdown 渲染(零依赖,先转义再解析,防注入) ----------
    function escapeHtml(s) {
      return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    }
    function renderInline(s) {
      s = s.replace(/`([^`]+)`/g, (m, c) => "<code>" + c + "</code>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
      // 仅允许 http(s) 链接,挡 javascript: 等
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>');
      return s;
    }
    function renderMarkdown(md) {
      const lines = escapeHtml(md).split("\n");
      const out = [];
      const isTableSep = (l) => l && l.includes("-") && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(l);
      const splitRow = (l) => l.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
      const isSpecial = (l, next) =>
        /^```/.test(l) || /^(#{1,6})\s/.test(l) || /^\s*[-*+]\s+/.test(l) ||
        /^\s*\d+\.\s+/.test(l) || /^\s*(---|\*\*\*|___)\s*$/.test(l) || /^\s*>\s?/.test(l) ||
        (l.includes("|") && isTableSep(next));
      let i = 0;
      while (i < lines.length) {
        const line = lines[i];
        let m;
        if (/^```/.test(line)) {                                   // 代码块
          const buf = []; i++;
          while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
          i++;
          out.push("<pre><code>" + buf.join("\n") + "</code></pre>"); continue;
        }
        if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {               // 标题
          const n = m[1].length; out.push(`<h${n}>` + renderInline(m[2]) + `</h${n}>`); i++; continue;
        }
        if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) { out.push("<hr>"); i++; continue; }  // 分隔线
        if (line.includes("|") && isTableSep(lines[i + 1])) {      // 表格
          const headers = splitRow(line); i += 2; const rows = [];
          while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
            rows.push(splitRow(lines[i])); i++;
          }
          let t = "<table><thead><tr>" + headers.map((c) => "<th>" + renderInline(c) + "</th>").join("") + "</tr></thead><tbody>";
          for (const r of rows) t += "<tr>" + r.map((c) => "<td>" + renderInline(c) + "</td>").join("") + "</tr>";
          out.push(t + "</tbody></table>"); continue;
        }
        if (/^\s*[-*+]\s+/.test(line)) {                           // 无序列表
          const items = [];
          while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
            items.push("<li>" + renderInline(lines[i].replace(/^\s*[-*+]\s+/, "")) + "</li>"); i++;
          }
          out.push("<ul>" + items.join("") + "</ul>"); continue;
        }
        if (/^\s*\d+\.\s+/.test(line)) {                           // 有序列表
          const items = [];
          while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
            items.push("<li>" + renderInline(lines[i].replace(/^\s*\d+\.\s+/, "")) + "</li>"); i++;
          }
          out.push("<ol>" + items.join("") + "</ol>"); continue;
        }
        if (/^\s*>\s?/.test(line)) {                               // 引用
          const buf = [];
          while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
          out.push("<blockquote>" + buf.map(renderInline).join("<br>") + "</blockquote>"); continue;
        }
        if (line.trim() === "") { i++; continue; }                 // 空行
        const para = [];                                           // 段落
        while (i < lines.length && lines[i].trim() !== "" && !isSpecial(lines[i], lines[i + 1])) {
          para.push(lines[i]); i++;
        }
        out.push("<p>" + para.map(renderInline).join("<br>") + "</p>");
      }
      return out.join("");
    }

    // ---------- RAG:引用角标 [n] → 可点,弹来源浮层(section_path + 原文) ----------
    let citePop = null;
    function ensureCitePop() {
      if (citePop) return citePop;
      citePop = document.createElement("div");
      citePop.className = "cite-pop";
      citePop.hidden = true;
      document.body.appendChild(citePop);
      document.addEventListener("click", (e) => {
        if (citePop.hidden) return;
        if (!citePop.contains(e.target) && !e.target.classList.contains("cite")) hideCitePop();
      });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideCitePop(); });
      window.addEventListener("resize", hideCitePop);
      return citePop;
    }
    function hideCitePop() { if (citePop) citePop.hidden = true; }
    function showCitePop(sup, c) {
      const pop = ensureCitePop();
      pop.innerHTML =
        '<div class="cite-pop-path">' + escapeHtml(c.section_path || "来源") + "</div>" +
        (c.question ? '<div class="cite-pop-q">' + escapeHtml(c.question) + "</div>" : "") +
        '<div class="cite-pop-a">' + escapeHtml(c.answer || "") + "</div>" +
        '<div class="cite-pop-meta">来源编号 [' + c.n + "]" +
        (c.content_type ? " · " + escapeHtml(c.content_type) : "") + "</div>";
      pop.hidden = false;
      // 定位:角标下方,超出视口右/下边则回收
      const r = sup.getBoundingClientRect();
      const pw = Math.min(320, window.innerWidth - 24);
      pop.style.width = pw + "px";
      let left = Math.min(r.left, window.innerWidth - pw - 12);
      let top = r.bottom + 6;
      if (top + pop.offsetHeight > window.innerHeight - 12) top = Math.max(12, r.top - pop.offsetHeight - 6);
      pop.style.left = Math.max(12, left) + "px";
      pop.style.top = top + "px";
    }
    function decorateCitations(container, citations) {
      const byN = new Map(citations.map((c) => [String(c.n), c]));
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
      const targets = [];
      let node;
      while ((node = walker.nextNode())) {
        if (node.parentElement.classList.contains("cite")) continue;
        if (/\[\d+\]/.test(node.nodeValue)) targets.push(node);
      }
      for (const textNode of targets) {
        const frag = document.createDocumentFragment();
        let last = 0;
        const re = /\[(\d+)\]/g;
        let m;
        while ((m = re.exec(textNode.nodeValue))) {
          const c = byN.get(m[1]);
          if (!c) continue;  // 非有效编号,保留原文
          if (m.index > last) frag.appendChild(document.createTextNode(textNode.nodeValue.slice(last, m.index)));
          const sup = document.createElement("sup");
          sup.className = "cite";
          sup.textContent = "[" + m[1] + "]";
          sup.title = c.section_path || "查看来源";
          sup.addEventListener("click", (e) => { e.stopPropagation(); showCitePop(sup, c); });
          frag.appendChild(sup);
          last = m.index + m[0].length;
        }
        if (last > 0) {
          if (last < textNode.nodeValue.length) frag.appendChild(document.createTextNode(textNode.nodeValue.slice(last)));
          textNode.parentNode.replaceChild(frag, textNode);
        }
      }
    }

    // ---------- RAG:每段回答满意度反馈(👍/👎,点一次点亮 + 已反馈,一次性)----------
    function addFeedbackBar(bubble) {
      if (bubble.querySelector(".feedback")) return;   // 防重复挂
      const bar = document.createElement("div");
      bar.className = "feedback";
      const up = document.createElement("button");
      up.type = "button"; up.className = "fb-btn"; up.innerHTML = THUMB_UP_SVG;
      up.setAttribute("aria-label", "这条回答有帮助");
      const down = document.createElement("button");
      down.type = "button"; down.className = "fb-btn"; down.innerHTML = THUMB_DOWN_SVG;
      down.setAttribute("aria-label", "这条回答没帮助");
      const done = document.createElement("span");
      done.className = "fb-done"; done.hidden = true; done.textContent = "反馈已记录";
      bar.append(up, down, done);
      // 观测:向上找这条回答之前最近的用户气泡,把该轮用户原话带给后端(👎 落池要用)
      function userQuestionBefore() {
        let row = bubble.closest(".row");
        while (row && (row = row.previousElementSibling)) {
          if (row.classList && row.classList.contains("user")) {
            const b = row.querySelector(".bubble");
            return (b ? b.textContent : row.textContent).trim();
          }
        }
        return "";
      }
      function give(chosen, other) {
        if (bar.dataset.given) return;   // 一次性,点过不再变
        bar.dataset.given = "1";
        chosen.classList.add("active");  // 点亮选中的
        other.classList.add("dim");      // 另一个淡出
        up.disabled = true; down.disabled = true;
        done.hidden = false;             // 显示"已反馈"
        // 观测:反馈接后端——👎 落低置信度问题池进飞轮,👍 后端只记日志;失败静默,UI 行为不变
        fetch("/api/feedback", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conversation_id: getConversationId(),
                                 rating: chosen === down ? "down" : "up",
                                 question: userQuestionBefore() }),
        }).catch(() => {});
      }
      up.addEventListener("click", () => give(up, down));
      down.addEventListener("click", () => give(down, up));
      bubble.appendChild(bar);
    }

    // ---------- 工作流:actions 帧 - 转人工/建工单两按钮交互 ----------
    function renderActions(bubble, items) {
      const bar = document.createElement("div");
      bar.className = "action-bar";
      for (const a of items) {
        if (a.type === "select_order") {
          // 工具系统:报了不属于自己的订单号被拒,把他名下的单列出来点选。拒绝之后得给条出路,
          // 否则用户既查不到,也不知道自己的单号是多少
          renderOrderCards(bubble, a.orders || [], pickThenAsk);
          continue;
        }
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "action-btn";
        if (a.type === "transfer_human") {
          btn.textContent = "转人工";
          btn.addEventListener("click", () => {
            btn.disabled = true;
            // 纯前端模拟:显示已转接 + 客服小猫问候(本模块不接真人系统)
            const sys = addRow("bot"); sys.textContent = "人工转接演示已启动（当前未接入真人坐席）";
            const greet = addRow("bot");
            greet.textContent = "您好，这里是人工客服演示。您也可以创建工单，登记需要跟进的问题。";
            messagesEl.scrollTop = messagesEl.scrollHeight;
          });
        } else if (a.type === "create_ticket") {
          btn.textContent = "建工单";
          // 点「建工单」不再直接建单,而是弹出工单表单让用户选类别+填描述再提交
          btn.addEventListener("click", () => openTicketModal(btn));
        } else if (a.type === "refund_form") {
          btn.textContent = "提交退款工单";
          // 意图:能退时 Agent 调 submit_refund 被拦成此动作,点开退款表单(订单号只读+原因下拉)
          btn.addEventListener("click", () => openRefundModal(btn, a.draft || {}));
        }
        bar.appendChild(btn);
      }
      if (bar.children.length) bubble.appendChild(bar);   // 只有订单卡时不留一条空按钮栏
    }

    // ---------- 工作流:建工单表单弹窗控制 ----------
    const ticketMask = document.getElementById("ticketMask");
    const ticketType = document.getElementById("ticketType");
    const ticketDesc = document.getElementById("ticketDesc");
    const ticketErr = document.getElementById("ticketErr");
    const ticketSubmit = document.getElementById("ticketSubmit");
    const ticketCancel = document.getElementById("ticketCancel");
    let ticketTriggerBtn = null;  // 记住是哪个「建工单」按钮触发的,成功后置灰它

    function openTicketModal(triggerBtn) {
      ticketTriggerBtn = triggerBtn;
      ticketType.value = "";          // 不预选,强制用户选
      ticketDesc.value = "";          // 留空,用户自己写
      ticketErr.textContent = "";
      ticketSubmit.disabled = false;
      ticketSubmit.textContent = "提交工单";
      ticketMask.classList.add("open");
      ticketDesc.focus();
    }
    function closeTicketModal() {
      ticketMask.classList.remove("open");
      ticketTriggerBtn = null;
    }

    ticketCancel.addEventListener("click", closeTicketModal);
    // 点遮罩空白处 / Esc 关闭(取消不建单,建工单按钮保持可用可重开)
    ticketMask.addEventListener("click", (e) => { if (e.target === ticketMask) closeTicketModal(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && ticketMask.classList.contains("open")) closeTicketModal();
    });

    ticketSubmit.addEventListener("click", async () => {
      const type = ticketType.value;
      const desc = ticketDesc.value.trim();
      if (!type) { ticketErr.textContent = "请选择反馈类别"; return; }
      if (!desc) { ticketErr.textContent = "请填写反馈描述"; ticketDesc.focus(); return; }
      ticketErr.textContent = "";
      ticketSubmit.disabled = true;
      ticketSubmit.textContent = "提交中…";
      try {
        const resp = await fetch("/api/actions/create-ticket", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            conversation_id: getConversationId(),
            description: desc,
            ticket_type: type,
          }),
        });
        const data = await resp.json();
        if (resp.ok) {
          if (ticketTriggerBtn) ticketTriggerBtn.disabled = true;  // 建成后该气泡的按钮置灰
          closeTicketModal();
          addRow("bot").textContent = "工单已创建:" + data.ticket_no;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else {
          ticketErr.textContent = "工单创建失败,请稍后重试";
          ticketSubmit.disabled = false;
          ticketSubmit.textContent = "提交工单";
        }
      } catch (e) {
        ticketErr.textContent = "工单创建失败,请稍后重试";
        ticketSubmit.disabled = false;
        ticketSubmit.textContent = "提交工单";
      }
    });

    // ---------- 意图:退款表单弹窗控制(订单号只读 + 固定类目原因下拉)----------
    const refundMask = document.getElementById("refundMask");
    const refundOrder = document.getElementById("refundOrder");
    const refundReason = document.getElementById("refundReason");
    const refundErr = document.getElementById("refundErr");
    const refundSubmit = document.getElementById("refundSubmit");
    const refundCancel = document.getElementById("refundCancel");
    let refundTriggerBtn = null;

    function openRefundModal(triggerBtn, draft) {
      refundTriggerBtn = triggerBtn;
      refundOrder.value = (draft && draft.order_id) || "";
      refundReason.value = "";
      refundErr.textContent = "";
      refundSubmit.disabled = false;
      refundSubmit.textContent = "提交退款";
      refundMask.classList.add("open");
    }
    function closeRefundModal() { refundMask.classList.remove("open"); refundTriggerBtn = null; }
    refundCancel.addEventListener("click", closeRefundModal);
    refundMask.addEventListener("click", (e) => { if (e.target === refundMask) closeRefundModal(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && refundMask.classList.contains("open")) closeRefundModal();
    });
    refundSubmit.addEventListener("click", async () => {
      const reason = refundReason.value;
      if (!reason) { refundErr.textContent = "请选择退款原因"; return; }
      refundErr.textContent = "";
      refundSubmit.disabled = true;
      refundSubmit.textContent = "提交中…";
      try {
        const resp = await fetch("/api/actions/create-refund", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conversation_id: getConversationId(),
            order_id: refundOrder.value, reason }),
        });
        const data = await resp.json();
        if (resp.ok) {
          if (refundTriggerBtn) refundTriggerBtn.disabled = true;
          closeRefundModal();
          addRow("bot").textContent = "退款申请已提交:" + data.ticket_no;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else {
          refundErr.textContent = "退款提交失败,请稍后重试";
          refundSubmit.disabled = false;
          refundSubmit.textContent = "提交退款";
        }
      } catch (e) {
        refundErr.textContent = "退款提交失败,请稍后重试";
        refundSubmit.disabled = false;
        refundSubmit.textContent = "提交退款";
      }
    });

    // ---------- 意图:订单选择器卡片(interrupt 缺订单号 → 聊天流点选 → resume 续跑)----------
    // onPick 决定点完卡片干什么:意图是 resume 把图续跑下去;工具系统归属校验拒绝之后
    // 没有待续跑的图,点了就当用户重新问一句(见 pickThenAsk)。卡片长相只此一份。
    function renderOrderCards(bubble, orders, onPick) {
      const intro = document.createElement("div");
      intro.className = "bubble-text";
      intro.textContent = orders.length ? "请选择您要处理的订单:" : "没有查到可选订单,请直接提供订单号。";
      bubble.appendChild(intro);
      const wrap = document.createElement("div");
      wrap.className = "order-cards";
      for (const o of orders) {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "order-card";
        card.innerHTML =
          '<div class="oc-top">订单 ' + o.order_id + '</div>' +
          '<div class="oc-mid">' + (o.product || "") + '</div>' +
          '<div class="oc-bot">' + (o.status || "") + ' · ¥' + (o.amount ?? "") + '</div>';
        card.addEventListener("click", async () => {
          wrap.querySelectorAll(".order-card").forEach((c) => (c.disabled = true));
          card.classList.add("picked");
          await (onPick || pickThenResume)(o);
        });
        wrap.appendChild(card);
      }
      bubble.appendChild(wrap);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // 意图:点完续跑图(有 interrupt 挂着)
    async function pickThenResume(o) {
      addRow("user").textContent = "选择订单 " + o.order_id;
      const nb = addRow("bot");
      setBusy(true);
      try {
        await streamInto(nb, () => fetch("/api/actions/resume", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conversation_id: getConversationId(), order_id: o.order_id }),
        }));
      } catch (e) {
        nb.className = "bubble error";
        nb.textContent = e.message || "续跑失败，请稍后重试";
        setInspectionState("请求失败");
      } finally {
        setBusy(false);
      }
    }

    // 工具系统:归属校验拒绝之后给的出路。这轮已经答完了,没有图等着续跑,所以走正常发消息那条路
    async function pickThenAsk(o) {
      await send("查一下订单 " + o.order_id);
    }

    // ---------- 工具系统:工单预览确认卡(interrupt confirm_ticket → 确认提交/取消 → resume 续跑)----------
    function renderTicketConfirm(bubble, preview) {
      const intro = document.createElement("div");
      intro.className = "bubble-text";
      intro.textContent = "请确认工单信息:";
      bubble.appendChild(intro);
      const card = document.createElement("div");
      card.className = "ticket-confirm";
      card.innerHTML =
        '<div class="tc-title">工单预览</div>' +
        '<div class="tc-row"><span class="tc-label">工单类型</span><span>' + escapeHtml(preview.ticket_type || "咨询") + "</span></div>" +
        '<div class="tc-row"><span class="tc-label">问题描述</span><span>' + escapeHtml(preview.description || "") + "</span></div>";
      const btns = document.createElement("div");
      btns.className = "tc-btns";
      const mk = (text, confirmed, extraClass) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "action-btn" + (extraClass ? " " + extraClass : "");
        btn.textContent = text;
        btn.addEventListener("click", async () => {
          btns.querySelectorAll("button").forEach((b) => (b.disabled = true));
          card.classList.add("decided");
          addRow("user").textContent = confirmed ? "确认提交工单" : "取消建单";
          const nb = addRow("bot");
          setBusy(true);
          try {
            await streamInto(nb, () => fetch("/api/actions/resume", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ conversation_id: getConversationId(), confirmed }),
            }));
          } catch (e) {
            nb.className = "bubble error";
            nb.textContent = e.message || "续跑失败，请稍后重试";
            setInspectionState("请求失败");
          } finally {
            setBusy(false);
          }
        });
        return btn;
      };
      btns.appendChild(mk("确认提交", true, "tc-ok"));
      btns.appendChild(mk("取消", false, "tc-cancel"));
      card.appendChild(btns);
      bubble.appendChild(card);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // 读一条 SSE 流,按帧分发到 on.{delta,tool,citations,actions,interrupt};done 存 conversation_id;event: error 抛错
    async function readSSEStream(resp, on) {
      if (!resp.ok || !resp.body) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : "服务暂时不可用，请稍后重试");
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "", finished = false;
      function dispatch(frame) {
        const lines = frame.split(/\r?\n/);
        const event = lines.find(l => l.startsWith("event:"));
        const payload = lines.filter(l => l.startsWith("data:")).map(l => l.slice(5).trimStart()).join("\n");
        if (!payload) return;
        if (payload === "[DONE]") { finished = true; return; }
        const data = JSON.parse(payload);
        if (event && event.slice(6).trim() === "error") throw new Error(data.message || "回复失败，请稍后重试");
        if (data.event === "tool") on.tool && on.tool(data.name);
        else if (data.event === "citations") on.citations && on.citations(data.items || []);
        else if (data.event === "interrupt") {
          if (data.conversation_id) setConversationId(data.conversation_id);
          on.interrupt && on.interrupt(data);
        } else if (data.event === "actions") on.actions && on.actions(data.items || []);
        else if (data.event === "done") setConversationId(data.conversation_id);
        else if (data.delta !== undefined) on.delta && on.delta(data.delta);
      }
      try {
        while (!finished) {
          const { value, done } = await reader.read();
          buffer += decoder.decode(value, { stream: !done });
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() || "";
          for (const frame of frames) dispatch(frame);
          if (done) {
            if (buffer.trim()) dispatch(buffer);
            if (!finished) throw new Error("连接提前结束，请检查回复后再重试");
            break;
          }
        }
      } finally {
        if (finished) await reader.cancel().catch(() => {});
        reader.releaseLock();
      }
    }

    // 把一次 SSE 流(/api/chat 首轮 或 /api/actions/resume 续跑)渲染进 bubble;
    // 遇 interrupt 按 kind 分发:select_order 渲染订单卡点选,confirm_ticket 渲染工单预览卡
    // (确认提交/取消),两者都在用户操作后开新气泡续跑。
    async function streamInto(bubble, doFetch) {
      beginInspection();
      showTyping(bubble);
      let structured = false, textStarted = false, interrupted = false, raw = "";
      let badgesEl = null, textEl = null, citations = [], actions = [];
      function ensureStructure() {
        if (structured) return;
        bubble.innerHTML = "";
        badgesEl = document.createElement("div");
        badgesEl.className = "tool-badges";
        textEl = document.createElement("div");
        textEl.className = "bubble-text";
        bubble.appendChild(badgesEl);
        bubble.appendChild(textEl);
        showTyping(textEl);
        structured = true;
      }
      const resp = await doFetch();
      await readSSEStream(resp, {
        delta: (delta) => {
          ensureStructure();
          if (!textStarted) { textEl.innerHTML = ""; textStarted = true; }
          raw += delta;
          textEl.innerHTML = renderMarkdown(raw);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        },
        tool: (toolName) => {
          ensureStructure();
          const b = document.createElement("div");
          b.className = "tool-badge";
          b.textContent = "工具返回 · " + toolLabel(toolName);
          inspectTool(toolName);
          badgesEl.appendChild(b);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        },
        citations: (items) => { citations = items; inspectSources(items); },
        actions: (items) => { actions = items; inspectActions(items); },
        interrupt: (data) => {
          interrupted = true;
          inspectInterrupt(data);
          if (!structured) { bubble.innerHTML = ""; structured = true; }
          // 工具系统:中断按 kind 分发——select_order 弹订单卡,confirm_ticket 弹工单预览卡
          if (data.kind === "confirm_ticket") renderTicketConfirm(bubble, data.preview || {});
          else renderOrderCards(bubble, data.orders || []);
        },
      });
      if (interrupted) return;
      setInspectionState("回复完成");
      if (!textStarted) { ensureStructure(); textEl.innerHTML = ""; textEl.textContent = "(无回复)"; }
      if (textStarted && citations.length) decorateCitations(textEl, citations);
      if (actions.length) renderActions(bubble, actions);
      if (textStarted) addFeedbackBar(bubble);
    }

    async function send(preset) {
      if (sendBtn.disabled) return;
      document.querySelector(".app").classList.remove("history-open");
      document.getElementById("history-toggle").setAttribute("aria-expanded", "false");
      const message = (preset ?? inputEl.value).trim();
      if (!message) return;
      inputEl.value = "";
      autoGrow();
      addRow("user").textContent = message;
      setBusy(true);
      const bubble = addRow("bot");
      try {
        await streamInto(bubble, () => fetch("/api/chat", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: getUserId(), message, conversation_id: getConversationId() }),
        }));
      } catch (err) {
        bubble.className = "bubble error";
        bubble.textContent = err.message || "回复失败，请稍后重试";
        setInspectionState("请求失败");
      } finally {
        setBusy(false);
        loadConversations();   // 上下文:每轮后刷新侧栏(新会话入列/摘要标记更新)
      }
    }

    // ---------- 上下文:会话侧栏(列表 / 切换 / 历史回载) ----------
    const convListEl = document.getElementById("conv-list");

    let conversationItems = [];
    async function loadConversations() {
      try {
        const r = await fetch("/api/conversations?user_id=" + encodeURIComponent(getUserId()));
        if (!r.ok) return;
        conversationItems = (await r.json()).items || [];
        filterConversations();
      } catch (e) { /* 侧栏失败不影响聊天 */ }
    }

    function renderConvList(items) {
      convListEl.innerHTML = "";
      if (!items.length) {
        const d = document.createElement("div");
        d.className = "sb-empty";
        d.textContent = document.getElementById("conversation-search").value ? "没有匹配的会话" : "还没有会话记录。\n发送消息，开始第一次服务。";
        convListEl.appendChild(d);
        return;
      }
      const current = getConversationId();
      for (const it of items) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "conv-item" + (it.id === current ? " active" : "");
        b.innerHTML =
          '<div class="ci-top">#' + it.id +
          (it.has_summary ? ' <span class="ci-sum">已摘要</span>' : "") + "</div>" +
          '<div class="ci-preview"></div>';
        b.querySelector(".ci-preview").textContent = it.preview || "";
        b.addEventListener("click", () => { if (!sendBtn.disabled) switchConversation(it.id); });
        convListEl.appendChild(b);
      }
    }

    // 历史回载时补挂按钮:suggested_actions 不落库,但这两句话术是固定文案,
    // 且转人工/建工单按钮不依赖当轮上下文(转人工纯前端模拟,建工单表单用户现填),
    // 按文案精确匹配重建即可。话术改了这里要跟着改(单一来源在后端 prompts.py)。
    const REPLAY_ACTIONS = [
      { text: "抱歉,这个问题我暂时没有查到确切信息,不敢乱答。建议您联系人工客服进一步确认,以免给您错误的指引。",
        actions: [{ type: "transfer_human" }] },
      { text: "非常抱歉给您带来了不好的体验,我理解您的心情。您可以选择转接人工客服,或让我为您登记一张工单跟进处理。",
        actions: [{ type: "transfer_human" }, { type: "create_ticket", draft: {} }] },
    ];

    async function switchConversation(cid) {
      document.querySelector(".app").classList.remove("history-open");
      document.getElementById("history-toggle").setAttribute("aria-expanded", "false");
      resetInspection("历史回看", "历史接口仅返回对话文本，本轮来源与工具记录将在下一次提问后显示。");
      // 已是当前会话且聊天区有内容才跳过;页面刚刷新时聊天区是空的,点当前会话也要回载
      if (cid === getConversationId() && messagesEl.querySelector(".row")) return;
      setConversationId(cid);
      messagesEl.innerHTML = "";
      try {
        const r = await fetch("/api/conversations/" + cid + "/messages");
        if (r.ok) {
          for (const m of ((await r.json()).items || [])) {
            if (!m.content) continue;
            if (m.role === "user") {
              addRow("user").textContent = m.content;
            } else {
              const bubble = addRow("bot");
              const textEl = document.createElement("div");
              textEl.className = "bubble-text";
              textEl.innerHTML = renderMarkdown(m.content);
              bubble.appendChild(textEl);
              const hit = REPLAY_ACTIONS.find((ra) => m.content.trim() === ra.text);
              if (hit) renderActions(bubble, hit.actions);
              addFeedbackBar(bubble);   // 反馈条(👍/👎)回载也要有,和直播轮一致
            }
          }
        }
      } catch (e) { /* 历史加载失败仍可继续聊 */ }
      if (!messagesEl.children.length) renderEmptyState();
      messagesEl.scrollTop = messagesEl.scrollHeight;
      loadConversations();   // 刷新 active 高亮
      inputEl.focus();
    }

    function autoGrow() {
      inputEl.style.height = "auto";
      inputEl.style.height = Math.min(inputEl.scrollHeight, 128) + "px";
    }

    sendBtn.addEventListener("click", () => send());
    inputEl.addEventListener("input", autoGrow);
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    });
    newChatBtn.addEventListener("click", () => {
      resetInspection();
      clearConversationId();          // 开新会话:丢弃当前 conversation_id(旧会话仍在侧栏可切回)
      renderEmptyState();
      loadConversations();            // 清 active 高亮
      inputEl.focus();
    });

    getUserId();
    renderEmptyState();
    loadConversations();              // 上下文:进页面先拉会话列表
    inputEl.focus();

    // Inspector displays only actual SSE events; no inferred intent or confidence.
    const TOOL_LABELS = {query_order:"订单查询",query_logistics:"物流查询",query_faq:"政策查询",create_ticket:"工单创建",submit_refund:"退款申请"};
    function toolLabel(name) { return TOOL_LABELS[name] || name; }
    function updateSessionLabel() {
      const id = getConversationId();
      document.getElementById("session-label").textContent = id ? "会话 #" + id : "新会话";
    }
    function setInspectionState(label) {
      document.getElementById("session-state").textContent = label;
      document.getElementById("inspection-state").textContent = label;
    }
    function placeholder(id, message) {
      const box = document.getElementById(id); box.textContent = "";
      const empty = document.createElement("div"); empty.className = "inspect-empty"; empty.textContent = message; box.appendChild(empty);
    }
    function resetInspection(state = "等待输入", note = "回复中的知识引用会在这里显示。点击来源，可查看政策原文。") {
      setInspectionState(state);
      placeholder("source-list", note);
      placeholder("tool-list", "本轮业务工具返回后，调用记录会显示在这里。");
      placeholder("pending-list", "涉及订单选择或工单提交时，操作卡片会出现在对话中。");
      document.getElementById("source-count").textContent = "0";
      document.getElementById("tool-count").textContent = "0";
    }
    function beginInspection() { resetInspection("处理请求中"); }
    function inspectSources(items) {
      const box = document.getElementById("source-list"); box.textContent = "";
      document.getElementById("source-count").textContent = items.length;
      if (!items.length) { placeholder("source-list", "本轮没有返回知识来源。"); return; }
      for (const c of items) {
        const detail = document.createElement("details"); detail.className = "source-card";
        const summary = document.createElement("summary"); summary.textContent = "[" + c.n + "] " + (c.section_path || c.question || "知识来源");
        const p = document.createElement("p"); p.textContent = c.answer || "";
        const small = document.createElement("small"); small.textContent = c.question || "知识库原文";
        detail.append(summary, p, small); box.appendChild(detail);
      }
    }
    function inspectTool(name) {
      const box = document.getElementById("tool-list");
      const empty = box.querySelector(".inspect-empty"); if (empty) empty.remove();
      const row = document.createElement("div"); row.className = "tool-entry";
      const icon = document.createElement("span"); icon.className = "tool-symbol"; icon.textContent = "◇";
      const text = document.createElement("div");
      const label = document.createElement("strong"); label.textContent = toolLabel(name);
      const meta = document.createElement("small"); meta.textContent = "收到工具返回 · " + new Date().toLocaleTimeString("zh-CN", {hour:"2-digit",minute:"2-digit"});
      text.append(label, meta); row.append(icon, text); box.appendChild(row);
      document.getElementById("tool-count").textContent = box.children.length;
    }
    function inspectInterrupt(data) {
      setInspectionState("等待确认");
      const note = document.createElement("div"); note.className = "pending-note";
      note.textContent = data.kind === "confirm_ticket" ? "请在对话中核对工单内容，确认提交或取消。" : "请在对话中选择需要处理的订单，服务将从暂停的位置继续。";
      const box = document.getElementById("pending-list"); box.replaceChildren(note);
    }
    function inspectActions(items) {
      const names = {refund_form:"提交退款工单",create_ticket:"创建工单",transfer_human:"人工转接演示",select_order:"选择订单"};
      placeholder("pending-list", "可在对话中操作：" + items.map(a => names[a.type] || a.type).join("、"));
    }
    function filterConversations() {
      const query = document.getElementById("conversation-search").value.trim().toLowerCase();
      renderConvList(conversationItems.filter(it => ("#" + it.id + " " + (it.preview || "")).toLowerCase().includes(query)));
    }
    document.getElementById("conversation-search").addEventListener("input", filterConversations);
    document.getElementById("history-toggle").addEventListener("click", () => {
      const open = document.querySelector(".app").classList.toggle("history-open");
      document.getElementById("history-toggle").setAttribute("aria-expanded", String(open));
    });
    document.getElementById("close-history").addEventListener("click", () => {
      document.querySelector(".app").classList.remove("history-open");
      document.getElementById("history-toggle").setAttribute("aria-expanded", "false");
      document.getElementById("history-toggle").focus();
    });
    document.getElementById("inspector-toggle").addEventListener("click", () => {
      const open = document.querySelector(".app").classList.toggle("inspector-open");
      document.getElementById("inspector-toggle").setAttribute("aria-expanded", String(open));
    });
    document.getElementById("close-inspector").addEventListener("click", () => {
      document.querySelector(".app").classList.remove("inspector-open");
      document.getElementById("inspector-toggle").setAttribute("aria-expanded", "false");
    });
    document.addEventListener("keydown", e => { if (e.key === "Escape") {
      document.querySelector(".app").classList.remove("inspector-open", "history-open");
      document.getElementById("inspector-toggle").setAttribute("aria-expanded", "false");
      document.getElementById("history-toggle").setAttribute("aria-expanded", "false");
    }});
    mountAdminNav("/"); updateSessionLabel(); resetInspection();
