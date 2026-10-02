from datetime import date
from unittest import TestCase

from vora.browser.forms import classify_form, format_date, parse_forms, plan_fills, submit_control


def field(index, tag="input", type="text", name="", label="", **extra):
    return {"id": f"0-{index}", "tag": tag, "type": type, "name": name, "domId": name, "label": label,
            "visible": True, **extra}


def form(*fields, text="", method="post"):
    return parse_forms([{"index": 0, "method": method, "action": "", "fields": list(fields), "visible": True,
                         "text": text}])[0]


SEARCH = form(
    field(0, name="txtDateFrom", label="Date from", placeholder="dd/mm/yyyy"),
    field(1, name="txtDateTo", label="Date to", placeholder="dd/mm/yyyy"),
    field(2, tag="select", type="select-one", name="ddlCategory", label="Category",
          options=[{"value": "", "text": "-- Select --"}, {"value": "1", "text": "Extraordinary"},
                   {"value": "2", "text": "Weekly"}]),
    field(3, name="txtKeyword", label="Keyword"),
    field(4, type="submit", name="btnSearch", label="Search"),
)


class FormPolicyTests(TestCase):
    def test_query_forms_are_recognised(self) -> None:
        self.assertEqual(classify_form(SEARCH), "query")
        self.assertEqual(classify_form(form(field(0, type="search", name="q", label="Search"),
                                            field(1, type="submit", label="Go"), method="get")), "query")

    def test_forms_that_change_state_or_send_data_are_never_query_forms(self) -> None:
        cases = {
            "auth": form(field(0, name="user", label="Username"), field(1, type="password", name="pwd"),
                         field(2, type="submit", label="Sign in")),
            "payment": form(field(0, name="cardnumber", label="Card number"), field(1, name="cvv"),
                            field(2, type="submit", label="Pay")),
            "contact": form(field(0, name="email", type="email", label="Email"),
                            field(1, tag="textarea", type="textarea", name="message"),
                            field(2, type="submit", label="Send")),
            "subscribe": form(field(0, name="email", type="email", label="Your email"),
                              field(1, type="submit", label="Subscribe"), text="Subscribe to our newsletter"),
            "upload": form(field(0, type="file", name="attachment"), field(1, type="submit", label="Upload")),
            "challenge": form(field(0, name="txtCaptcha", label="Enter the characters shown"),
                              field(1, name="txtKeyword"), field(2, type="submit", label="Search")),
        }
        for expected, item in cases.items():
            with self.subTest(expected):
                self.assertEqual(classify_form(item), expected)

    def test_submit_control_avoids_unsafe_labels(self) -> None:
        mixed = form(field(0, name="q", type="search"), field(1, type="submit", label="Subscribe"),
                     field(2, type="submit", label="Search"))
        self.assertEqual(submit_control(mixed).label, "Search")
        only_unsafe = form(field(0, name="q", type="search"), field(1, type="submit", label="Send message"))
        self.assertIsNone(submit_control(only_unsafe))

    def test_values_come_from_the_request_only(self) -> None:
        window = (date(2026, 9, 1), date(2026, 9, 30))
        fills = {item.field_id: item for item in plan_fills(SEARCH, window=window, keywords=["gazette"],
                                                            places=["Weekly"])}
        self.assertEqual(fills["0-0"].value, "01/09/2026")           # the field's own date format
        self.assertEqual(fills["0-1"].value, "30/09/2026")
        self.assertEqual((fills["0-2"].kind, fills["0-2"].value), ("select", "Weekly"))
        self.assertEqual(fills["0-3"].value, "gazette")

    def test_without_constraints_nothing_is_guessed(self) -> None:
        self.assertEqual(plan_fills(SEARCH), [])
        self.assertEqual([item.field_id for item in plan_fills(SEARCH, keywords=["gazette"])], ["0-3"])

    def test_date_formats(self) -> None:
        day = date(2026, 3, 7)
        iso = {"type": "date", "placeholder": "", "pattern": ""}
        dashed = {"type": "text", "placeholder": "DD-MM-YYYY", "pattern": ""}
        plain = {"type": "text", "placeholder": "", "pattern": ""}
        from vora.browser.forms import FormField
        make = lambda extra: FormField(id="0-0", tag="input", **extra)  # noqa: E731
        self.assertEqual(format_date(day, make(iso)), "2026-03-07")
        self.assertEqual(format_date(day, make(dashed)), "07-03-2026")
        self.assertEqual(format_date(day, make(plain)), "2026-03-07")


class HintsTests(TestCase):
    def test_hints_come_from_the_request_without_the_named_source(self) -> None:
        from datetime import date as day

        from vora.research.planning.provider import analyze_goal
        from vora.research.reading.deep_lane import build_hints

        plan = analyze_goal("fetch me data from egazette", use_llm=False, today=day(2026, 9, 30))
        hints = build_hints(plan)
        self.assertEqual(hints.keywords, ())            # "egazette" says where to look, not what to search
        self.assertIsNone(hints.window)
        plan = analyze_goal("AI conferences in Europe in 2026", use_llm=False, today=day(2026, 9, 30))
        hints = build_hints(plan, forms=False)
        self.assertEqual(hints.window, (day(2026, 1, 1), day(2026, 12, 31)))
        self.assertIn("conference", hints.keywords)
        self.assertFalse(hints.forms)
