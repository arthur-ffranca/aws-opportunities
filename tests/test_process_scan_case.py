import importlib.util
import unittest
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = DEMO_ROOT / "process-scan-case.py"

spec = importlib.util.spec_from_file_location("process_scan_case", MODULE_PATH)
process_scan_case = importlib.util.module_from_spec(spec)
spec.loader.exec_module(process_scan_case)


class PotentialGainTest(unittest.TestCase):
    def test_missing_assortment_estimates_monthly_revenue_per_equivalent_store(self):
        rows = [
            [
                "Produto",
                "Marca",
                "Fabricante",
                "Imp Cliente",
                "Imp Concorrencia",
                "Unid Cliente",
                "Unid Concorrencia",
                "Preco Cliente",
                "Preco Concorrencia",
                "Lojas Cliente",
                "Lojas Concorrencia",
            ],
            ["CHOC TESTE", "MARCA", "FAB", "", "0.5", "", "120", "", "4.50", "0", "0.75"],
        ]

        scored = process_scan_case.score_rows(rows)

        self.assertEqual(scored[0]["acao_recomendada"], "INCLUIR NO SORTIMENTO")
        self.assertEqual(scored[0]["potencial_receita_mensal"], 405.0)
        self.assertIn("potencial", scored[0]["racional"].lower())

    def test_fallback_insights_call_out_total_and_top_potential(self):
        summary = {
            "products": 1,
            "brands": 1,
            "manufacturers": 1,
            "potential_revenue_total": 405.0,
            "top_10": [
                {
                    "rank": 1,
                    "produto": "CHOC TESTE",
                    "acao": "INCLUIR NO SORTIMENTO",
                    "score": 55,
                    "potencial_receita_mensal": 405.0,
                    "racional": "Produto alerta com potencial.",
                }
            ],
            "top_potential": [
                {
                    "rank": 1,
                    "produto": "CHOC TESTE",
                    "acao": "INCLUIR NO SORTIMENTO",
                    "score": 55,
                    "potencial_receita_mensal": 405.0,
                }
            ],
        }

        insights = process_scan_case.fallback_insights(summary)

        self.assertIn("R$ 405.0/mes por loja-equivalente", insights)
        self.assertIn("Top Potencial", insights)

    def test_action_metric_suffix_creates_datadog_safe_metric_suffix(self):
        self.assertEqual(
            process_scan_case.action_metric_suffix("REVER PRECO"),
            "rever_preco",
        )

    def test_summary_exposes_client_vs_competitor_comparison_by_action(self):
        rows = [
            [
                "Produto",
                "Marca",
                "Fabricante",
                "Imp Cliente",
                "Imp Concorrencia",
                "Unid Cliente",
                "Unid Concorrencia",
                "Preco Cliente",
                "Preco Concorrencia",
                "Lojas Cliente",
                "Lojas Concorrencia",
            ],
            ["CHOC A", "MARCA", "FAB", "0.1", "0.2", "100", "150", "5", "4", "1", "1"],
            ["CHOC B", "MARCA", "FAB", "0.1", "0.2", "50", "100", "6", "5", "1", "1"],
        ]

        scored = process_scan_case.score_rows(rows)
        summary = process_scan_case.build_summary(scored, "s3://raw/file.xlsx", "s3://curated/out.csv")
        rever_preco = summary["comparison_by_action"]["REVER PRECO"]

        self.assertEqual(rever_preco["count"], 2)
        self.assertEqual(rever_preco["price_client_avg"], 5.5)
        self.assertEqual(rever_preco["price_competitor_avg"], 4.5)
        self.assertEqual(rever_preco["units_client_avg"], 75.0)
        self.assertEqual(rever_preco["units_competitor_avg"], 125.0)
        self.assertEqual(rever_preco["price_gap_pct_avg"], 22.5)


if __name__ == "__main__":
    unittest.main()
