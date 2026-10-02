// Collect visible clickable controls (buttons and button-like links) from one
// document, tagging each with data-jobseeker-button for a stable selector.
(prefix) => {
  const TAG = "data-jobseeker-button";
  const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return el.getClientRects().length > 0;
  };

  let counter = 0;
  for (const tagged of document.querySelectorAll(`[${TAG}]`)) {
    const value = tagged.getAttribute(TAG);
    const number = Number(value.slice(prefix.length));
    if (value.startsWith(prefix) && number > counter) counter = number;
  }

  const selector = "button, input[type=submit], input[type=button], a[role=button], [role=button]";
  const buttons = [];
  for (const el of document.querySelectorAll(selector)) {
    if (el.disabled || !visible(el)) continue;
    const text = clean(el.innerText || el.value || el.getAttribute("aria-label") || el.title);
    if (!text) continue;
    const id = el.getAttribute(TAG) || `${prefix}${++counter}`;
    el.setAttribute(TAG, id);
    buttons.push({
      button_id: id,
      text,
      is_submit_type: el.type === "submit",
      selector: `[${TAG}="${id}"]`,
    });
  }
  return buttons;
}
