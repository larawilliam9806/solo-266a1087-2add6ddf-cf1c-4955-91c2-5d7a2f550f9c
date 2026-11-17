from __future__ import annotations

import csv
import io
import os
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

import db

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-not-a-secret")

csv.field_size_limit(1024 * 1024)

REQUIRED_COLUMNS = ("record_id", "source", "name", "org")
HEADER_ALIASES = {
    "record_id": {"record_id", "recordid", "id", "唯一记录id", "记录id", "记录编号", "唯一记录编号"},
    "source": {"source", "来源", "数据来源", "系统"},
    "name": {"name", "姓名", "作者姓名", "作者"},
    "org": {"org", "organization", "organisation", "unit", "单位", "机构", "所属单位"},
}
SAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_records.csv")


def init_app() -> None:
    db.init_db()


@app.context_processor
def inject_globals():
    return {"flash_categories": ("success", "error", "warning", "info")}


@app.get("/")
def index():
    search = (request.args.get("q") or "").strip()
    records = db.list_records(search)
    groups = db.group_summary()
    verdicts = db.list_verdicts()
    group_of = {r.record_id: g["group_key"] for g in groups for r in g["records"]}
    return render_template(
        "index.html",
        records=records,
        groups=groups,
        verdicts=verdicts,
        group_of=group_of,
        search=search,
        sel_a=request.args.get("a", ""),
        sel_b=request.args.get("b", ""),
        stats={
            "records": len(group_of),
            "groups": len(groups),
            "same": sum(1 for v in verdicts if v.kind == "same"),
            "different": sum(1 for v in verdicts if v.kind == "different"),
        },
    )


def _safe_back(raw: str | None) -> str:
    """Only follow same-site relative paths, never an attacker-supplied host."""
    if not raw:
        return url_for("index")
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return url_for("index")
    return raw


@app.post("/decide")
def decide():
    a = (request.form.get("a") or "").strip()
    b = (request.form.get("b") or "").strip()
    kind = (request.form.get("kind") or "").strip()
    back = _safe_back(request.form.get("back"))
    try:
        result = db.decide(a, b, kind)
    except db.NotFoundError as exc:
        flash(str(exc), "error")
        return redirect(back)
    except db.ValidationError as exc:
        flash(str(exc), "error")
        return redirect(back)

    if not result["ok"]:
        pairs = "；".join(f"{x} ↔ {y}" for x, y in result["conflict_pairs"])
        if kind == "different":
            flash(
                f"冲突：记录 {a} 与 {b} 目前经由其他“同一人”判定已处于同一分组，"
                f"不能判定为“不同人”。本次判定未保存，原分组保持不变。"
                f"如确需分开，请先撤回相关的“同一人”判定。",
                "error",
            )
        else:
            flash(
                f"冲突：判定 {a} 与 {b} 为“同一人”会使已判定为不同人的记录对 "
                f"[{pairs}] 进入同一分组。本次判定未保存，原分组保持不变。",
                "error",
            )
        return redirect(back)

    flash(f"已保存判定：{result['a']} 与 {result['b']} → {'同一人' if kind == 'same' else '不同人'}", "success")
    return redirect(back)


@app.post("/revoke/<int:verdict_id>")
def revoke(verdict_id: int):
    back = _safe_back(request.form.get("back"))
    try:
        result = db.revoke(verdict_id)
    except db.NotFoundError as exc:
        flash(str(exc), "error")
        return redirect(back)
    if result["already_revoked"]:
        flash(f"判定 #{verdict_id} 此前已撤回。", "info")
    else:
        flash(f"已撤回判定 #{verdict_id}，分组已按其余生效判定重新计算。", "success")
    return redirect(back)


def _parse_csv(text: str) -> tuple[list[db.Record], list[str]]:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], ["文件为空"]
    normalized = [h.strip().lower().lstrip("﻿") for h in header]
    column_for: dict[str, int] = {}
    for field, aliases in HEADER_ALIASES.items():
        for idx, h in enumerate(normalized):
            if h in aliases or h.replace(" ", "_") in aliases:
                column_for[field] = idx
                break
    missing = [f for f in REQUIRED_COLUMNS if f not in column_for]
    if missing:
        return [], [
            "缺少必需列：" + ", ".join(missing)
            + "（可用表头：唯一记录ID/来源/姓名/单位 或 record_id,source,name,org）"
        ]

    records: list[db.Record] = []
    errors: list[str] = []
    for line_no, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue  # blank line

        def cell(field: str) -> str:
            idx = column_for[field]
            return row[idx].strip() if idx < len(row) else ""

        values = {f: cell(f) for f in REQUIRED_COLUMNS}
        empty = [f for f, v in values.items() if not v]
        if empty:
            errors.append(f"第 {line_no} 行缺少值：{', '.join(empty)}")
            continue
        if len(values["record_id"]) > 200:
            errors.append(f"第 {line_no} 行记录ID过长（>200 字符）")
            continue
        records.append(db.Record(**values))
    return records, errors


@app.get("/import")
def import_form():
    return redirect(url_for("index"))


@app.post("/import")
def import_csv():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("请选择要导入的 CSV 文件。", "error")
        return redirect(url_for("index"))

    raw = upload.read()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        flash("无法识别文件编码，请使用 UTF-8（可带 BOM）或 GB18030 保存 CSV。", "error")
        return redirect(url_for("index"))

    records, errors = _parse_csv(text)
    if errors:
        preview = "；".join(errors[:5])
        more = f"（另有 {len(errors) - 5} 处错误）" if len(errors) > 5 else ""
        flash(f"导入失败，未写入任何记录：{preview}{more}", "error")
        return redirect(url_for("index"))
    if not records:
        flash("CSV 中没有可导入的数据行。", "error")
        return redirect(url_for("index"))

    result = db.import_records(records, filename=upload.filename)
    flash(
        f"导入完成：新增 {result['inserted']} 条，更新 {result['updated']} 条，"
        f"文件内重复记录 {result['skipped_duplicates']} 条（以最后一行为准）。",
        "success",
    )
    return redirect(url_for("index"))


@app.get("/sample.csv")
def sample_csv():
    return send_file(SAMPLE_PATH, mimetype="text/csv; charset=utf-8", as_attachment=True,
                     download_name="sample_records.csv")


@app.get("/export.csv")
def export_csv():
    rows = db.export_rows()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["record_id", "group_id"])
    writer.writerows(rows)
    suffix = os.environ.get("EXPORT_FILENAME", "record_group_mapping.csv")
    return Response(
        "﻿" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{suffix}"'},
    )


@app.get("/api/groups")
def api_groups():
    return jsonify(
        {
            "groups": [
                {"group_id": g["group_key"], "record_ids": [r.record_id for r in g["records"]]}
                for g in db.group_summary()
            ],
            "mapping": dict(db.export_rows()),
        }
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.errorhandler(413)
def too_large(_exc):
    flash("文件超过 16 MB 限制。", "error")
    return redirect(url_for("index")), 413


init_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
