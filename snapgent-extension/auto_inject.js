// auto_inject.js - content script: inject prompt into chat provider input and auto-send
(function () {
  const SELECTORS = {
    "chatgpt.com": {
      input: "#prompt-textarea, textarea[data-id=prompt-textarea], div[contenteditable=true][data-id=prompt-textarea]",
      send: "button[data-testid=send-button], button[aria-label*='Send'], button.send-button"
    },
    "chat.openai.com": {
      input: "#prompt-textarea, textarea",
      send: "button[data-testid=send-button], button[aria-label*='Send']"
    }
  };
  const host = location.hostname.replace(/^www\./, "");
  const cfg = SELECTORS[host];
  if (!cfg) return;

  function setInput(text) {
    const el = document.querySelector(cfg.input);
    if (!el) return false;
    if (el.tagName === "TEXTAREA") {
      el.value = text;
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      el.innerText = text;
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }
    return true;
  }

  function clickSend() {
    const btn = document.querySelector(cfg.send);
    if (btn) { btn.click(); return true; }
    return false;
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === "auto_inject_prompt") {
      const ok = setInput(msg.prompt) && clickSend();
      sendResponse({ ok });
    }
  });
})();
