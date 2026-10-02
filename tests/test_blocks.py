from unittest import TestCase

from vora.extraction.blocks import apply_block_support, block_support
from vora.shared.contracts import Observation


def scored(block: str, value: str, *, period: str = "2024", status: str = "accepted", coverage: float = 0.9,
           relevance: float = 0.8, method: str = "html_table", url: str = "https://stats.test/page") -> Observation:
    matched = {"price": {"field": "price", "credit": coverage, "via": "exact"}} if coverage >= 0.45 else {}
    return Observation(
        source_url=url, method=method, fields={"price": value, "year": period},
        normalized={"period": period, "price": value}, status=status,
        tier="high" if status == "accepted" else "partial", block_id=block,
        score_breakdown={"coverage": coverage}, relevance_score=relevance, concept_matches=matched,
    )


class BlockSupportTests(TestCase):
    def test_a_block_that_carries_the_data_is_kept(self) -> None:
        rows = [scored("table#1", f"${40 + index},000", period=str(2020 + index)) for index in range(4)]
        self.assertGreater(block_support(rows), 0.8)
        accepted, partial, _ = apply_block_support(rows, [], [])
        self.assertEqual((len(accepted), len(partial)), (4, 0))

    def test_one_passing_row_in_an_unrelated_block_is_demoted(self) -> None:
        lucky = scored("table#2", "$12,000", coverage=0.6, relevance=0.5)
        others = [scored("table#2", "Call us", status="partial", coverage=0.0, relevance=0.1) for _ in range(4)]
        self.assertLess(block_support([lucky, *others]), 0.30)
        accepted, partial, _ = apply_block_support([lucky], others, [])
        self.assertEqual(accepted, [])
        self.assertIn("Block doesn't support the requested data", partial[0].reasons[-1])

    def test_the_same_values_through_two_routes_are_kept_once(self) -> None:
        table = [scored("table#1", f"${40 + index},000", period=str(2020 + index)) for index in range(3)]
        chart = [scored("", f"{40 + index}000", period=str(2020 + index), method="network_json",
                        coverage=0.7, relevance=0.6) for index in range(3)]
        chart = [item.model_copy(update={"block_id": "tab 1/table#1"}) for item in chart]
        accepted, partial, _ = apply_block_support([*table, *chart], [], [])
        self.assertEqual({item.method for item in accepted}, {"html_table"})
        self.assertEqual(len(partial), 3)
        self.assertTrue(all("stronger block" in item.reasons[-1] for item in partial))

    def test_rows_without_a_block_are_untouched(self) -> None:
        sentence = scored("", "$9,000", coverage=0.6, relevance=0.2, method="text_statement")
        self.assertEqual(apply_block_support([sentence], [], []), ([sentence], [], []))
