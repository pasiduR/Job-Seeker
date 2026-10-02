// Collect fillable form fields from one document. Returns plain data only.
// Each element (or radio/checkbox group) is tagged with data-jobseeker-field so
// later fill steps can find it with a stable selector on the same page.
(prefix) => {
  const TAG = "data-jobseeker-field";
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image"]);
  const clean = (text) => (text || "").replace(/\s+/g, " ").trim();

  const visible = (el) => {
    // File inputs are often hidden behind a styled button; their container is not.
    if (el.type === "file") return !el.parentElement || visible(el.parentElement);
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return el.getClientRects().length > 0;
  };

  const textOfIds = (ids) =>
    clean(
      (ids || "")
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((node) => node.textContent)
        .join(" ")
    );

  const labelFor = (el) => {
    const labelled = textOfIds(el.getAttribute("aria-labelledby"));
    if (labelled) return labelled;
    if (el.labels && el.labels.length) {
      return clean(Array.from(el.labels).map((label) => label.textContent).join(" "));
    }
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    return clean(el.getAttribute("placeholder")) || clean(el.getAttribute("title"));
  };

  const groupLabel = (el) => {
    const group = el.closest("fieldset, [role=radiogroup], [role=group]");
    if (!group) return "";
    const legend = group.querySelector("legend");
    if (legend) return clean(legend.textContent);
    return textOfIds(group.getAttribute("aria-labelledby")) || clean(group.getAttribute("aria-label"));
  };

  const isRequired = (el, label) =>
    el.required ||
    el.getAttribute("aria-required") === "true" ||
    /\*\s*$/.test(label);

  const fields = [];
  const groups = new Map();
  // Elements tagged by an earlier extraction keep their id, so answers mapped
  // before new fields appeared still point at the same elements.
  let counter = 0;
  for (const tagged of document.querySelectorAll(`[${TAG}]`)) {
    const number = Number(tagged.getAttribute(TAG).slice(prefix.length));
    if (tagged.getAttribute(TAG).startsWith(prefix) && number > counter) counter = number;
  }
  const idFor = (el) => el.getAttribute(TAG) || `${prefix}${++counter}`;

  const elements = document.querySelectorAll("input, select, textarea");
  for (const el of elements) {
    const type = el.tagName === "INPUT" ? (el.type || "text").toLowerCase() : el.tagName.toLowerCase();
    if (SKIP_TYPES.has(type) || el.disabled || !visible(el)) continue;

    if ((type === "radio" || type === "checkbox") && el.name) {
      const key = `${type}:${el.form ? Array.from(document.forms).indexOf(el.form) : -1}:${el.name}`;
      let group = groups.get(key);
      if (!group) {
        group = { id: idFor(el), type, members: [], el };
        groups.set(key, group);
        fields.push(group);
      }
      group.members.push(el);
      el.setAttribute(TAG, group.id);
      continue;
    }

    const id = idFor(el);
    el.setAttribute(TAG, id);
    const label = labelFor(el);
    const role = (el.getAttribute("role") || "").toLowerCase();
    fields.push({
      field_id: id,
      label,
      type: role === "combobox" && type !== "select" ? "combobox" : type,
      required: isRequired(el, label),
      options:
        type === "select"
          ? Array.from(el.options)
              .map((option) => clean(option.textContent))
              .filter((text) => text && !/^(select|choose|--)/i.test(text))
          : [],
      multiple: Boolean(el.multiple),
      placeholder: clean(el.getAttribute("placeholder")),
      accept: clean(el.getAttribute("accept")),
      selector: `[${TAG}="${id}"]`,
    });
  }

  return fields.map((field) => {
    if (!field.members) return field;
    const { members, type, id } = field;
    const single = type === "checkbox" && members.length === 1;
    const label = single ? labelFor(members[0]) : groupLabel(members[0]) || labelFor(members[0]);
    return {
      field_id: id,
      label,
      type: single ? "checkbox" : `${type}_group`,
      required: members.some((member) => isRequired(member, label)),
      options: single ? [] : members.map((member) => labelFor(member) || clean(member.value)),
      multiple: type === "checkbox" && !single,
      placeholder: "",
      accept: "",
      selector: `[${TAG}="${id}"]`,
    };
  });
}
