/* Shared application shell. Existing page routes and business APIs stay intact. */
(function () {
  const NAV = [
    { href: "/admin", label: "工作台概览", icon: "▦", group: "工作空间" },
    { href: "/", label: "会话工作台", icon: "◫" },
    { href: "/kb", label: "知识中心", icon: "▤" },
    { href: "/review", label: "知识审核", icon: "✓" },
    { href: "/rag-eval", label: "检索质量", icon: "◎", group: "分析与优化" },
    { href: "/observability", label: "观测与成本", icon: "↗" },
    { href: "/topics", label: "问题洞察", icon: "≋" },
    { href: "/acceptance", label: "分类器实验", icon: "◇", children: [
      ["/acceptance", "实验概览"], ["/acceptance/eval", "评测详情"],
      ["/acceptance/data", "数据产物"], ["/acceptance/errors", "错例复核"],
    ] },
  ];
  const elem = (tag, cls, text) => {
    const e = document.createElement(tag); e.className = cls || "";
    if (text !== undefined) e.textContent = text;
    return e;
  };
  function moduleOf(active) {
    return NAV.find(m => m.href === active)
      || NAV.find(m => m.href !== "/" && active.startsWith(m.href + "/"));
  }
  function renderAdminNav(active) {
    const mod = moduleOf(active);
    const side = elem("aside", "studio-nav"); side.id = "studio-navigation";
    const brand = elem("a", "studio-brand"); brand.href = "/admin";
    brand.append(elem("span", "brand-mark", "S"));
    const brandText = elem("span", "brand-text");
    brandText.append(elem("strong", "", "知序客服"), elem("small", "", "SUPPORT STUDIO"));
    brand.append(brandText); side.append(brand);
    const workspace = elem("div", "workspace-label");
    workspace.append(elem("span", "workspace-avatar", "知"), elem("span", "", "电商服务工作空间"));
    side.append(workspace);
    const nav = elem("nav", "studio-links"); nav.setAttribute("aria-label", "主导航");
    for (const m of NAV) {
      if (m.group) nav.append(elem("div", "nav-group", m.group));
      const a = elem("a", "studio-link" + (m === mod ? " active" : "")); a.href = m.href;
      if (m === mod) a.setAttribute("aria-current", "page");
      const icon = elem("span", "nav-icon", m.icon); icon.setAttribute("aria-hidden", "true");
      a.append(icon, elem("span", "", m.label)); nav.append(a);
      if (m === mod && m.children) {
        const sub = elem("div", "studio-subnav");
        for (const [href, label] of m.children) {
          const child = elem("a", href === active ? "active" : "", label); child.href = href;
          if (href === active) child.setAttribute("aria-current", "page"); sub.append(child);
        }
        nav.append(sub);
      }
    }
    side.append(nav);
    const bottom = elem("div", "nav-bottom");
    bottom.append(elem("div", "nav-bottom-title", "让每一次服务都有依据"),
      elem("p", "", "知识检索 · 业务工具 · 人工确认"));
    const account = elem("div", "nav-account");
    account.append(elem("span", "account-avatar", "WS"), elem("span", "", "客服演示工作空间"));
    bottom.append(account); side.append(bottom);
    return side;
  }
  function mountAdminNav(active) {
    if (document.querySelector(".studio-shell")) return document.querySelector(".studio-nav");
    const content = document.querySelector(".wrap") || document.querySelector(".app");
    if (!content) return;
    const shell = elem("div", "studio-shell" + (active === "/" ? " chat-shell" : ""));
    const side = renderAdminNav(active);
    const main = elem("main", "studio-main"); main.id = "workspace-main";
    const chrome = elem("div", "studio-chrome");
    const menu = elem("button", "shell-menu", "☰"); menu.type = "button";
    menu.setAttribute("aria-label", "打开导航"); menu.setAttribute("aria-controls", side.id);
    menu.setAttribute("aria-expanded", "false");
    const mod = moduleOf(active);
    chrome.append(menu, elem("span", "chrome-breadcrumb", "工作空间 / " + (mod ? mod.label : "客服")));
    chrome.append(elem("span", "chrome-tag", "电商客服 · 演示环境"));
    main.append(chrome); content.parentNode.insertBefore(shell, content);
    main.append(content); shell.append(side, main);
    const shade = elem("button", "nav-shade"); shade.type = "button";
    shade.setAttribute("aria-label", "关闭导航"); shell.append(shade);
    const close = () => { shell.classList.remove("nav-open"); menu.setAttribute("aria-expanded", "false"); };
    menu.addEventListener("click", () => {
      const open = shell.classList.toggle("nav-open"); menu.setAttribute("aria-expanded", String(open));
    });
    shade.addEventListener("click", close);
    document.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
    return side;
  }
  window.renderAdminNav = renderAdminNav;
  window.mountAdminNav = mountAdminNav;
})();
