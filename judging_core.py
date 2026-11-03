"""Pure validation, calculation and CSV export. No UI or database dependencies."""
import csv
import io
import json
import zipfile
from decimal import Decimal, ROUND_HALF_UP

ALGORITHMS = {"criterion_trim_v1": "По критериям, снятие крайних при 5+", "legacy_total_trim_v1": "Прежний: по сумме, снятие при 3+"}


def validate_config(config):
    if not isinstance(config, dict) or set(config) != set("1234567"):
        raise ValueError("Необходимо семь критериев с ID 1..7")
    for c in config.values():
        if not isinstance(c, dict) or not isinstance(c.get("title"), str) or not c["title"].strip() or type(c.get("max")) is not int or c["max"] <= 0:
            raise ValueError("У критерия должны быть название и целочисленный положительный максимум")
        if not isinstance(c.get("ranges"), dict) or not c["ranges"] or any(not isinstance(k, str) or not k.isdigit() or not isinstance(v, str) or not v.strip() for k, v in c["ranges"].items()):
            raise ValueError("Некорректные дескрипторы критерия")
    if sum(c["max"] for c in config.values()) != 100:
        raise ValueError("Необходимо 7 критериев с суммой максимумов 100")
    for c in config.values():
        bounds = sorted(map(int, c["ranges"]))
        if bounds[0] != 0 or bounds[-1] > c["max"]:
            raise ValueError("Некорректные границы дескрипторов")


def validate_scores(scores, criteria):
    if set(scores) != set(criteria):
        raise ValueError("Заполните все критерии")
    for k, c in criteria.items():
        v = scores[k]
        if type(v) is not int or not 0 <= v <= c["max"]:
            raise ValueError(f"Недопустимый балл: {c['title']}")


def rounded(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate(snapshot):
    event, judges = snapshot["event"], snapshot["judges"]
    criteria = event["criteria"]
    active = {j["id"] for j in judges if j["active"]}
    result = []
    for p in snapshot["participants"]:
        if p["archived"]:
            continue
        cards = {c["judge_id"]: c for c in snapshot["cards"] if c["participant_id"] == p["id"] and c["judge_id"] in active and c["status"] == "submitted"}
        for c in cards.values():
            validate_scores(c["scores"], criteria)
        totals = {j: sum(c["scores"].values()) for j, c in cards.items()}
        legacy = event["algorithm"] == "legacy_total_trim_v1"
        kept_total = sorted(totals, key=lambda j: (totals[j], j))
        if legacy and len(kept_total) >= 3:
            kept_total = kept_total[1:-1]
        detail, means = [], {}
        for k, crit in criteria.items():
            ordered = sorted(cards, key=lambda j: (cards[j]["scores"][k], j))
            low, high = None, None
            kept = list(ordered)
            if not legacy and len(kept) >= 5:
                low, high = kept[0], kept[-1]
                kept = kept[1:-1]
            elif legacy:
                kept = kept_total
            mean = sum((Decimal(cards[j]["scores"][k]) for j in kept), Decimal(0)) / len(kept) if kept else None
            means[k] = mean
            values = [cards[j]["scores"][k] for j in ordered]
            spread = Decimal(max(values) - min(values)) / crit["max"] if values else Decimal(0)
            for judge in judges:
                if not judge["active"]:
                    continue
                jid = judge["id"]
                decision = "отсутствует" if jid not in cards else "учтена"
                if jid == low:
                    decision = "снята минимальная"
                if jid == high:
                    decision = "снята максимальная"
                if legacy and jid in cards and jid not in kept:
                    decision = "снята по сумме судьи"
                detail.append({"criterion": k, "judge": jid, "value": cards[jid]["scores"][k] if jid in cards else None, "decision": decision, "mean": mean, "spread": spread, "discussion": event.get("threshold") is not None and spread > Decimal(str(event["threshold"]))})
        total = rounded(sum(means.values(), Decimal(0))) if cards else None
        result.append({"participant": p, "means": means, "total": total, "count": len(cards), "expected": len(active), "complete": bool(active) and len(cards) == len(active), "detail": detail, "rank": None})
    result.sort(key=lambda r: (r["total"] is None, -(r["total"] or Decimal(0)), r["participant"]["number"]))
    if event["status"] in ("closed", "approved") and all(r["complete"] for r in result):
        previous, rank = None, None
        for i, row in enumerate(result, 1):
            if row["total"] != previous:
                rank = i
            row["rank"], previous = rank, row["total"]
    return result


def safe_text(value):
    text = str(value)
    check = text.lstrip(" \t\r\n\ufeff")
    return "'" + text if check.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def csv_bytes(headers, rows, universal=False):
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter="," if universal else ";", lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        cells = []
        for v in row:
            if v is None:
                cells.append("")
            elif isinstance(v, Decimal):
                s = str(rounded(v))
                cells.append(s if universal else s.replace(".", ","))
            elif isinstance(v, (int, float)):
                cells.append(v)
            else:
                cells.append(safe_text(v))
        writer.writerow(cells)
    return out.getvalue().encode("utf-8-sig")


def export_files(snapshot, universal=False):
    event = snapshot["event"]
    results = calculate(snapshot)
    criteria = event["criteria"]
    keys = list(criteria)
    summary, scores, detail = [], [], []
    for r in results:
        p = r["participant"]
        summary.append([event["id"], event["name"], event["status"], p["id"], p["number"], p["title"], ", ".join(p["members"]), p["work_type"], p["stage"], r["rank"], r["expected"], r["count"], "полная" if r["complete"] else "неполная", *[r["means"][k] for k in keys], r["total"], 0, r["total"], event["algorithm"], event["config_version"], snapshot["id"], snapshot["created_at"]])
        for j in snapshot["judges"]:
            if not j["active"]:
                continue
            c = next((c for c in snapshot["cards"] if c["judge_id"] == j["id"] and c["participant_id"] == p["id"]), None)
            scores.append([event["id"], p["id"], p["number"], p["title"], j["id"], j["number"], c["status"] if c else "не заполнена", *[c["scores"].get(k) if c else None for k in keys], sum(c["scores"].values()) if c and c["scores"] else None, c["comment"] if c else "", c["version"] if c else None, c["updated_at"] if c else "", c["submitted_at"] if c else "", "да" if c and c["status"] == "submitted" else "нет"])
        for d in r["detail"]:
            crit = criteria[d["criterion"]]
            detail.append([event["id"], p["id"], d["criterion"], crit["title"], crit["max"], d["judge"], d["value"], d["decision"], "по сумме судьи" if event["algorithm"].startswith("legacy") and d["decision"] != "учтена" else "", d["mean"], d["spread"], "да" if d["discussion"] else "нет"])
    files = {
        "summary.csv": csv_bytes(["Мероприятие_ID", "Мероприятие", "Статус_протокола", "Работа_ID", "Номер", "Команда_или_работа", "Участники", "Тип_работы", "Стадия", "Место", "Судей_ожидается", "Судей_отправили", "Полнота", *[f"Критерий_{k}" for k in keys], "Итог_до_штрафов", "Штраф", "Итог", "Алгоритм", "Версия_методики", "Снимок_ID", "Сформировано"], summary, universal),
        "scores.csv": csv_bytes(["Мероприятие_ID", "Работа_ID", "Номер", "Команда_или_работа", "Судья_ID", "Судья_номер", "Статус_карточки", *[f"Критерий_{k}" for k in keys], "Сумма_судьи", "Комментарий", "Версия_карточки", "Сохранено", "Отправлено", "Допущен_к_расчёту"], scores, universal),
        "criteria.csv": csv_bytes(["Мероприятие_ID", "Работа_ID", "Критерий_ID", "Критерий", "Максимум", "Судья_ID", "Исходный_балл", "Решение", "Причина_исключения", "Агрегат_критерия", "Разброс", "Требует_обсуждения"], detail, universal),
        "metadata.json": json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    files["protocol.zip"] = out.getvalue()
    return files
