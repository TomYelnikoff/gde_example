(() => {
  const $ = (id) => document.getElementById(id);
  const layout = document.querySelector(".layout");
  const chat = $("chat");
  const thread = $("thread");
  const welcome = $("welcome");
  const form = $("composer");
  const input = $("input");
  const sendBtn = $("send");
  const historyEl = $("history");
  const searchEl = $("search");
  const titleEl = $("convo-title");
  const convoUsageEl = $("convo-usage");

  const planEl = $("plan");
  const banner = $("limit-banner");
  const composerEl = form;

  let quota = null; // {plan, limit, used, remaining} - remaining is null on paid plans
  let totals = { input: 0, output: 0 };
  let conversations = []; // [{id, title, updated_at}]
  let currentId = null;
  let busy = false;
  let loadToken = 0; // guards against out-of-order conversation loads

  // ---------- Safe, minimal Markdown (HTML is escaped first) ----------
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function inline(text) {
    return esc(text)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  function renderMarkdown(src) {
    let html = "";
    src.split(/```/).forEach((part, i) => {
      if (i % 2 === 1) {
        const code = part.replace(/^[^\n]*\n/, "").replace(/\n$/, "");
        html += `<pre><button type="button" class="copy-btn">Copy</button><code>${esc(code)}</code></pre>`;
        return;
      }
      let list = null, para = [];
      const flushPara = () => { if (para.length) { html += `<p>${para.join("<br>")}</p>`; para = []; } };
      const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
      for (const line of part.split("\n")) {
        let m;
        if ((m = line.match(/^(#{1,3})\s+(.*)/))) { flushPara(); flushList(); html += `<h3>${inline(m[2])}</h3>`; }
        else if ((m = line.match(/^\s*[-*]\s+(.*)/))) {
          flushPara(); if (list !== "ul") { flushList(); html += "<ul>"; list = "ul"; }
          html += `<li>${inline(m[1])}</li>`;
        } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
          flushPara(); if (list !== "ol") { flushList(); html += "<ol>"; list = "ol"; }
          html += `<li>${inline(m[1])}</li>`;
        } else if (line.trim() === "") { flushPara(); flushList(); }
        else { flushList(); para.push(inline(line)); }
      }
      flushPara(); flushList();
    });
    return html;
  }

  // ---------- API helpers ----------
  async function api(url, options) {
    const res = await fetch(url, options);
    if (res.status === 401) { location.replace("/login"); throw new Error("Please sign in."); }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const e = new Error(err.error || `Request failed (${res.status})`);
      e.status = res.status;
      throw e;
    }
    return res.status === 204 ? null : res.json();
  }

  // ---------- Sidebar ----------
  function groupLabel(iso) {
    const d = new Date(iso);
    const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
    const days = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days <= 7) return "Previous 7 days";
    if (days <= 30) return "Previous 30 days";
    return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  }

  const ICON_EDIT = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
  const ICON_TRASH = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';

  function renderSidebar() {
    const q = searchEl.value.trim().toLowerCase();
    const list = conversations.filter((c) => c.title.toLowerCase().includes(q));
    historyEl.innerHTML = "";

    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "empty-history";
      empty.textContent = q ? "No matching chats" : "No conversations yet";
      historyEl.appendChild(empty);
      return;
    }

    let lastGroup = null;
    for (const c of list) {
      const label = groupLabel(c.updated_at);
      if (label !== lastGroup) {
        const g = document.createElement("div");
        g.className = "group-label";
        g.textContent = label;
        historyEl.appendChild(g);
        lastGroup = label;
      }

      const item = document.createElement("div");
      item.className = "item" + (c.id === currentId ? " active" : "");
      item.dataset.id = c.id;
      item.innerHTML =
        `<div class="title" role="link" tabindex="0"></div>` +
        `<div class="actions">` +
        `<button class="icon-btn" type="button" data-act="rename" aria-label="Rename chat" title="Rename">${ICON_EDIT}</button>` +
        `<button class="icon-btn danger" type="button" data-act="delete" aria-label="Delete chat" title="Delete">${ICON_TRASH}</button>` +
        `</div>`;
      item.querySelector(".title").textContent = c.title;
      item.querySelector(".title").title = c.title;
      historyEl.appendChild(item);
    }
  }

  function upsertConversation(conv) {
    const i = conversations.findIndex((c) => c.id === conv.id);
    if (i >= 0) conversations[i] = { ...conversations[i], ...conv };
    else conversations.unshift(conv);
    conversations.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
    renderSidebar();
    if (conv.id === currentId) titleEl.textContent = conversations.find((c) => c.id === conv.id).title;
  }

  function startRename(item) {
    const id = Number(item.dataset.id);
    const conv = conversations.find((c) => c.id === id);
    const field = document.createElement("input");
    field.className = "rename-input";
    field.value = conv.title;
    field.maxLength = 80;
    item.replaceChildren(field);
    field.focus();
    field.select();

    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const title = field.value.trim();
      if (save && title && title !== conv.title) {
        try {
          const res = await api(`/api/conversations/${id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title }),
          });
          conv.title = res.title;
        } catch (err) { alert(err.message); }
      }
      renderSidebar();
      if (id === currentId) titleEl.textContent = conv.title;
    };
    field.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") finish(false);
    });
    field.addEventListener("blur", () => finish(true));
  }

  async function deleteConversation(id) {
    const conv = conversations.find((c) => c.id === id);
    if (!confirm(`Delete "${conv.title}"? This can't be undone.`)) return;
    try {
      await api(`/api/conversations/${id}`, { method: "DELETE" });
    } catch (err) {
      if (err.status !== 404) { alert(err.message); return; }
    }
    conversations = conversations.filter((c) => c.id !== id);
    if (id === currentId) newChat(true); else renderSidebar();
  }

  // ---------- Thread rendering ----------
  function scrollDown() { chat.scrollTop = chat.scrollHeight; }

  const fmt = (n) => Number(n).toLocaleString();

  function usageText(u) {
    if (!u || u.input_tokens == null || u.output_tokens == null) return "";
    return `${fmt(u.input_tokens)} in · ${fmt(u.output_tokens)} out · ${fmt(u.input_tokens + u.output_tokens)} tokens`;
  }

  function setUsage(row, u) {
    const el = row.querySelector(".usage");
    if (el) el.textContent = usageText(u);
  }

  function setConvoUsage(u) {
    totals = { input: u?.input_tokens || 0, output: u?.output_tokens || 0 };
    const sum = totals.input + totals.output;
    convoUsageEl.hidden = sum === 0;
    convoUsageEl.textContent = `${fmt(sum)} tokens`;
    convoUsageEl.title = `This conversation: ${fmt(totals.input)} input + ${fmt(totals.output)} output tokens`;
  }

  function addMessage(role, text = "", usage = null) {
    welcome.remove();
    const row = document.createElement("div");
    row.className = `msg ${role === "user" ? "user" : "bot"}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "user") {
      bubble.textContent = text;
      row.append(bubble);
    } else {
      const avatar = document.createElement("div");
      avatar.className = "avatar";
      avatar.textContent = "AI";
      const body = document.createElement("div");
      body.className = "body";
      const usageEl = document.createElement("div");
      usageEl.className = "usage";
      usageEl.title = "Input tokens include the conversation history sent with this call";
      usageEl.textContent = usageText(usage);
      if (text) bubble.innerHTML = renderMarkdown(text);
      body.append(bubble, usageEl);
      row.append(avatar, body);
    }
    thread.appendChild(row);
    return { row, bubble };
  }

  function resetThread() {
    thread.replaceChildren(welcome);
    setConvoUsage(null);
  }

  // ---------- Navigation ----------
  function closeSidebarOnMobile() { layout.classList.remove("side-open"); }

  function newChat(replace = false) {
    if (busy) return;
    loadToken++;
    currentId = null;
    resetThread();
    titleEl.textContent = "New chat";
    history[replace ? "replaceState" : "pushState"]({}, "", "/");
    renderSidebar();
    closeSidebarOnMobile();
    input.focus();
  }

  async function openConversation(id, { push = true } = {}) {
    if (busy && id !== currentId) return;
    const token = ++loadToken;
    try {
      const conv = await api(`/api/conversations/${id}`);
      if (token !== loadToken) return;
      currentId = conv.id;
      resetThread();
      conv.messages.forEach((m) => addMessage(m.role, m.content, m));
      setConvoUsage(conv.usage);
      titleEl.textContent = conv.title;
      if (push) history.pushState({}, "", `/c/${conv.id}`);
      renderSidebar();
      scrollDown();
    } catch (err) {
      if (err.status === 404) { newChat(true); } else { alert(err.message); }
    }
    closeSidebarOnMobile();
  }

  function routeFromLocation() {
    const m = location.pathname.match(/^\/c\/(\d+)$/);
    if (m) openConversation(Number(m[1]), { push: false });
    else { loadToken++; currentId = null; resetThread(); titleEl.textContent = "New chat"; renderSidebar(); }
  }

  // ---------- Sending ----------
  const isLocked = () => !!quota && quota.plan !== "paid" && quota.remaining <= 0;

  function refreshComposer() {
    const locked = isLocked();
    input.disabled = busy || locked;
    sendBtn.disabled = busy || locked || !input.value.trim();
    input.placeholder = locked ? "Free message limit reached" : "Message the assistant…";
    composerEl.classList.toggle("locked", locked);
    banner.hidden = !locked;
  }

  function applyQuota(q) {
    quota = q;
    if (!q) { planEl.hidden = true; refreshComposer(); return; }

    const paid = q.plan === "paid";
    planEl.hidden = false;
    planEl.classList.toggle("low", !paid && q.remaining > 0 && q.remaining <= 3);
    planEl.classList.toggle("empty", !paid && q.remaining <= 0);
    $("plan-name").textContent = paid ? "Paid plan" : "Free plan";
    $("plan-count").textContent = paid ? "Unlimited" : `${q.remaining} of ${q.limit} left`;
    $("meter").hidden = paid;
    $("upgrade").hidden = paid;
    if (!paid) {
      $("meter-fill").style.width = `${Math.min(100, (q.used / q.limit) * 100)}%`;
      $("meter").setAttribute("aria-valuemax", q.limit);
      $("meter").setAttribute("aria-valuenow", Math.min(q.used, q.limit));
      $("limit-title").textContent = `You've used all ${q.limit} free messages.`;
    }
    refreshComposer();
  }

  function setBusy(value) {
    busy = value;
    refreshComposer();
    if (!value && !isLocked()) input.focus();
  }

  function autoResize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }

  async function send(text) {
    text = text.trim();
    if (!text || busy || isLocked()) return;

    const userMsg = addMessage("user", text);
    input.value = "";
    autoResize();
    setBusy(true);

    const { row, bubble } = addMessage("bot");
    bubble.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
    scrollDown();
    let reply = "";
    let finished = false;

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversation_id: currentId }),
      });
      if (res.status === 401) { location.replace("/login"); return; }
      if (res.status === 402) { // free allowance used up (e.g. in another tab): undo and lock
        const err = await res.json().catch(() => ({}));
        userMsg.row.remove();
        row.remove();
        if (!thread.children.length) thread.appendChild(welcome);
        input.value = text;
        autoResize();
        applyQuota(err.quota || { plan: "free", limit: quota?.limit ?? 10, used: quota?.limit ?? 10, remaining: 0 });
        return;
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const e = new Error(err.error || `Request failed (${res.status})`);
        e.status = res.status;
        throw e;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const event = (block.match(/^event: (.*)$/m) || [])[1];
          const data = JSON.parse((block.match(/^data: (.*)$/m) || [, "{}"])[1]);

          if (event === "token") {
            reply += data.text;
            bubble.innerHTML = renderMarkdown(reply);
            scrollDown();
          } else if (event === "saved") {
            currentId = data.conversation_id;
            if (data.is_new) history.pushState({}, "", `/c/${currentId}`);
            upsertConversation({ id: data.conversation_id, title: data.title, updated_at: data.updated_at });
            setUsage(row, data.usage);
            if (data.quota) applyQuota(data.quota);
            setConvoUsage({
              input_tokens: totals.input + (data.usage?.input_tokens || 0),
              output_tokens: totals.output + (data.usage?.output_tokens || 0),
            });
          } else if (event === "done") {
            finished = true;
            setBusy(false); // re-enable input; the title event may still follow
          } else if (event === "title") {
            upsertConversation({ id: data.conversation_id, title: data.title });
          } else if (event === "error") {
            throw new Error(data.message);
          }
        }
      }
      if (!finished) throw new Error("The connection was interrupted.");
    } catch (err) {
      if (reply) {
        bubble.innerHTML = renderMarkdown(reply) + `<p><em>${esc(err.message)}</em> This reply was not saved.</p>`;
      } else {
        row.classList.add("error");
        row.querySelector(".avatar")?.remove();
        bubble.textContent = err.message || "Something went wrong.";
      }
    } finally {
      setBusy(false);
    }
  }

  // ---------- Events ----------
  form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  input.addEventListener("input", () => { autoResize(); refreshComposer(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });

  document.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => send(chip.textContent)));

  $("signout").addEventListener("click", async () => {
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (_) { /* leave anyway */ }
    location.replace("/login");
  });

  $("new-chat").addEventListener("click", () => newChat());
  $("open-side").addEventListener("click", () => layout.classList.add("side-open"));
  $("close-side").addEventListener("click", closeSidebarOnMobile);
  $("scrim").addEventListener("click", closeSidebarOnMobile);
  searchEl.addEventListener("input", renderSidebar);

  historyEl.addEventListener("click", (e) => {
    const item = e.target.closest(".item");
    if (!item || e.target.closest(".rename-input")) return;
    const id = Number(item.dataset.id);
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "rename") startRename(item);
    else if (act === "delete") deleteConversation(id);
    else if (id !== currentId) openConversation(id);
    else closeSidebarOnMobile();
  });
  historyEl.addEventListener("keydown", (e) => {
    const title = e.target.closest(".title");
    if (title && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); title.click(); }
  });

  thread.addEventListener("click", (e) => {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    navigator.clipboard.writeText(btn.parentElement.querySelector("code").textContent).then(() => {
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    });
  });

  window.addEventListener("popstate", () => { if (!busy) routeFromLocation(); });

  // ---------- Init ----------
  (async function init() {
    sendBtn.disabled = true;
    try { conversations = await api("/api/conversations"); } catch (err) { console.error(err); }
    try { applyQuota((await api("/api/me")).quota); } catch (err) { console.error(err); }
    renderSidebar();
    routeFromLocation();
    input.focus();
  })();
})();
