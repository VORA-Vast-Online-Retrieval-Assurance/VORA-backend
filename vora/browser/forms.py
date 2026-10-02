"""Query forms: which forms may be operated on a page, and with what values.

Many portals (registries, gazettes, statistics offices, directories) keep their
records behind a search form, so a crawler that never operates one cannot reach
them. Operating a form is only safe when it *asks a question of the site* and
changes nothing about the visitor. This module separates the two by purpose:

* ``query``: search, filter and date-range forms. May be filled and submitted.
* ``auth``, ``payment``, ``contact``, ``subscribe``, ``upload``: change state or
  send data about the visitor. Never touched.
* ``challenge``: a CAPTCHA or human check. Never solved or bypassed.

Purpose is decided from the fields (types, names, labels), never from the
site. Values come from the request: a date range from its time window, keywords
from its subject; a field with no matching value keeps the site's own default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

def tokens(value: str) -> tuple[str, ...]:
    """Lower-case words of a field name or label; camelCase and underscores split
    ("txtDateFrom" -> txt, date, from). Core keeps its own so it needs nothing above it."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(value or ""))
    return tuple(re.findall(r"[a-z]+|\d+", text.casefold()))


Purpose = Literal["query", "auth", "payment", "contact", "subscribe", "upload", "challenge", "other"]

# Reads every form on the page and marks each field/submit control with
# data-vora-field="<form>-<field>" so it can be addressed later.
INVENTORY_JS = r"""
() => {
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const labelOf = (el) => {
    let text = '';
    if (el.labels && el.labels.length) text = el.labels[0].innerText;
    if (!text && el.id) {
      const tag = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (tag) text = tag.innerText;
    }
    if (!text) text = el.getAttribute('aria-label') || '';
    if (!text) {  // table layouts (WebForms): the label sits in the previous cell
      const cell = el.closest('td');
      if (cell && cell.previousElementSibling) text = cell.previousElementSibling.innerText;
    }
    return (text || el.getAttribute('placeholder') || el.getAttribute('title') || '').trim().slice(0, 80);
  };
  return [...document.forms].map((form, formIndex) => {
    const fields = [];
    [...form.querySelectorAll('input, select, textarea, button')].forEach((el, fieldIndex) => {
      const type = (el.getAttribute('type') || (el.tagName === 'BUTTON' ? 'submit' : 'text')).toLowerCase();
      const id = `${formIndex}-${fieldIndex}`;
      el.setAttribute('data-vora-field', id);
      fields.push({
        id, tag: el.tagName.toLowerCase(), type, name: el.getAttribute('name') || '', domId: el.id || '',
        label: el.tagName === 'BUTTON' || type === 'submit' || type === 'button'
          ? (el.innerText || el.getAttribute('value') || '').trim().slice(0, 60) : labelOf(el),
        placeholder: el.getAttribute('placeholder') || '', pattern: el.getAttribute('pattern') || '',
        required: !!el.required, visible: visible(el) && type !== 'hidden',
        options: el.tagName === 'SELECT'
          ? [...el.options].slice(0, 300).map((option) => ({ value: option.value, text: option.text.trim() })) : [],
        value: type === 'password' ? '' : String(el.value || '').slice(0, 100),
      });
    });
    return { index: formIndex, method: (form.method || 'get').toLowerCase(),
             action: form.getAttribute('action') || '', fields, visible: visible(form),
             text: (form.innerText || '').slice(0, 400) };
  });
}
"""


@dataclass(slots=True)
class FormField:
    id: str
    tag: str
    type: str
    name: str = ""
    dom_id: str = ""
    label: str = ""
    placeholder: str = ""
    pattern: str = ""
    required: bool = False
    visible: bool = True
    options: list[dict[str, str]] = field(default_factory=list)
    value: str = ""

    @property
    def words(self) -> set[str]:
        return set(tokens(" ".join([self.name, self.dom_id, self.label, self.placeholder])))


@dataclass(slots=True)
class FormInfo:
    index: int
    method: str
    action: str
    fields: list[FormField]
    visible: bool = True
    text: str = ""

    @property
    def controls(self) -> list[FormField]:
        return [item for item in self.fields if item.visible and item.tag != "button" and item.type not in
                {"submit", "button", "reset", "image", "hidden", "checkbox", "radio"}]

    @property
    def submits(self) -> list[FormField]:
        return [item for item in self.fields if item.visible and (item.tag == "button" or item.type in
                {"submit", "image", "button"})]


def parse_forms(raw: list[dict]) -> list[FormInfo]:
    forms = []
    for entry in raw:
        fields = [FormField(
            id=item["id"], tag=item["tag"], type=item["type"], name=item.get("name", ""),
            dom_id=item.get("domId", ""), label=item.get("label", ""), placeholder=item.get("placeholder", ""),
            pattern=item.get("pattern", ""), required=bool(item.get("required")), visible=bool(item.get("visible")),
            options=list(item.get("options") or []), value=item.get("value", "")) for item in entry["fields"]]
        forms.append(FormInfo(entry["index"], entry.get("method", "get"), entry.get("action", ""), fields,
                              bool(entry.get("visible", True)), entry.get("text", "")))
    return forms


_CARD = {"card", "cvv", "cvc", "ccnum", "expiry", "expiration", "billing", "iban", "cardnumber"}
_AUTH = {"login", "signin", "username", "userid", "passcode", "otp"}
_CONTACT = {"message", "comment", "enquiry", "inquiry", "feedback", "subject_line"}
_SUBSCRIBE = {"subscribe", "newsletter", "subscription"}
_CHALLENGE = {"captcha", "recaptcha", "hcaptcha", "turnstile", "verification", "verify"}
_UNSAFE_SUBMIT = re.compile(
    r"\b(?:log ?in|sign ?(?:in|up)|register|subscribe|buy|cart|checkout|order|pay|donate|send|message|"
    r"upload|delete|remove|download|book|reserve|contact|join|apply now|enrol+)\b", re.I)


def classify_form(form: FormInfo) -> Purpose:
    """What a form is for, from its fields alone."""
    words = set().union(*(item.words for item in form.fields)) if form.fields else set()
    types = {item.type for item in form.fields}
    text_words = set(tokens(form.text))
    if words & _CHALLENGE or "captcha" in form.text.casefold():
        return "challenge"
    if "password" in types or words & _AUTH and not words & {"search", "query"}:
        return "auth"
    if "file" in types:
        return "upload"
    if words & _CARD:
        return "payment"
    has_email = "email" in types or "email" in words or "mail" in words
    if (words | text_words) & _SUBSCRIBE and has_email:
        return "subscribe"
    if any(item.tag == "textarea" for item in form.fields) or (words & _CONTACT and has_email):
        return "contact"
    controls = form.controls
    if controls and form.submits:
        return "query"
    return "other"


def submit_control(form: FormInfo) -> FormField | None:
    """The control that runs the search, or None when every candidate looks unsafe."""
    best, best_rank = None, 99
    for item in form.submits:
        label = " ".join([item.label, item.name, item.dom_id]).strip()
        if _UNSAFE_SUBMIT.search(label):
            continue
        words = set(tokens(label))
        rank = 0 if words & {"search", "find", "go", "show", "view", "filter", "get", "list", "submit"} else 1
        if rank < best_rank:
            best, best_rank = item, rank
    return best


@dataclass(slots=True)
class Fill:
    field_id: str
    kind: Literal["fill", "select"]
    value: str
    why: str


_FROM = {"from", "start", "begin", "since", "after", "min"}
_TO = {"to", "end", "until", "till", "through", "before", "max"}
_DATE = {"date", "dt", "issued", "issue", "published", "period"}
_KEYWORD = {"q", "query", "search", "keyword", "keywords", "title", "subject", "text", "term", "find", "name"}
_ANY = re.compile(r"^\W*(?:all|any|select|choose|--+|none)\b", re.I)


def format_date(value: date, field: FormField) -> str:
    """A date written the way the field asks (input type, placeholder or pattern), else ISO."""
    if field.type == "date":
        return value.isoformat()
    hint = f"{field.placeholder} {field.pattern}".casefold()
    match = re.search(r"(dd|mm|yyyy|yy)([^a-z0-9]?)(dd|mm|yyyy|yy)\2?(dd|mm|yyyy|yy)?", hint)
    if match:
        separator = match.group(2) or ""
        parts = [part for part in (match.group(1), match.group(3), match.group(4)) if part]
        rendered = {"dd": f"{value.day:02d}", "mm": f"{value.month:02d}", "yyyy": f"{value.year}",
                    "yy": f"{value.year % 100:02d}"}
        return separator.join(rendered[part] for part in parts)
    return value.isoformat()


def plan_fills(form: FormInfo, *, window: tuple[date, date] | None = None,
               keywords: list[str] | None = None, places: list[str] | None = None) -> list[Fill]:
    """Values for a query form's fields, from the request's constraints only.

    Dates come from the time window, a search box from the subject keywords, a
    drop-down from an option that names a requested keyword or place. Every
    other field keeps the site's own default, so nothing is guessed.
    """
    fills: list[Fill] = []
    keywords = [word for word in (keywords or []) if word]
    wanted = [term.casefold() for term in [*keywords, *(places or [])] if term]
    keyword_fields = [item for item in form.controls if item.type in {"text", "search"} and item.words & _KEYWORD]
    if not keyword_fields:
        boxes = [item for item in form.controls if item.type in {"text", "search"}]
        keyword_fields = boxes if len(boxes) == 1 and not boxes[0].words & _DATE else []
    for item in form.controls:
        if item.type in {"date", "datetime-local"} or (item.type in {"text", ""} and item.words & _DATE):
            if window is None:
                continue
            if item.words & _FROM:
                fills.append(Fill(item.id, "fill", format_date(window[0], item), "start of the requested period"))
            elif item.words & _TO:
                fills.append(Fill(item.id, "fill", format_date(window[1], item), "end of the requested period"))
        elif item.tag == "select":
            match = next((option for option in item.options if option["text"] and not _ANY.match(option["text"])
                          and any(term in option["text"].casefold() for term in wanted)), None)
            if match:
                fills.append(Fill(item.id, "select", match["text"], "option names a requested term"))
        elif item in keyword_fields and keywords:
            fills.append(Fill(item.id, "fill", " ".join(keywords[:4]), "subject keywords"))
    return fills
