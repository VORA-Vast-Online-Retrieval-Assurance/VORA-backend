from unittest import TestCase

from vora.extraction.noise import assess
from vora.extraction.semantics import ConceptMatcher, concept_spec, time_field_names, value_kind


class ConceptMatchingTests(TestCase):
    def match(self, concept: str, fields: dict[str, str], aliases=()):
        spec = concept_spec(concept, aliases=aliases)
        return ConceptMatcher([spec]).match(fields, spec)

    def test_alias_field_receives_semantic_credit_for_price(self) -> None:
        match = self.match("price", {"year": "2024", "average_pack_price": "$115/kWh"})
        self.assertEqual(match.field, "average_pack_price")
        self.assertGreaterEqual(match.credit, 0.9)

    def test_lexicon_synonyms_across_domains(self) -> None:
        cases = [
            ("price", {"msrp": "$42,990"}),
            ("price", {"cost_per_unit": "$3.10"}),
            ("yield", {"production_yield": "3.9 t/ha"}),
            ("yield", {"harvest_yield_t_ha": "4.1"}),
            ("geography", {"province": "Ontario"}),
            ("revenue", {"net_sales_usd_bn": "211.9"}),
            ("rate", {"unemployment_pct": "4.1%"}),
        ]
        for concept, fields in cases:
            with self.subTest(concept=concept, fields=fields):
                match = self.match(concept, fields)
                self.assertIsNotNone(match)
                self.assertGreaterEqual(match.credit, 0.8)

    def test_related_and_value_type_credit_is_partial(self) -> None:
        generic = self.match("price", {"column_2": "$137"})
        self.assertEqual(generic.via, "value_type")
        self.assertLess(generic.credit, 0.8)
        related = self.match("price", {"amount": "1,200"})
        self.assertLess(related.credit, 0.8)

    def test_misspelt_concept_is_corrected(self) -> None:
        spec = concept_spec("yeild")
        self.assertEqual(spec.name, "yield")

    def test_fuzzy_lexicon_lookup_only_corrects_typos(self) -> None:
        from vora.extraction.semantics import token_class
        self.assertEqual(token_class("yeild"), "yield")
        self.assertIsNone(token_class("heading"))  # not a typo of "reading"

    def test_specific_class_wins_over_generic(self) -> None:
        self.assertEqual(concept_spec("yield_quantity").name, "yield")
        self.assertEqual(concept_spec("price_usd").name, "price")

    def test_unknown_domain_concept_matches_literally(self) -> None:
        match = self.match("shipments", {"gpu_shipments_millions": "12.4"})
        self.assertGreaterEqual(match.credit, 0.8)

    def test_unrelated_fields_get_no_credit(self) -> None:
        self.assertIsNone(self.match("price", {"author": "Jane Doe", "title": "About"}))

    def test_time_fields_by_name_and_value(self) -> None:
        self.assertEqual(time_field_names({"year": "2024", "price": "$1"}), ["year"])
        self.assertEqual(time_field_names({"column_1": "2019", "column_2": "5"}), ["column_1"])
        self.assertEqual(time_field_names({"last_updated": "2026-01-01"}), [])

    def test_value_kinds(self) -> None:
        self.assertEqual(value_kind("$115/kWh"), "money")
        self.assertEqual(value_kind("down 20%"), "percent")
        self.assertEqual(value_kind("4.1 t/ha"), "number")
        self.assertEqual(value_kind("2024"), "period")
        self.assertEqual(value_kind("Figure 1."), "text")
        self.assertEqual(value_kind("Model 3"), "text")


class NoiseTests(TestCase):
    def test_security_verification_page_is_challenge(self) -> None:
        verdict = assess({"title": "Just a moment...", "text": "Performing security verification. "
                          "This website uses a security service to protect against malicious bots."},
                         "page_summary")
        self.assertEqual(verdict.role, "challenge")

    def test_navigation_links_are_rejected(self) -> None:
        for fields in ({"url": "/about", "text": "About"}, {"name": "Database Updates", "url": "#data/PE"},
                       {"url": "#chapter-1", "text": "1 Economic dimensions of agriculture"}):
            with self.subTest(fields=fields):
                self.assertEqual(assess(fields, "repeated_region").role, "navigation")

    def test_metadata_blocks_and_property_rows(self) -> None:
        self.assertEqual(assess({"type": "WebSite", "name": "Example", "url": "https://example.com"},
                                "json_ld").role, "metadata")
        self.assertEqual(assess({"type": "Organization", "name": "Example", "url": "https://example.com/"},
                                "json_ld").role, "metadata")
        self.assertEqual(assess({"field": "Last Updated", "value": "July 14, 2026"}, "html_table").role,
                         "metadata")

    def test_interface_widgets_and_consent_banners(self) -> None:
        self.assertEqual(assess({"column_1": "×", "column_4": "search"}, "html_table").role, "noise")
        self.assertEqual(assess({"text": "We use cookies to improve your experience. Accept all cookies"},
                                "repeated_region").role, "noise")

    def test_real_data_is_not_noise(self) -> None:
        verdict = assess({"year": "2024", "average_pack_price": "$115/kWh", "change": "down 20%"}, "html_table")
        self.assertEqual(verdict.role, "data")
        self.assertEqual(verdict.probability, 0.0)
        # Company lists are data, not publisher metadata.
        self.assertEqual(assess({"type": "Organization", "name": "Acme", "url": "https://acme.test/about",
                                 "revenue": "$2.1B"}, "json_ld").role, "data")
