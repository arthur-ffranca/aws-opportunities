import argparse
import csv
import json
import math
import re
import socket
import textwrap
import subprocess
import sys
import zipfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = {
    "r": "http://schemas.openxmlformats.org/package/2006/relationships",
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


class Telemetry:
    def __init__(self, host, port, service_name, events_path):
        self.host = host
        self.port = port
        self.default_tags = [
            f"service:{service_name}",
            "env:localstack",
            "component:worker",
        ]
        self.events_path = Path(events_path)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def _tag(self, value):
        return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_:\\.-]+", "_", str(value).lower())).strip("_")

    def metric(self, name, value, kind, tags=None):
        all_tags = [self._tag(tag) for tag in [*self.default_tags, *(tags or [])] if tag]
        line = f"{name}:{value}|{kind}|#{','.join(all_tags)}"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.2)
            sock.sendto(line.encode("utf-8"), (self.host, self.port))
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def increment(self, name, value=1, tags=None):
        self.metric(name, value, "c", tags)

    def gauge(self, name, value, tags=None):
        self.metric(name, value, "g", tags)

    def timing(self, name, value, tags=None):
        self.metric(name, value, "ms", tags)

    def event(self, name, payload=None, tags=None):
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "name": name,
            "tags": [self._tag(tag) for tag in [*self.default_tags, *(tags or [])] if tag],
            **(payload or {}),
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_aws(endpoint, *args):
    command = [
        "aws",
        "--cli-connect-timeout",
        "5",
        "--cli-read-timeout",
        "15",
        f"--endpoint-url={endpoint}",
        *args,
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"AWS CLI falhou: {' '.join(command)}\n{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def run_aws_with_timeout(endpoint, timeout_seconds, *args):
    command = [
        "aws",
        "--cli-connect-timeout",
        "5",
        "--cli-read-timeout",
        str(timeout_seconds),
        f"--endpoint-url={endpoint}",
        *args,
    ]
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"AWS CLI falhou: {' '.join(command)}\n{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def col_to_index(cell_ref):
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    total = 0
    for ch in letters:
        total = total * 26 + (ord(ch) - 64)
    return total - 1


def read_first_sheet(path):
    with zipfile.ZipFile(path) as zf:
        strings = []
        try:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("a:si", NS):
                strings.append("".join(node.text or "" for node in si.findall(".//a:t", NS)))
        except KeyError:
            pass

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            rel.attrib["Id"]: rel.attrib["Target"].replace("../", "")
            for rel in rels.findall("r:Relationship", REL_NS)
        }
        sheet = workbook.find("a:sheets/a:sheet", REL_NS)
        rel_id = sheet.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        target = rel_map[rel_id]
        if not target.startswith("xl/"):
            target = "xl/" + target

        root = ET.fromstring(zf.read(target))
        rows = []
        max_cols = 0
        for row in root.findall("a:sheetData/a:row", NS):
            values = []
            for cell in row.findall("a:c", NS):
                idx = col_to_index(cell.attrib.get("r", "A1"))
                while len(values) <= idx:
                    values.append("")
                value_node = cell.find("a:v", NS)
                inline_node = cell.find("a:is/a:t", NS)
                if inline_node is not None:
                    value = inline_node.text or ""
                elif value_node is None:
                    value = ""
                else:
                    raw = value_node.text or ""
                    value = strings[int(raw)] if cell.attrib.get("t") == "s" else raw
                values[idx] = value
            max_cols = max(max_cols, len(values))
            rows.append(values)

        for row in rows:
            row.extend([""] * (max_cols - len(row)))
        return [row for row in rows if any(str(value).strip() for value in row)]


def number(value):
    if value in ("", None):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return None if math.isnan(parsed) else parsed


def safe_ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def as_pct(value):
    return None if value is None else round(value * 100, 2)


def as_money(value):
    return None if value is None else round(value, 2)


def action_metric_suffix(value):
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value).lower())).strip("_")


def classify(row):
    client_missing = row["imp_cliente"] is None or (row["lojas_cliente"] or 0) == 0
    competitor_has = row["imp_concorrencia"] is not None and (row["lojas_concorrencia"] or 0) > 0
    unit_gap = row["gap_unidades"] or 0
    store_gap = row["gap_lojas"] or 0
    price_gap = row["gap_preco_pct"]

    if client_missing and competitor_has:
        return "INCLUIR NO SORTIMENTO"
    if price_gap is not None and price_gap >= 0.08 and unit_gap > 0:
        return "REVER PRECO"
    if store_gap >= 0.15 and row["gap_importancia"] > 0:
        return "EXPANDIR DISTRIBUICAO"
    if unit_gap >= 50 and row["gap_importancia"] > 0:
        return "ACELERAR GIRO"
    return "MANTER / MONITORAR"


def rationalize(row):
    action = row["acao_recomendada"]
    potential = as_money(row.get("potencial_receita_mensal") or 0)
    potential_text = f" Potencial estimado: R$ {potential}/mes por loja-equivalente."
    if action == "INCLUIR NO SORTIMENTO":
        return (
            f"Concorrencia vende em {as_pct(row['lojas_concorrencia'])}% das lojas "
            f"e o cliente nao vende; oportunidade de sortimento."
            f"{potential_text}"
        )
    if action == "REVER PRECO":
        return (
            f"Cliente esta {as_pct(row['gap_preco_pct'])}% acima do preco concorrente "
            f"e vende {as_money(row['gap_unidades'])} unid/loja/mes a menos."
            f"{potential_text}"
        )
    if action == "EXPANDIR DISTRIBUICAO":
        return (
            f"Concorrencia tem {as_pct(row['gap_lojas'])} p.p. a mais de presenca "
            f"e maior importancia no mix."
            f"{potential_text}"
        )
    if action == "ACELERAR GIRO":
        return (
            f"Produto existe nos dois, mas concorrencia gira "
            f"{as_money(row['gap_unidades'])} unid/loja/mes a mais."
            f"{potential_text}"
        )
    return "Produto sem gap prioritario; manter acompanhamento."


def estimate_monthly_potential(row):
    action = row["acao_recomendada"]
    competitor_units = row["unid_concorrencia"] or 0
    competitor_price = row["preco_concorrencia"] or row["preco_cliente"] or 0
    unit_gap = max(row["gap_unidades"] or 0, 0)
    store_gap = max(row["gap_lojas"] or 0, 0)
    competitor_coverage = min(max(row["lojas_concorrencia"] or 0, 0), 1)

    if action == "INCLUIR NO SORTIMENTO":
        potential = competitor_units * competitor_price * competitor_coverage
    elif action in ("REVER PRECO", "ACELERAR GIRO"):
        potential = unit_gap * competitor_price * (competitor_coverage or 1)
    elif action == "EXPANDIR DISTRIBUICAO":
        potential = competitor_units * competitor_price * store_gap
    else:
        potential = 0

    return round(potential, 2)


def avg(values):
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def build_action_comparisons(rows):
    comparisons = {}
    for action in sorted({row["acao_recomendada"] for row in rows}):
        action_rows = [row for row in rows if row["acao_recomendada"] == action]
        comparisons[action] = {
            "count": len(action_rows),
            "price_client_avg": avg(row["preco_cliente"] for row in action_rows),
            "price_competitor_avg": avg(row["preco_concorrencia"] for row in action_rows),
            "units_client_avg": avg(row["unid_cliente"] for row in action_rows),
            "units_competitor_avg": avg(row["unid_concorrencia"] for row in action_rows),
            "price_gap_pct_avg": as_pct(avg(row["gap_preco_pct"] for row in action_rows)),
            "units_gap_avg": avg(row["gap_unidades"] for row in action_rows),
            "store_presence_client_avg": as_pct(avg(row["lojas_cliente"] for row in action_rows)),
            "store_presence_competitor_avg": as_pct(avg(row["lojas_concorrencia"] for row in action_rows)),
        }
    return comparisons


def score_rows(rows):
    parsed = []
    for source in rows[1:]:
        if len(source) < 11:
            continue
        item = {
            "produto": source[0],
            "marca": source[1],
            "fabricante": source[2],
            "imp_cliente": number(source[3]),
            "imp_concorrencia": number(source[4]),
            "unid_cliente": number(source[5]),
            "unid_concorrencia": number(source[6]),
            "preco_cliente": number(source[7]),
            "preco_concorrencia": number(source[8]),
            "lojas_cliente": number(source[9]),
            "lojas_concorrencia": number(source[10]),
        }
        item["gap_importancia"] = (item["imp_concorrencia"] or 0) - (item["imp_cliente"] or 0)
        item["gap_unidades"] = (item["unid_concorrencia"] or 0) - (item["unid_cliente"] or 0)
        item["gap_lojas"] = (item["lojas_concorrencia"] or 0) - (item["lojas_cliente"] or 0)
        price_ratio = safe_ratio(item["preco_cliente"], item["preco_concorrencia"])
        item["gap_preco_pct"] = None if price_ratio is None else price_ratio - 1
        parsed.append(item)

    max_importance = max((row["imp_concorrencia"] or 0 for row in parsed), default=1) or 1
    max_unit_gap = max((max(row["gap_unidades"], 0) for row in parsed), default=1) or 1

    for row in parsed:
        row["acao_recomendada"] = classify(row)
        row["potencial_receita_mensal"] = estimate_monthly_potential(row)
        importance_score = ((row["imp_concorrencia"] or 0) / max_importance) * 35
        unit_score = (max(row["gap_unidades"], 0) / max_unit_gap) * 25
        store_score = max(row["gap_lojas"], 0) * 20
        price_score = 0
        if row["gap_preco_pct"] is not None and row["gap_preco_pct"] > 0:
            price_score = min(row["gap_preco_pct"], 0.5) / 0.5 * 15
        missing_boost = 20 if row["acao_recomendada"] == "INCLUIR NO SORTIMENTO" else 0
        row["score_oportunidade"] = round(
            min(100, importance_score + unit_score + store_score + price_score + missing_boost),
            2,
        )
        row["racional"] = rationalize(row)

    parsed.sort(key=lambda item: item["score_oportunidade"], reverse=True)
    for idx, row in enumerate(parsed, start=1):
        row["rank"] = idx
    return parsed


def export_csv(rows, output_path):
    fieldnames = [
        "rank",
        "acao_recomendada",
        "score_oportunidade",
        "produto",
        "marca",
        "fabricante",
        "racional",
        "importancia_cliente_pct",
        "importancia_concorrencia_pct",
        "unidades_cliente",
        "unidades_concorrencia",
        "preco_cliente",
        "preco_concorrencia",
        "lojas_cliente_pct",
        "lojas_concorrencia_pct",
        "gap_unidades",
        "gap_lojas_pp",
        "gap_preco_pct",
        "potencial_receita_mensal",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "rank": row["rank"],
                    "acao_recomendada": row["acao_recomendada"],
                    "score_oportunidade": row["score_oportunidade"],
                    "produto": row["produto"],
                    "marca": row["marca"],
                    "fabricante": row["fabricante"],
                    "racional": row["racional"],
                    "importancia_cliente_pct": as_pct(row["imp_cliente"]),
                    "importancia_concorrencia_pct": as_pct(row["imp_concorrencia"]),
                    "unidades_cliente": as_money(row["unid_cliente"]),
                    "unidades_concorrencia": as_money(row["unid_concorrencia"]),
                    "preco_cliente": as_money(row["preco_cliente"]),
                    "preco_concorrencia": as_money(row["preco_concorrencia"]),
                    "lojas_cliente_pct": as_pct(row["lojas_cliente"]),
                    "lojas_concorrencia_pct": as_pct(row["lojas_concorrencia"]),
                    "gap_unidades": as_money(row["gap_unidades"]),
                    "gap_lojas_pp": as_pct(row["gap_lojas"]),
                    "gap_preco_pct": as_pct(row["gap_preco_pct"]),
                    "potencial_receita_mensal": as_money(row["potencial_receita_mensal"]),
                }
            )


def build_summary(rows, raw_uri, curated_csv_uri):
    action_counts = {}
    action_potential = {}
    for row in rows:
        action_counts[row["acao_recomendada"]] = action_counts.get(row["acao_recomendada"], 0) + 1
        action_potential[row["acao_recomendada"]] = round(
            action_potential.get(row["acao_recomendada"], 0) + row["potencial_receita_mensal"],
            2,
        )
    total_potential = round(sum(row["potencial_receita_mensal"] for row in rows), 2)
    return {
        "source": raw_uri,
        "curated_csv": curated_csv_uri,
        "products": len(rows),
        "brands": len({row["marca"] for row in rows if row["marca"]}),
        "manufacturers": len({row["fabricante"] for row in rows if row["fabricante"]}),
        "action_counts": action_counts,
        "potential_revenue_total": total_potential,
        "potential_revenue_by_action": action_potential,
        "comparison_by_action": build_action_comparisons(rows),
        "top_10": [
            {
                "rank": row["rank"],
                "produto": row["produto"],
                "acao": row["acao_recomendada"],
                "score": row["score_oportunidade"],
                "potencial_receita_mensal": row["potencial_receita_mensal"],
                "racional": row["racional"],
            }
            for row in rows[:10]
        ],
        "top_potential": [
            {
                "rank": row["rank"],
                "produto": row["produto"],
                "acao": row["acao_recomendada"],
                "potencial_receita_mensal": row["potencial_receita_mensal"],
                "score": row["score_oportunidade"],
            }
            for row in sorted(rows, key=lambda item: item["potencial_receita_mensal"], reverse=True)[:10]
            if row["potencial_receita_mensal"] > 0
        ],
    }


def fallback_insights(summary):
    top = summary["top_10"][:5]
    top_potential = summary.get("top_potential", [])[:5]
    lines = [
        "# Bedrock Insights",
        "",
        "Modo fallback local: Bedrock nao retornou resposta nesta execucao.",
        "",
        "## Leitura Comercial",
        "",
        (
            f"A base tem {summary['products']} produtos, {summary['brands']} marcas "
            f"e {summary['manufacturers']} fabricantes."
        ),
        (
            "Potencial estimado total: R$ "
            f"{as_money(summary['potential_revenue_total'])}/mes por loja-equivalente."
        ),
        "As maiores oportunidades estao em sortimento ausente, preco acima da concorrencia e menor giro por loja.",
        "",
        "## Top Prioridades",
        "",
    ]
    for item in top:
        lines.append(
            f"- #{item['rank']} {item['produto']}: {item['acao']} "
            f"(score {item['score']}, potencial R$ {as_money(item['potencial_receita_mensal'])}/mes). "
            f"{item['racional']}"
        )
    if top_potential:
        lines.extend(["", "## Top Potencial", ""])
        for item in top_potential:
            lines.append(
                f"- #{item['rank']} {item['produto']}: R$ "
                f"{as_money(item['potencial_receita_mensal'])}/mes por loja-equivalente "
                f"({item['acao']})."
            )
    return "\n".join(lines) + "\n"


def bedrock_insights(endpoint, model_id, timeout_seconds, summary, json_dir):
    prompt = textwrap.dedent(
        f"""
        Voce e um analista comercial de varejo alimentar usando dados de sortimento,
        preco, distribuicao e giro contra concorrencia.

        Gere insights objetivos em portugues para um demo AWS. Seja pratico:
        1. resumo em 3 bullets;
        2. prioridades comerciais;
        3. riscos ou cuidados antes de acionar o cliente;
        4. proximas acoes sugeridas.

        Use somente os dados abaixo. Nao invente margem, faturamento ou estoque.
        Quando falar de ganho, chame de potencial estimado mensal por loja-equivalente.

        Dados:
        {json.dumps(summary, ensure_ascii=False)}
        """
    ).strip()

    body_path = json_dir / "bedrock-invoke-body.json"
    output_path = json_dir / "bedrock-invoke-output.json"
    prompt = (
        "<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n"
        + prompt
        + "\n<|eot_id|>\n<|start_header_id|>assistant<|end_header_id|>"
    )
    body_path.write_text(
        json.dumps(
            {
                "prompt": prompt,
                "max_gen_len": 700,
                "temperature": 0.2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    run_aws_with_timeout(
        endpoint,
        timeout_seconds,
        "bedrock-runtime",
        "invoke-model",
        "--region",
        "us-east-1",
        "--model-id",
        model_id,
        "--body",
        f"file://{body_path}",
        "--cli-binary-format",
        "raw-in-base64-out",
        str(output_path),
    )

    response = json.loads(output_path.read_text(encoding="utf-8"))
    text = (
        response.get("generation")
        or response.get("outputText")
        or response.get("completion")
        or response.get("text")
    )
    if not text and response.get("outputs"):
        first = response["outputs"][0]
        text = first.get("text") or first.get("generation")
    if not text:
        raise RuntimeError("Bedrock respondeu sem texto de insight.")
    return str(text).strip() + "\n"


def dynamodb_item(row):
    return {
        "produto": {"S": str(row["produto"])},
        "rank": {"N": str(row["rank"])},
        "acao": {"S": row["acao_recomendada"]},
        "score": {"N": str(row["score_oportunidade"])},
        "marca": {"S": row["marca"] or ""},
        "fabricante": {"S": row["fabricante"] or ""},
        "racional": {"S": row["racional"]},
        "potencial_receita_mensal": {"N": str(row["potencial_receita_mensal"])},
    }


def put_json_item(endpoint, table, item, path):
    path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    run_aws(endpoint, "dynamodb", "put-item", "--table-name", table, "--item", f"file://{path}")


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--raw-bucket", required=True)
    parser.add_argument("--raw-key", required=True)
    parser.add_argument("--curated-bucket", required=True)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--top-n-dynamodb", type=int, default=50)
    parser.add_argument("--use-bedrock", action="store_true")
    parser.add_argument(
        "--bedrock-model-id",
        default="meta.llama3-8b-instruct-v1:0",
    )
    parser.add_argument("--bedrock-timeout-seconds", type=int, default=20)
    parser.add_argument("--datadog-host", default="127.0.0.1")
    parser.add_argument("--datadog-port", type=int, default=8125)
    parser.add_argument("--service-name", default="scan-opportunity-radar")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    data_dir = work_dir / "data"
    out_dir = work_dir / "curated"
    json_dir = work_dir / "json"
    observability_dir = work_dir / "observability"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    telemetry = Telemetry(
        args.datadog_host,
        args.datadog_port,
        args.service_name,
        observability_dir / "events.jsonl",
    )
    telemetry.increment("scan.radar.worker.started")
    telemetry.event("worker.started", {"raw_bucket": args.raw_bucket, "raw_key": args.raw_key})

    input_xlsx = data_dir / "scan-case-input.xlsx"
    raw_uri = f"s3://{args.raw_bucket}/{args.raw_key}"
    run_aws(args.endpoint, "s3", "cp", raw_uri, str(input_xlsx))

    rows = score_rows(read_first_sheet(input_xlsx))
    telemetry.gauge("scan.radar.worker.products", len(rows))
    csv_path = out_dir / "scan_product_recommendations.csv"
    summary_path = out_dir / "scan_demo_summary.json"
    insights_path = out_dir / "bedrock_insights.md"
    export_csv(rows, csv_path)

    curated_csv_uri = "s3://{}/recommendations/scan_product_recommendations.csv".format(
        args.curated_bucket
    )
    summary = build_summary(rows, raw_uri, curated_csv_uri)
    summary["bedrock_model_id"] = args.bedrock_model_id
    summary["bedrock_status"] = "not_requested"
    for action, count in summary["action_counts"].items():
        telemetry.gauge("scan.radar.worker.action_count", count, [f"action:{action}"])
    telemetry.gauge("scan.radar.worker.potential_revenue_total", summary["potential_revenue_total"])
    for action, potential in summary["potential_revenue_by_action"].items():
        telemetry.gauge("scan.radar.worker.potential_revenue", potential, [f"action:{action}"])
        telemetry.gauge(f"scan.radar.worker.potential_revenue.{action_metric_suffix(action)}", potential)

    if args.use_bedrock:
        try:
            insights = bedrock_insights(
                args.endpoint,
                args.bedrock_model_id,
                args.bedrock_timeout_seconds,
                summary,
                json_dir,
            )
            summary["bedrock_status"] = "ok"
            telemetry.increment("scan.radar.bedrock.ok")
        except Exception as exc:
            summary["bedrock_status"] = f"fallback: {exc}"
            telemetry.increment("scan.radar.bedrock.fallback")
            insights = fallback_insights(summary)
    else:
        telemetry.increment("scan.radar.bedrock.skipped")
        insights = fallback_insights(summary)

    insights_path.write_text(insights, encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    run_aws(args.endpoint, "s3", "cp", str(csv_path), curated_csv_uri)
    run_aws(
        args.endpoint,
        "s3",
        "cp",
        str(summary_path),
        f"s3://{args.curated_bucket}/summary/scan_demo_summary.json",
    )
    run_aws(
        args.endpoint,
        "s3",
        "cp",
        str(insights_path),
        f"s3://{args.curated_bucket}/insights/bedrock_insights.md",
    )

    for row in rows[: args.top_n_dynamodb]:
        safe_rank = str(row["rank"]).zfill(3)
        put_json_item(
            args.endpoint,
            args.table_name,
            dynamodb_item(row),
            json_dir / f"recommendation-{safe_rank}.json",
        )

    summary_item = {
        "produto": {"S": "__SUMMARY__"},
        "rank": {"N": "0"},
        "acao": {"S": "PIPELINE_SUMMARY"},
        "score": {"N": "0"},
        "marca": {"S": ""},
        "fabricante": {"S": ""},
        "racional": {"S": json.dumps(summary, ensure_ascii=False)},
    }
    put_json_item(args.endpoint, args.table_name, summary_item, json_dir / "summary-item.json")

    duration_ms = round((time.perf_counter() - started) * 1000)
    telemetry.timing("scan.radar.worker.duration_ms", duration_ms)
    telemetry.increment("scan.radar.worker.completed")
    telemetry.event(
        "worker.completed",
        {
            "durationMs": duration_ms,
            "products": summary["products"],
            "brands": summary["brands"],
            "manufacturers": summary["manufacturers"],
            "bedrockStatus": summary["bedrock_status"],
            "potentialRevenueTotal": summary["potential_revenue_total"],
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
