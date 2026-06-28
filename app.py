from __future__ import annotations

import json
import os
import re
import secrets
import sys
import traceback
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd


APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
OUTPUTS = ROOT / "outputs"
DATA_DIR = APP_DIR / "data"
OUTPUTS.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

AGENT_PASSWORD = os.environ.get("AGENT_PASSWORD", "").strip()
SESSION_COOKIE_NAME = "agent_session"
SESSION_TOKEN = os.environ.get("AGENT_SESSION_TOKEN", secrets.token_urlsafe(32))

DEFAULT_TARGET_KEYWORDS = """hidden camera outlet
ax nanny cam
hidden cameras
wall outlet camera
hidden camera charger
outlet camera
hidden camera 4k
wall outlet camera with audio
b0gqmrpkhw
hidden camera in outlet
spy camera hidden camera
nanny cam outlet
hidden peephole camera
nanny camera
wall outlet hidden camera
b0g5j4fmzd
hidden camera white
b0glg7d81f
plug in hidden camera with audio/video
b0gsg4dshc
secret cameras for spying with audio
hidden camera
nanny cam hidden camera
hidden camera with audio/video
spy camera hidden camera with audio/video
spy camera
nanny cam
camaras espias ocultas
hidden cameras for home
nanny cam hidden camera with audio/video
secret camera"""

DEFAULT_COMPETITOR_ASINS = """B0GK6W93CB
B0G5785Q47
B0DMS3W5Y6
B0CLNW169N"""

RISK_WORDS = [
    "bathroom",
    "toilet",
    "restroom",
    "locker",
    "changing",
    "dressing",
    "cheating",
    "cheater",
    "employee",
    "workplace",
    "hotel",
    "airbnb",
    "motel",
    "illegal",
    "secretly",
    "undetectable",
    "invisible",
    "spy on",
]

COL_ALIASES = {
    "campaign": ["广告活动", "Campaign", "Campaign Name", "campaignName"],
    "ad_group": ["广告组", "Ad Group", "Ad group", "Ad Group Name", "adGroupName"],
    "placement": ["广告位", "Placement", "placement"],
    "targeting": ["投放", "Targeting", "Target", "targeting"],
    "keyword": ["关键词", "Keyword", "keyword"],
    "match": ["匹配方式", "Match Type", "matchType"],
    "search": ["用户搜索词", "Customer Search Term", "Search Term", "searchTerm", "搜索词"],
    "bid": ["当前竞价-本币", "Bid", "bid"],
    "budget": ["预算", "Budget", "budget"],
    "impressions": ["曝光量", "Impressions", "impressions"],
    "clicks": ["点击", "Clicks", "clicks"],
    "spend": ["花费-本币", "Spend", "Cost", "spend", "cost"],
    "sales": ["广告销售额-本币", "Sales", "7 Day Total Sales", "sales"],
    "orders": ["广告订单", "Orders", "7 Day Total Orders (#)", "orders"],
}


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_float(value: str | bytes | None, default_value: float) -> float:
    if value is None:
        return default_value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    try:
        return float(str(value).strip().replace("%", ""))
    except ValueError:
        return default_value


def parse_text(value: str | bytes | None, default_value: str = "") -> str:
    if value is None:
        return default_value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def split_lines(text: str) -> list[str]:
    items = []
    for part in re.split(r"[\n,，;；\s]+", text):
        part = part.strip()
        if part:
            items.append(part)
    return items


def read_table(file_obj: dict | None) -> pd.DataFrame | None:
    if not file_obj or not file_obj.get("content"):
        return None
    filename = str(file_obj.get("filename") or "").lower()
    data = BytesIO(file_obj["content"])
    if filename.endswith(".csv"):
        return pd.read_csv(data)
    return pd.read_excel(data)


def find_col(df: pd.DataFrame, key: str) -> str | None:
    if df is None:
        return None
    candidates = COL_ALIASES.get(key, [key])
    columns = {str(c).strip(): c for c in df.columns}
    lower = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand in columns:
            return columns[cand]
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def ensure_numeric(df: pd.DataFrame, cols: list[str | None]) -> pd.DataFrame:
    out = df.copy()
    for col in cols:
        if col and col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
    return out


def metrics_for(df: pd.DataFrame, columns: dict[str, str | None], target_cpa: float) -> pd.DataFrame:
    out = df.copy()
    clicks = columns.get("clicks")
    imps = columns.get("impressions")
    spend = columns.get("spend")
    sales = columns.get("sales")
    orders = columns.get("orders")

    out["CTR"] = out[clicks] / out[imps].replace(0, pd.NA) if clicks and imps else pd.NA
    out["CPC"] = out[spend] / out[clicks].replace(0, pd.NA) if spend and clicks else pd.NA
    out["CPA"] = out[spend] / out[orders].replace(0, pd.NA) if spend and orders else pd.NA
    out["ACOS"] = out[spend] / out[sales].replace(0, pd.NA) if spend and sales else pd.NA
    out["CVR"] = out[orders] / out[clicks].replace(0, pd.NA) if orders and clicks else pd.NA
    out["Max CPC"] = target_cpa * out["CVR"]
    out["CPC Gap"] = out["Max CPC"] - out["CPC"]
    return out


def money(v) -> str:
    if pd.isna(v):
        return "-"
    return f"${float(v):,.2f}"


def pct(v) -> str:
    if pd.isna(v):
        return "-"
    return f"{float(v) * 100:.2f}%"


def to_record(row: pd.Series, cols: list[str]) -> dict:
    data = {}
    for col in cols:
        val = row.get(col, "")
        if pd.isna(val):
            val = ""
        if isinstance(val, float):
            val = round(val, 4)
        data[col] = val
    return data


def classify_term(row: pd.Series, term_col: str, cols: dict[str, str | None], target_cpa: float, target_cvr: float) -> tuple[str, str, str]:
    term = str(row.get(term_col, "")).lower()
    spend = float(row.get(cols.get("spend"), 0) or 0)
    clicks = float(row.get(cols.get("clicks"), 0) or 0)
    orders = float(row.get(cols.get("orders"), 0) or 0)
    cpa = row.get("CPA")
    cvr = row.get("CVR")

    if any(word in term for word in RISK_WORDS):
        return "风险否词", "否定/不放大", "隐私或审核风险高，不适合冲排名。"
    if orders >= 6 and pd.notna(cpa) and cpa <= target_cpa and pd.notna(cvr) and cvr >= target_cvr:
        return "第一梯队冲排名", "Exact独立活动 + Top of Search加权", "多单、CPA达标、CVR达标，适合用广告订单推自然排名。"
    if orders >= 3 and pd.notna(cpa) and cpa <= target_cpa * 1.35 and pd.notna(cvr) and cvr >= target_cvr:
        return "第二梯队观察冲", "Exact低预算冲刺", "转化达标，但CPA或单量略弱，先小预算验证。"
    if orders >= 5 and (pd.isna(cvr) or cvr < target_cvr):
        return "先优化Listing再冲", "保留但不加大", "有订单但CVR不足，直接放大会推高CPA。"
    if orders >= 1 and pd.notna(cpa) and cpa > target_cpa * 1.35:
        return "降价观察", "降竞价20%-30%", "有订单但CPA偏高，先压CPC。"
    if orders == 0 and (spend >= target_cpa or clicks >= 25):
        return "否词止损", "精准否定/暂停", "花费或点击已到止损线仍无订单。"
    return "样本不足", "继续观察", "数据还不够，不做激进动作。"


def build_search_analysis(search_df: pd.DataFrame | None, target_cpa: float, target_cvr: float) -> tuple[dict, pd.DataFrame, list[dict], list[dict]]:
    if search_df is None or search_df.empty:
        return {}, pd.DataFrame(), [], []

    cols = {key: find_col(search_df, key) for key in ["search", "impressions", "clicks", "spend", "sales", "orders"]}
    if not cols["search"]:
        cols["search"] = find_col(search_df, "keyword") or find_col(search_df, "targeting")
    required = [cols[k] for k in ["search", "clicks", "spend", "orders"]]
    if any(c is None for c in required):
        raise ValueError("搜索词报表缺少必要字段：用户搜索词/点击/花费/广告订单")

    numeric_cols = [cols[k] for k in ["impressions", "clicks", "spend", "sales", "orders"]]
    df = ensure_numeric(search_df, numeric_cols)
    grouped = df.groupby(cols["search"], dropna=False).agg(
        {
            cols["impressions"]: "sum" if cols["impressions"] else "size",
            cols["clicks"]: "sum",
            cols["spend"]: "sum",
            cols["sales"]: "sum" if cols["sales"] else "sum",
            cols["orders"]: "sum",
        }
    ).reset_index()

    # If impressions or sales were unavailable, create stable fallback columns.
    if not cols["impressions"]:
        grouped["曝光量"] = 0
        cols["impressions"] = "曝光量"
    if not cols["sales"]:
        grouped["广告销售额"] = 0
        cols["sales"] = "广告销售额"

    grouped = metrics_for(grouped, cols, target_cpa)
    grouped[["分层", "下一步动作", "原因"]] = grouped.apply(
        lambda row: pd.Series(classify_term(row, cols["search"], cols, target_cpa, target_cvr)),
        axis=1,
    )
    grouped["建议竞价"] = grouped.apply(
        lambda r: min(max(float(r["Max CPC"]) if pd.notna(r["Max CPC"]) else 0.3, 0.18), 0.9),
        axis=1,
    )
    grouped["建议日预算"] = grouped.apply(
        lambda r: 40
        if r["分层"] == "第一梯队冲排名" and r[cols["orders"]] >= 14
        else (25 if r["分层"] == "第一梯队冲排名" else (15 if r["分层"] == "第二梯队观察冲" else 0)),
        axis=1,
    )
    priority = {"第一梯队冲排名": 1, "第二梯队观察冲": 2, "先优化Listing再冲": 3, "降价观察": 4, "否词止损": 5, "风险否词": 6, "样本不足": 7}
    grouped["排序"] = grouped["分层"].map(priority).fillna(99)
    grouped = grouped.sort_values(["排序", cols["orders"], "CPA"], ascending=[True, False, True])

    total_spend = grouped[cols["spend"]].sum()
    total_orders = grouped[cols["orders"]].sum()
    total_clicks = grouped[cols["clicks"]].sum()
    total_sales = grouped[cols["sales"]].sum()
    summary = {
        "spend": float(total_spend),
        "orders": float(total_orders),
        "sales": float(total_sales),
        "clicks": float(total_clicks),
        "cpa": float(total_spend / total_orders) if total_orders else None,
        "cvr": float(total_orders / total_clicks) if total_clicks else None,
        "acos": float(total_spend / total_sales) if total_sales else None,
        "cpc": float(total_spend / total_clicks) if total_clicks else None,
    }

    show_cols = [cols["search"], "分层", "下一步动作", cols["clicks"], cols["spend"], cols["sales"], cols["orders"], "CPC", "CPA", "CVR", "ACOS", "建议竞价", "建议日预算", "原因"]
    rank_terms = grouped[grouped["分层"].isin(["第一梯队冲排名", "第二梯队观察冲", "先优化Listing再冲"])][show_cols].head(80)
    negatives = grouped[grouped["分层"].isin(["否词止损", "风险否词", "降价观察"])][show_cols].head(120)
    return summary, grouped, rank_terms.to_dict(orient="records"), negatives.to_dict(orient="records")


def build_adgroup_analysis(adgroup_df: pd.DataFrame | None, target_cpa: float, target_cvr: float) -> list[dict]:
    if adgroup_df is None or adgroup_df.empty:
        return []
    cols = {key: find_col(adgroup_df, key) for key in ["campaign", "ad_group", "bid", "impressions", "clicks", "spend", "sales", "orders"]}
    if not cols["campaign"] or not cols["ad_group"]:
        return []
    df = ensure_numeric(adgroup_df, [cols[k] for k in ["bid", "impressions", "clicks", "spend", "sales", "orders"]])
    df = metrics_for(df, cols, target_cpa)

    def action(row: pd.Series) -> str:
        orders = row.get(cols["orders"], 0)
        spend = row.get(cols["spend"], 0)
        cpa = row.get("CPA")
        cvr = row.get("CVR")
        if pd.notna(cpa) and cpa <= target_cpa and pd.notna(cvr) and cvr >= target_cvr:
            return "保留/加预算"
        if orders >= 5 and pd.notna(cpa) and cpa <= target_cpa * 1.35:
            return "观察冲排名"
        if orders == 0 and spend >= target_cpa:
            return "暂停止损"
        if pd.notna(cpa) and cpa > target_cpa * 1.35:
            return "降价20%-30%"
        return "观察"

    df["动作"] = df.apply(action, axis=1)
    df = df.sort_values([cols["orders"], "CPA"], ascending=[False, True])
    show_cols = [cols["campaign"], cols["ad_group"], cols["bid"], cols["clicks"], cols["spend"], cols["sales"], cols["orders"], "CPC", "CPA", "CVR", "ACOS", "动作"]
    return df[show_cols].head(80).to_dict(orient="records")


def build_placement_analysis(placement_df: pd.DataFrame | None, target_cpa: float) -> list[dict]:
    if placement_df is None or placement_df.empty:
        return []
    cols = {key: find_col(placement_df, key) for key in ["placement", "impressions", "clicks", "spend", "sales", "orders"]}
    if not cols["placement"]:
        return []
    df = ensure_numeric(placement_df, [cols[k] for k in ["impressions", "clicks", "spend", "sales", "orders"]])
    grouped = df.groupby(cols["placement"], dropna=False).agg(
        {cols["impressions"]: "sum", cols["clicks"]: "sum", cols["spend"]: "sum", cols["sales"]: "sum", cols["orders"]: "sum"}
    ).reset_index()
    grouped = metrics_for(grouped, cols, target_cpa)

    def action(row: pd.Series) -> str:
        placement = str(row.get(cols["placement"], ""))
        cvr = row.get("CVR")
        cpa = row.get("CPA")
        if "顶部" in placement or "Top" in placement:
            return "排名词加Top of Search 50%-120%" if pd.notna(cvr) else "观察"
        if "商品" in placement or "Product" in placement:
            return "只保留高转化ASIN"
        if pd.notna(cpa) and cpa > target_cpa * 1.35:
            return "降权/降价"
        return "保留基础流量"

    grouped["动作"] = grouped.apply(action, axis=1)
    show_cols = [cols["placement"], cols["clicks"], cols["spend"], cols["sales"], cols["orders"], "CPC", "CPA", "CVR", "ACOS", "动作"]
    return grouped[show_cols].sort_values(cols["orders"], ascending=False).to_dict(orient="records")


def normalize_competitor(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    aliases = {
        "asin": ["asin", "ASIN", "竞品ASIN", "竞品", "产品ASIN"],
        "title": ["title", "Title", "标题"],
        "price": ["price", "Price", "价格"],
        "coupon": ["coupon", "Coupon", "优惠券"],
        "rating": ["rating", "Rating", "评分"],
        "reviews": ["reviews", "review_count", "Reviews", "评论数", "Review Count"],
        "bsr": ["bsr", "BSR", "排名", "类目排名"],
        "image_hash": ["image_hash", "主图hash", "主图指纹"],
    }
    result = pd.DataFrame()
    lower_cols = {str(c).strip().lower(): c for c in df.columns}
    raw_cols = {str(c).strip(): c for c in df.columns}
    for key, names in aliases.items():
        col = None
        for name in names:
            if name in raw_cols:
                col = raw_cols[name]
                break
            if name.lower() in lower_cols:
                col = lower_cols[name.lower()]
                break
        result[key] = df[col] if col is not None else ""
    result["asin"] = result["asin"].astype(str).str.upper().str.strip()
    result = result[result["asin"].ne("")]
    result["snapshot_time"] = datetime.now().isoformat(timespec="seconds")
    return result


def compare_competitors(current: pd.DataFrame, asin_text: str) -> list[dict]:
    history_path = DATA_DIR / "competitor_history.csv"
    watch_asins = [a.upper() for a in split_lines(asin_text)]
    changes: list[dict] = []

    if current is None or current.empty:
        for asin in watch_asins:
            changes.append({"ASIN": asin, "动作": "待采集", "变化": "没有上传竞品快照，今天需要采集价格、coupon、评分、评论数、BSR、主图。", "建议": "先建立第一天基线。"})
        return changes

    prev = pd.read_csv(history_path) if history_path.exists() else pd.DataFrame()
    for _, row in current.iterrows():
        asin = str(row["asin"]).upper()
        prev_rows = prev[prev["asin"].astype(str).str.upper().eq(asin)] if not prev.empty and "asin" in prev.columns else pd.DataFrame()
        if prev_rows.empty:
            changes.append({"ASIN": asin, "动作": "建立基线", "变化": "首次上传该竞品快照。", "建议": "明天开始对比价格、coupon、评论、BSR、图片变化。"})
            continue
        last = prev_rows.iloc[-1]
        notes = []
        for col, label in [("price", "价格"), ("coupon", "Coupon"), ("rating", "评分"), ("reviews", "评论数"), ("bsr", "BSR"), ("title", "标题"), ("image_hash", "主图")]:
            old = str(last.get(col, "")).strip()
            new = str(row.get(col, "")).strip()
            if new and old and new != old:
                notes.append(f"{label}: {old} -> {new}")
        if notes:
            changes.append({"ASIN": asin, "动作": "发现变化", "变化": "；".join(notes), "建议": "如果竞品降价/加券/换主图，同时观察你的核心词排名和转化。"})
        else:
            changes.append({"ASIN": asin, "动作": "无明显变化", "变化": "核心字段未变化。", "建议": "继续监控广告位和自然排名。"})

    combined = pd.concat([prev, current], ignore_index=True) if not prev.empty else current
    combined.to_csv(history_path, index=False, encoding="utf-8-sig")
    return changes


def compare_ad_history(terms: pd.DataFrame) -> list[dict]:
    if terms is None or terms.empty:
        return []
    history_path = DATA_DIR / "ad_term_history.csv"
    cols = ["用户搜索词", "Customer Search Term", "Search Term", "searchTerm", "搜索词"]
    term_col = next((c for c in cols if c in terms.columns), terms.columns[0])
    snapshot = terms.copy()
    snapshot["snapshot_time"] = datetime.now().isoformat(timespec="seconds")
    keep = [c for c in [term_col, "CPC", "CPA", "CVR", "ACOS", "Max CPC", "建议日预算", "分层", "snapshot_time"] if c in snapshot.columns]
    snapshot = snapshot[keep].rename(columns={term_col: "term"})
    prev = pd.read_csv(history_path) if history_path.exists() else pd.DataFrame()
    notes = []
    if not prev.empty and "term" in prev.columns:
        latest_time = prev["snapshot_time"].max()
        last = prev[prev["snapshot_time"].eq(latest_time)]
        merged = snapshot.merge(last, on="term", suffixes=("_now", "_prev"))
        for _, row in merged.head(300).iterrows():
            cpa_now = row.get("CPA_now")
            cpa_prev = row.get("CPA_prev")
            cvr_now = row.get("CVR_now")
            cvr_prev = row.get("CVR_prev")
            tier = row.get("分层_now", "")
            if pd.notna(cpa_now) and pd.notna(cpa_prev) and cpa_now < cpa_prev * 0.8 and tier in ["第一梯队冲排名", "第二梯队观察冲"]:
                notes.append({"搜索词": row["term"], "变化": f"CPA改善：{money(cpa_prev)} -> {money(cpa_now)}", "建议": "可继续冲排名或加预算。"})
            elif pd.notna(cvr_now) and pd.notna(cvr_prev) and cvr_now < cvr_prev * 0.75:
                notes.append({"搜索词": row["term"], "变化": f"CVR下滑：{pct(cvr_prev)} -> {pct(cvr_now)}", "建议": "先别加预算，检查价格、coupon、库存、Listing。"})
    combined = pd.concat([prev, snapshot], ignore_index=True) if not prev.empty else snapshot
    combined.to_csv(history_path, index=False, encoding="utf-8-sig")
    return notes[:30]


def build_today_actions(summary: dict, rank_terms: list[dict], negatives: list[dict], placements: list[dict], competitor_changes: list[dict], target_cpa: float, target_cvr: float) -> list[dict]:
    actions = []
    if summary:
        if summary.get("cvr") is not None and summary["cvr"] < target_cvr:
            actions.append({"优先级": "P0", "动作": "先优化Listing承接", "对象": "主图/五点/A+和参数说明", "原因": f"当前整体CVR {pct(summary['cvr'])}，低于目标 {pct(target_cvr)}。", "是否自动": "否"})
        if summary.get("cpa") is not None and summary["cpa"] > target_cpa:
            actions.append({"优先级": "P0", "动作": "预算从泛词转移到冲排名词", "对象": "Exact出单词", "原因": f"当前整体CPA {money(summary['cpa'])}，高于目标 {money(target_cpa)}。", "是否自动": "需审批"})

    tier1 = [r for r in rank_terms if r.get("分层") == "第一梯队冲排名"]
    if tier1:
        terms = "、".join(str(r.get("用户搜索词") or r.get("Search Term") or list(r.values())[0]) for r in tier1[:5])
        actions.append({"优先级": "P0", "动作": "新建/更新Rank_Exact_Tier1", "对象": terms, "原因": "这些词满足多单、CPA/CVR达标，适合冲自然前20。", "是否自动": "需审批"})

    top_place = next((p for p in placements if "顶部" in str(next(iter(p.values()), "")) or "Top" in str(next(iter(p.values()), ""))), None)
    if top_place:
        actions.append({"优先级": "P1", "动作": "排名词加Top of Search", "对象": "第一梯队Exact活动", "原因": "顶部广告位通常更接近排名冲刺流量；请结合表格里的CVR/CPA决定加权。", "是否自动": "需审批"})

    stop_terms = [r for r in negatives if r.get("分层") in ["否词止损", "风险否词"]]
    if stop_terms:
        terms = "、".join(str(r.get("用户搜索词") or r.get("Search Term") or list(r.values())[0]) for r in stop_terms[:6])
        actions.append({"优先级": "P0", "动作": "精准否词/风险否词", "对象": terms, "原因": "无效消耗或合规风险高，预算不限也不能放任低质点击。", "是否自动": "可自动"})

    if competitor_changes:
        changed = [c for c in competitor_changes if c.get("动作") == "发现变化"]
        if changed:
            actions.append({"优先级": "P1", "动作": "查看竞品变化并决定是否应对", "对象": "、".join(c["ASIN"] for c in changed[:5]), "原因": "竞品价格/coupon/标题/评论/BSR变化可能影响你的转化和排名。", "是否自动": "否"})
        else:
            actions.append({"优先级": "P2", "动作": "补齐竞品基线", "对象": "竞品ASIN", "原因": "先建立基线，后续才能发现竞品每天动作。", "是否自动": "否"})
    return actions


def save_excel_report(result: dict, term_df: pd.DataFrame) -> str:
    report_id = now_id()
    path = OUTPUTS / f"agent_daily_report_{report_id}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(result["today_actions"]).to_excel(writer, sheet_name="今日动作", index=False)
        pd.DataFrame(result["rank_terms"]).to_excel(writer, sheet_name="排名冲刺词", index=False)
        pd.DataFrame(result["negatives"]).to_excel(writer, sheet_name="否词降价", index=False)
        pd.DataFrame(result["adgroups"]).to_excel(writer, sheet_name="广告组动作", index=False)
        pd.DataFrame(result["placements"]).to_excel(writer, sheet_name="广告位策略", index=False)
        pd.DataFrame(result["competitors"]).to_excel(writer, sheet_name="竞品变化", index=False)
        pd.DataFrame(result["ad_history_notes"]).to_excel(writer, sheet_name="广告变化复盘", index=False)
        if term_df is not None and not term_df.empty:
            term_df.head(500).to_excel(writer, sheet_name="搜索词全量前500", index=False)
        pd.DataFrame(
            columns=[
                "动作日期",
                "对象类型",
                "活动",
                "广告组",
                "关键词/ASIN/搜索词",
                "动作",
                "旧值",
                "新值",
                "动作原因",
                "24h结果",
                "48h结果",
                "72h结果",
                "7天结果",
                "结论",
            ]
        ).to_excel(writer, sheet_name="动作日志模板", index=False)
    return path.name


def analyze(form: dict, files: dict) -> dict:
    asin = parse_text(form.get("asin"), "B0GWQJMC1V").strip().upper()
    price = parse_float(form.get("price"), 59.99)
    cost_rmb = parse_float(form.get("cost_rmb"), 150)
    fx_rate = parse_float(form.get("fx_rate"), 7.15)
    return_rate = parse_float(form.get("return_rate"), 22) / 100
    target_cpa = parse_float(form.get("target_cpa"), 9)
    target_cvr = parse_float(form.get("target_cvr"), 4) / 100
    competitor_asins = parse_text(form.get("competitor_asins"), DEFAULT_COMPETITOR_ASINS)

    search_df = read_table(files.get("search_report"))
    adgroup_df = read_table(files.get("adgroup_report"))
    placement_df = read_table(files.get("placement_report"))
    comp_df = normalize_competitor(read_table(files.get("competitor_report")))

    summary, term_df, rank_terms, negatives = build_search_analysis(search_df, target_cpa, target_cvr)
    adgroups = build_adgroup_analysis(adgroup_df, target_cpa, target_cvr)
    placements = build_placement_analysis(placement_df, target_cpa)
    competitors = compare_competitors(comp_df, competitor_asins)
    ad_history_notes = compare_ad_history(term_df)
    today_actions = build_today_actions(summary, rank_terms, negatives, placements, competitors, target_cpa, target_cvr)
    report_name = save_excel_report(
        {
            "today_actions": today_actions,
            "rank_terms": rank_terms,
            "negatives": negatives,
            "adgroups": adgroups,
            "placements": placements,
            "competitors": competitors,
            "ad_history_notes": ad_history_notes,
        },
        term_df,
    )

    cost_usd = cost_rmb / fx_rate if fx_rate else 0
    gross_before_ad = price - cost_usd
    return_adjusted = gross_before_ad * (1 - return_rate)
    result = {
        "asin": asin,
        "summary": summary,
        "economics": {
            "price": price,
            "cost_usd": cost_usd,
            "gross_before_ad": gross_before_ad,
            "return_adjusted": return_adjusted,
            "target_cpa": target_cpa,
            "target_cvr": target_cvr,
            "max_cpc_at_target": target_cpa * target_cvr,
        },
        "today_actions": today_actions,
        "rank_terms": rank_terms[:60],
        "negatives": negatives[:80],
        "adgroups": adgroups[:60],
        "placements": placements,
        "competitors": competitors,
        "ad_history_notes": ad_history_notes,
        "download": f"/download?file={report_name}",
    }
    return result


def list_reports() -> list[dict]:
    reports = []
    for path in sorted(OUTPUTS.glob("agent_daily_report_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True):
        reports.append(
            {
                "name": path.name,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size_kb": round(path.stat().st_size / 1024, 1),
                "download": f"/download?file={path.name}",
            }
        )
    return reports


HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Amazon Ads Agent</title>
  <style>
    :root { --bg:#f6f7f9; --panel:#fff; --line:#dce2ea; --text:#0f172a; --muted:#64748b; --accent:#2563eb; --warn:#b7791f; --bad:#c2410c; --radius:10px; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter,"Segoe UI",Arial,sans-serif; color:var(--text); background:var(--bg); }
    header { height:60px; display:flex; align-items:center; justify-content:space-between; padding:0 20px; border-bottom:1px solid var(--line); background:#fff; position:sticky; top:0; z-index:5; }
    .brand { display:flex; align-items:center; gap:12px; font-weight:800; }
    .mark { width:30px; height:30px; border-radius:8px; background:var(--accent); display:grid; place-items:center; color:#fff; font-weight:900; }
    .layout { display:grid; grid-template-columns:380px minmax(0,1fr); min-height:calc(100vh - 60px); }
    aside { border-right:1px solid var(--line); background:#fff; padding:18px; overflow:auto; }
    main { padding:20px; overflow:auto; }
    h1 { font-size:22px; margin:0 0 8px; }
    h2 { font-size:18px; margin:0 0 12px; }
    h3 { font-size:18px; margin:14px 0 10px; }
    p { color:var(--muted); line-height:1.6; margin:0 0 14px; }
    label { display:block; font-size:12px; font-weight:700; color:#334155; margin:12px 0 6px; }
    input, textarea { width:100%; border:1px solid var(--line); border-radius:8px; padding:10px 12px; font:inherit; background:#fff; color:var(--text); }
    textarea { min-height:88px; resize:vertical; }
    input[type=file] { padding:8px; background:#fbfcfe; }
    .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .button { width:100%; border:0; border-radius:8px; background:var(--accent); color:#fff; font-weight:800; padding:12px 14px; margin-top:16px; cursor:pointer; }
    .button:disabled { opacity:.6; cursor:wait; }
    .secondary { display:inline-flex; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:8px; padding:9px 12px; color:var(--text); background:#fff; text-decoration:none; font-weight:700; }
    .hero { margin-bottom:16px; }
    .cards { display:grid; grid-template-columns:repeat(5,minmax(120px,1fr)); gap:12px; margin:16px 0; }
    .skill-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin-bottom:16px; }
    .card, .section { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius); }
    .card { padding:14px; }
    .card .label { color:var(--muted); font-size:12px; margin-bottom:8px; }
    .card .value { font-size:22px; font-weight:800; }
    .section { margin-bottom:14px; padding:16px; }
    .skill-card { padding:18px; }
    .skill-card { cursor:pointer; transition:border-color .15s ease, box-shadow .15s ease, transform .15s ease; }
    .skill-card:hover, .skill-card.active { border-color:var(--accent); box-shadow:0 8px 22px rgba(37,99,235,.10); transform:translateY(-1px); }
    .skill-card:focus { outline:2px solid rgba(37,99,235,.28); outline-offset:2px; }
    .skill-card .meta { color:var(--muted); line-height:1.7; margin-bottom:8px; }
    .skill-panel { border:1px solid var(--line); border-radius:var(--radius); padding:16px; background:#fbfcfe; margin-top:14px; }
    .skill-panel-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
    .skill-panel-head h3 { margin:0; font-size:20px; }
    .skill-panel-head p { margin:6px 0 0; color:var(--muted); }
    .skill-panel-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
    .skill-panel-box { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fff; min-height:118px; }
    .skill-panel-box h4 { margin:0 0 8px; font-size:14px; }
    .skill-panel-box p { margin:0; color:var(--muted); line-height:1.7; }
    .skill-panel-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; align-items:center; }
    .skill-primary { width:auto; margin-top:0; }
    .small-button { border:1px solid var(--line); background:#fff; border-radius:8px; padding:11px 13px; font-weight:800; color:var(--text); cursor:pointer; }
    .hint { color:var(--muted); font-size:13px; }
    .focus-pulse { box-shadow:0 0 0 3px rgba(37,99,235,.18); border-color:var(--accent) !important; }
    .chips { display:flex; gap:8px; flex-wrap:wrap; }
    .pill { display:inline-flex; align-items:center; border-radius:999px; padding:4px 10px; font-size:12px; font-weight:700; background:#eef4ff; color:#164fb7; }
    .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:8px; }
    table { width:100%; border-collapse:collapse; font-size:13px; background:#fff; }
    th, td { padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap; }
    th { background:#f1f4f8; font-weight:700; color:#334155; position:sticky; top:0; }
    tr:last-child td { border-bottom:0; }
    .p0 { color:var(--bad); font-weight:800; }
    .p1 { color:var(--warn); font-weight:800; }
    .empty { padding:24px; color:var(--muted); text-align:center; }
    .status { font-size:13px; color:var(--muted); margin-top:10px; min-height:20px; }
    @media (max-width:980px) { .layout{grid-template-columns:1fr;} aside{border-right:0;border-bottom:1px solid var(--line);} .cards{grid-template-columns:repeat(2,1fr);} .skill-grid{grid-template-columns:1fr;} .skill-panel-grid{grid-template-columns:1fr;} }
  </style>
</head>
<body>
  <header>
    <div class="brand"><div class="mark">A</div><span>Amazon Ads Agent</span></div>
    <a class="secondary" id="downloadLink" href="#" style="display:none">&#x4e0b;&#x8f7d; Excel &#x62a5;&#x544a;</a>
  </header>
  <div class="layout">
    <aside>
      <h2>&#x8f93;&#x5165;&#x6570;&#x636e;</h2>
      <p>&#x4e0a;&#x4f20;&#x51cc;&#x661f;/Amazon &#x5e7f;&#x544a;&#x62a5;&#x8868;&#x540e;&#xff0c;Agent &#x4f1a;&#x751f;&#x6210;&#x4eca;&#x5929;&#x7684;&#x5e7f;&#x544a;&#x52a8;&#x4f5c;&#x548c;&#x7ade;&#x54c1;&#x76d1;&#x63a7;&#x6e05;&#x5355;&#x3002;</p>
      <form id="agentForm">
        <label>ASIN</label><input name="asin" value="B0GWQJMC1V" />
        <div class="grid2">
          <div><label>&#x552e;&#x4ef7; USD</label><input name="price" value="59.99" /></div>
          <div><label>&#x6210;&#x672c; RMB</label><input name="cost_rmb" value="150" /></div>
          <div><label>&#x6c47;&#x7387;</label><input name="fx_rate" value="7.15" /></div>
          <div><label>&#x9000;&#x8d27;&#x7387; %</label><input name="return_rate" value="22" /></div>
          <div><label>&#x76ee;&#x6807; CPA</label><input name="target_cpa" value="9" /></div>
          <div><label>&#x76ee;&#x6807; CVR %</label><input name="target_cvr" value="4" /></div>
          <div><label>&#x5229;&#x6da6;&#x76ee;&#x6807; USD</label><input name="target_profit" value="15" /></div>
          <div><label>&#x4e8f;&#x635f;&#x5468;&#x671f; &#x5929;</label><input name="max_launch_loss_days" value="7" /></div>
          <div><label>&#x5e7f;&#x544a;&#x7ec4;&#x65e0;&#x5355;&#x6b62;&#x635f;</label><input name="adgroup_no_order_spend" value="10" /></div>
          <div><label>&#x5173;&#x952e;&#x8bcd;&#x70b9;&#x51fb;&#x6b62;&#x635f;</label><input name="keyword_no_order_clicks" value="20" /></div>
        </div>
        <label>&#x7528;&#x6237;&#x641c;&#x7d22;&#x8bcd;&#x62a5;&#x544a;</label><input type="file" name="search_report" accept=".xlsx,.xls,.csv" />
        <label>&#x5e7f;&#x544a;&#x7ec4;&#x62a5;&#x544a;</label><input type="file" name="adgroup_report" accept=".xlsx,.xls,.csv" />
        <label>&#x5e7f;&#x544a;&#x4f4d;&#x62a5;&#x544a;</label><input type="file" name="placement_report" accept=".xlsx,.xls,.csv" />
        <label>&#x7ade;&#x54c1;&#x5feb;&#x7167;&#x8868;&#xff0c;&#x53ef;&#x9009;</label><input type="file" name="competitor_report" accept=".xlsx,.xls,.csv" />
        <label>&#x7ade;&#x54c1; ASIN&#xff0c;&#x4e00;&#x884c;&#x4e00;&#x4e2a;</label><textarea name="competitor_asins">B0GK6W93CB
B0G5785Q47
B0DMS3W5Y6
B0CLNW169N</textarea>
        <label>&#x76ee;&#x6807;&#x5173;&#x952e;&#x8bcd;</label><textarea name="target_keywords">hidden camera outlet
ax nanny cam
hidden cameras
wall outlet camera
hidden camera charger
outlet camera
hidden camera 4k
wall outlet camera with audio
b0gqmrpkhw
hidden camera in outlet
spy camera hidden camera
nanny cam outlet
hidden peephole camera
nanny camera
wall outlet hidden camera
b0g5j4fmzd
hidden camera white
b0glg7d81f
plug in hidden camera with audio/video
b0gsg4dshc
secret cameras for spying with audio
hidden camera
nanny cam hidden camera
hidden camera with audio/video
spy camera hidden camera with audio/video
spy camera
nanny cam
camaras espias ocultas
hidden cameras for home
nanny cam hidden camera with audio/video
secret camera</textarea>
        <button class="button" type="submit">&#x8fd0;&#x884c; Agent &#x5206;&#x6790;</button>
        <div class="status" id="status"></div>
      </form>
    </aside>
    <main>
      <div class="hero"><h1>&#x6bcf;&#x65e5;&#x5e7f;&#x544a;&#x4e0e;&#x7ade;&#x54c1; Agent</h1><p>&#x5148;&#x505a;&#x534a;&#x81ea;&#x52a8;&#x7248;&#xff1a;&#x4f60;&#x628a;&#x6570;&#x636e;&#x653e;&#x8fdb;&#x6765;&#xff0c;&#x5b83;&#x6bcf;&#x5929;&#x7ed9;&#x4f60;&#x4e0b;&#x4e00;&#x6b65;&#x52a8;&#x4f5c;&#x3002;&#x7b49;&#x89c4;&#x5219;&#x8dd1;&#x987a;&#xff0c;&#x518d;&#x63a5; Amazon Ads API &#x548c;&#x7ade;&#x54c1;&#x6570;&#x636e;&#x6e90;&#x3002;</p></div>
      <div class="section">
        <h2>Skills</h2><p>&#x4f60;&#x73b0;&#x5728;&#x8fd9;&#x5957; Agent &#x5148;&#x6309; 6 &#x4e2a;&#x80fd;&#x529b;&#x6a21;&#x5757;&#x6765;&#x7528;&#x3002;</p>
        <div class="skill-grid">
          <div class="card skill-card"><span class="pill">&#x5df2;&#x53ef;&#x7528;</span><h3>&#x5e7f;&#x544a;&#x52a8;&#x4f5c; Agent</h3><div class="meta"><strong>&#x8f93;&#x5165;&#xff1a;</strong>&#x641c;&#x7d22;&#x8bcd; / &#x5e7f;&#x544a;&#x7ec4; / &#x5e7f;&#x544a;&#x4f4d;</div><div class="meta"><strong>&#x8f93;&#x51fa;&#xff1a;</strong>&#x52a0;&#x4ef7;&#x3001;&#x964d;&#x4ef7;&#x3001;&#x5426;&#x8bcd;&#x3001;&#x9884;&#x7b97;&#x8f6c;&#x79fb;&#x3001;&#x51b2;&#x6392;&#x540d;&#x8bcd;</div><div class="chips"><span class="pill">CPA</span><span class="pill">CVR</span><span class="pill">&#x5426;&#x8bcd;</span><span class="pill">&#x51b2;&#x6392;&#x540d;</span></div></div>
          <div class="card skill-card"><span class="pill">&#x534a;&#x81ea;&#x52a8;</span><h3>&#x7ade;&#x54c1;&#x76d1;&#x63a7; Agent</h3><div class="meta"><strong>&#x8f93;&#x5165;&#xff1a;</strong>&#x7ade;&#x54c1; ASIN / &#x5feb;&#x7167;&#x8868;</div><div class="meta"><strong>&#x8f93;&#x51fa;&#xff1a;</strong>&#x4ef7;&#x683c;&#x3001;coupon&#x3001;&#x8bc4;&#x5206;&#x3001;&#x8bc4;&#x8bba;&#x3001;BSR&#x3001;&#x4e3b;&#x56fe;&#x53d8;&#x5316;</div><div class="chips"><span class="pill">&#x7ade;&#x54c1;</span><span class="pill">&#x4ef7;&#x683c;</span><span class="pill">&#x8bc4;&#x8bba;</span><span class="pill">BSR</span></div></div>
          <div class="card skill-card"><span class="pill">&#x5f85;&#x63a5;&#x6392;&#x540d;&#x6570;&#x636e;</span><h3>&#x81ea;&#x7136;&#x6392;&#x540d; Agent</h3><div class="meta"><strong>&#x8f93;&#x5165;&#xff1a;</strong>&#x5173;&#x952e;&#x8bcd;&#x81ea;&#x7136;&#x6392;&#x540d;</div><div class="meta"><strong>&#x8f93;&#x51fa;&#xff1a;</strong>&#x5224;&#x65ad;&#x5e7f;&#x544a;&#x52a8;&#x4f5c;&#x662f;&#x5426;&#x628a;&#x81ea;&#x7136;&#x6392;&#x540d;&#x63a8;&#x5230;&#x524d;20</div><div class="chips"><span class="pill">&#x81ea;&#x7136;&#x6392;&#x540d;</span><span class="pill">&#x5173;&#x952e;&#x8bcd;</span><span class="pill">Top20</span></div></div>
          <div class="card skill-card"><span class="pill">&#x5df2;&#x53ef;&#x5efa;&#x8bae;</span><h3>Listing &#x8f6c;&#x5316; Agent</h3><div class="meta"><strong>&#x8f93;&#x5165;&#xff1a;</strong>&#x6807;&#x9898;&#x3001;&#x4e94;&#x70b9;&#x3001;&#x5356;&#x70b9;&#x3001;&#x5e7f;&#x544a; CVR</div><div class="meta"><strong>&#x8f93;&#x51fa;&#xff1a;</strong>&#x627e;&#x51fa;&#x5f71;&#x54cd;&#x8f6c;&#x5316;&#x7684;&#x9875;&#x9762;&#x95ee;&#x9898;</div><div class="chips"><span class="pill">Listing</span><span class="pill">&#x4e3b;&#x56fe;</span><span class="pill">&#x4e94;&#x70b9;</span><span class="pill">A+</span></div></div>
          <div class="card skill-card"><span class="pill">&#x5f85;&#x63a5;&#x8bc4;&#x8bba;&#x6570;&#x636e;</span><h3>VOC &#x7528;&#x6237;&#x9700;&#x6c42; Agent</h3><div class="meta"><strong>&#x8f93;&#x5165;&#xff1a;</strong>&#x81ea;&#x5df1;&#x548c;&#x7ade;&#x54c1;&#x8bc4;&#x8bba;</div><div class="meta"><strong>&#x8f93;&#x51fa;&#xff1a;</strong>&#x4e70;&#x5bb6;&#x75db;&#x70b9;&#x3001;&#x9000;&#x8d27;&#x539f;&#x56e0;&#x3001;&#x5356;&#x70b9;&#x8865;&#x5f3a;&#x65b9;&#x5411;</div><div class="chips"><span class="pill">VOC</span><span class="pill">&#x8bc4;&#x8bba;</span><span class="pill">&#x9000;&#x8d27;</span></div></div>
          <div class="card skill-card"><span class="pill">&#x9700; API &#x6388;&#x6743;</span><h3>&#x81ea;&#x52a8;&#x6267;&#x884c; Agent</h3><div class="meta"><strong>&#x8f93;&#x5165;&#xff1a;</strong>Amazon Ads API</div><div class="meta"><strong>&#x8f93;&#x51fa;&#xff1a;</strong>&#x81ea;&#x52a8;&#x8c03; bid&#x3001;&#x9884;&#x7b97;&#x3001;&#x5426;&#x8bcd;&#x548c;&#x7cbe;&#x51c6;&#x8bcd;</div><div class="chips"><span class="pill">API</span><span class="pill">&#x81ea;&#x52a8;&#x5316;</span><span class="pill">&#x5ba1;&#x6279;</span></div></div>
        </div>
        <div id="skillPanel" class="skill-panel">
          <div class="skill-panel-head">
            <div>
              <span class="pill" id="skillBadge"></span>
              <h3 id="skillTitle"></h3>
              <p id="skillSubtitle"></p>
            </div>
          </div>
          <div class="skill-panel-grid">
            <div class="skill-panel-box"><h4>&#x9700;&#x8981;&#x4f60;&#x7ed9;&#x7684;&#x8f93;&#x5165;</h4><p id="skillInput"></p></div>
            <div class="skill-panel-box"><h4>Agent &#x4f1a;&#x7ed9;&#x4f60;&#x7684;&#x8f93;&#x51fa;</h4><p id="skillOutput"></p></div>
            <div class="skill-panel-box"><h4>&#x4f60;&#x73b0;&#x5728;&#x4e0b;&#x4e00;&#x6b65;</h4><p id="skillNext"></p></div>
          </div>
          <div class="skill-panel-actions">
            <button id="skillPrimaryAction" class="button skill-primary" type="button"></button>
            <button id="skillShowResults" class="small-button" type="button">&#x770b;&#x8f93;&#x51fa;&#x7ed3;&#x679c;&#x533a;</button>
            <span id="skillHint" class="hint"></span>
          </div>
        </div>
      </div>
      <div id="results"><div class="section empty">&#x8fd8;&#x6ca1;&#x6709;&#x5206;&#x6790;&#x7ed3;&#x679c;&#x3002;&#x4e0a;&#x4f20;&#x62a5;&#x8868;&#x540e;&#x70b9;&#x51fb;&#x201c;&#x8fd0;&#x884c; Agent &#x5206;&#x6790;&#x201d;&#x3002;</div></div>
    </main>
  </div>
  <script>
    const form = document.getElementById('agentForm');
    const statusEl = document.getElementById('status');
    const results = document.getElementById('results');
    const downloadLink = document.getElementById('downloadLink');
    const skillKeys = ['ads', 'competitor', 'rank', 'listing', 'voc', 'auto'];
    const skillCards = Array.from(document.querySelectorAll('.skill-card'));
    const skillData = {
      ads: {
        badge: '&#x5df2;&#x53ef;&#x7528;',
        title: '&#x5e7f;&#x544a;&#x52a8;&#x4f5c; Agent',
        subtitle: '&#x5f53;&#x524d; Skill &#x5df2;&#x9009;&#x4e2d;',
        input: '&#x4e0a;&#x4f20;&#x7528;&#x6237;&#x641c;&#x7d22;&#x8bcd;&#x3001;&#x5e7f;&#x544a;&#x7ec4;&#x3001;&#x5e7f;&#x544a;&#x4f4d; 3 &#x4e2a;&#x62a5;&#x8868;&#x3002;&#x53ef;&#x9009;&#x5e7f;&#x544a;&#x6d3b;&#x52a8;&#x62a5;&#x8868;&#x3002;',
        output: '&#x4eca;&#x65e5;&#x52a8;&#x4f5c;&#x6e05;&#x5355;&#xff1a;&#x5426;&#x8bcd;&#x3001;&#x964d; bid&#x3001;&#x51b2;&#x81ea;&#x7136;&#x6392;&#x540d;&#x3001;&#x5e7f;&#x544a;&#x4f4d;&#x8c03;&#x6574;&#x3001;&#x9884;&#x7b97;&#x5efa;&#x8bae;&#x3002;&#x6240;&#x6709;&#x6539;&#x52a8;&#x90fd;&#x9700;&#x8981;&#x4f60;&#x786e;&#x8ba4;&#x3002;',
        next: '&#x5148;&#x5728;&#x5de6;&#x4fa7;&#x9009;&#x62e9; 3 &#x4e2a;&#x5e7f;&#x544a;&#x62a5;&#x8868;&#xff0c;&#x518d;&#x70b9;&#x8fd0;&#x884c; Agent &#x5206;&#x6790;&#x3002;',
        button: '&#x53bb;&#x4e0a;&#x4f20;&#x5e7f;&#x544a;&#x62a5;&#x8868;',
        focus: 'search_report'
      },
      competitor: {
        badge: '&#x534a;&#x81ea;&#x52a8;',
        title: '&#x7ade;&#x54c1;&#x76d1;&#x63a7; Agent',
        subtitle: '&#x5f53;&#x524d; Skill &#x5df2;&#x9009;&#x4e2d;',
        input: '&#x586b; 4 &#x4e2a;&#x7ade;&#x54c1; ASIN&#xff0c;&#x4e5f;&#x53ef;&#x4ee5;&#x4e0a;&#x4f20;&#x7ade;&#x54c1;&#x5feb;&#x7167;&#x8868;&#x3002;',
        output: '&#x8f93;&#x51fa;&#x4ef7;&#x683c;&#x3001;coupon&#x3001;&#x8bc4;&#x5206;&#x3001;&#x8bc4;&#x8bba;&#x3001;BSR&#x3001;&#x4e3b;&#x56fe;&#x53d8;&#x5316;&#x548c;&#x8ddf;&#x8fdb;&#x52a8;&#x4f5c;&#x3002;',
        next: '&#x5148;&#x786e;&#x8ba4;&#x7ade;&#x54c1; ASIN&#xff1b;&#x6709;&#x5feb;&#x7167;&#x8868;&#x5c31;&#x4e0a;&#x4f20;&#xff0c;&#x6ca1;&#x6709;&#x4e5f;&#x53ef;&#x4ee5;&#x5148;&#x4fdd;&#x5b58;&#x57fa;&#x7ebf;&#x3002;',
        button: '&#x53bb;&#x586b;&#x7ade;&#x54c1; ASIN',
        focus: 'competitor_asins'
      },
      rank: {
        badge: '&#x5f85;&#x63a5;&#x6392;&#x540d;&#x6570;&#x636e;',
        title: '&#x81ea;&#x7136;&#x6392;&#x540d; Agent',
        subtitle: '&#x5f53;&#x524d; Skill &#x5df2;&#x9009;&#x4e2d;',
        input: '&#x8f93;&#x5165;&#x5173;&#x952e;&#x8bcd;&#x5f53;&#x524d;&#x81ea;&#x7136;&#x6392;&#x540d;&#xff0c;&#x6700;&#x597d;&#x6bcf;&#x5929;&#x540c;&#x4e00;&#x65f6;&#x95f4;&#x8bb0;&#x5f55;&#x3002;',
        output: '&#x5224;&#x65ad;&#x5e7f;&#x544a;&#x52a8;&#x4f5c;&#x662f;&#x5426;&#x628a;&#x91cd;&#x8981;&#x5173;&#x952e;&#x8bcd;&#x63a8;&#x5230;&#x524d;20&#xff0c;&#x5e76;&#x63d0;&#x9192;&#x7ee7;&#x7eed;&#x51b2;&#x8fd8;&#x662f;&#x964d;&#x672c;&#x3002;',
        next: '&#x5148;&#x628a;&#x81ea;&#x7136;&#x6392;&#x540d;&#x6570;&#x636e;&#x8865;&#x8fdb;&#x5feb;&#x7167;&#x8868;&#xff0c;&#x540e;&#x7eed;&#x6211;&#x53ef;&#x4ee5;&#x63a5;&#x8868;&#x683c;&#x4e0a;&#x4f20;&#x3002;',
        button: '&#x53bb;&#x770b;&#x76ee;&#x6807;&#x5173;&#x952e;&#x8bcd;',
        focus: 'target_keywords'
      },
      listing: {
        badge: '&#x5df2;&#x53ef;&#x5efa;&#x8bae;',
        title: 'Listing &#x8f6c;&#x5316; Agent',
        subtitle: '&#x5f53;&#x524d; Skill &#x5df2;&#x9009;&#x4e2d;',
        input: '&#x8f93;&#x5165;&#x6807;&#x9898;&#x3001;&#x4e94;&#x70b9;&#x3001;&#x5356;&#x70b9;&#x3001;&#x4e3b;&#x56fe;/A+&#x548c;&#x5e7f;&#x544a; CVR&#x3002;',
        output: '&#x8f93;&#x51fa;&#x5f71;&#x54cd;&#x8f6c;&#x5316;&#x7684;&#x95ee;&#x9898;&#xff0c;&#x4f8b;&#x5982;&#x5356;&#x70b9;&#x4e0d;&#x51c6;&#x3001;&#x4e3b;&#x56fe;&#x4e0d;&#x6e05;&#x3001;&#x9690;&#x79c1;/&#x5408;&#x89c4;&#x8868;&#x8fbe;&#x98ce;&#x9669;&#x3002;',
        next: '&#x5148;&#x4fee; Listing &#x8f6c;&#x5316;&#xff0c;&#x518d;&#x653e;&#x5927;&#x5e7f;&#x544a;&#xff0c;&#x5426;&#x5219; CPA &#x5bb9;&#x6613;&#x88ab;&#x62c9;&#x9ad8;&#x3002;',
        button: '&#x53bb;&#x770b;&#x5e7f;&#x544a; CVR',
        focus: 'target_cvr'
      },
      voc: {
        badge: '&#x5f85;&#x63a5;&#x8bc4;&#x8bba;&#x6570;&#x636e;',
        title: 'VOC &#x7528;&#x6237;&#x9700;&#x6c42; Agent',
        subtitle: '&#x5f53;&#x524d; Skill &#x5df2;&#x9009;&#x4e2d;',
        input: '&#x8f93;&#x5165;&#x81ea;&#x5df1;&#x548c;&#x7ade;&#x54c1;&#x8bc4;&#x8bba;&#xff0c;&#x5c24;&#x5176;&#x662f;&#x5dee;&#x8bc4;&#x3001;&#x9000;&#x8d27;&#x539f;&#x56e0;&#x548c;&#x4e70;&#x5bb6;&#x63d0;&#x95ee;&#x3002;',
        output: '&#x8f93;&#x51fa;&#x7528;&#x6237;&#x75db;&#x70b9;&#x3001;&#x9000;&#x8d27;&#x539f;&#x56e0;&#x3001;&#x5356;&#x70b9;&#x8865;&#x5f3a;&#x65b9;&#x5411;&#x3002;',
        next: '&#x5148;&#x51c6;&#x5907;&#x8bc4;&#x8bba;&#x6570;&#x636e;&#xff1b;&#x6ca1;&#x6709;&#x8bc4;&#x8bba;&#x8868;&#x65f6;&#xff0c;&#x8fd9;&#x4e2a; Skill &#x5148;&#x4fdd;&#x6301;&#x5f85;&#x63a5;&#x5165;&#x3002;',
        button: '&#x53bb;&#x586b;&#x7ade;&#x54c1; ASIN',
        focus: 'competitor_asins'
      },
      auto: {
        badge: '&#x9700; API &#x6388;&#x6743;',
        title: '&#x81ea;&#x52a8;&#x6267;&#x884c; Agent',
        subtitle: '&#x5f53;&#x524d; Skill &#x5df2;&#x9009;&#x4e2d;',
        input: '&#x8f93;&#x5165; Amazon Ads API &#x6388;&#x6743;&#x3002;',
        output: '&#x8f93;&#x51fa;&#x81ea;&#x52a8;&#x8c03; bid&#x3001;&#x9884;&#x7b97;&#x3001;&#x5426;&#x8bcd;&#x548c;&#x7cbe;&#x51c6;&#x8bcd;&#xff0c;&#x4f46;&#x6bcf;&#x4e2a;&#x52a8;&#x4f5c;&#x4ecd;&#x9700;&#x4f60;&#x786e;&#x8ba4;&#x540e;&#x6267;&#x884c;&#x3002;',
        next: '&#x73b0;&#x5728;&#x5148;&#x4e0d;&#x81ea;&#x52a8;&#x6539;&#x5e7f;&#x544a;&#xff0c;&#x7b49;&#x89c4;&#x5219;&#x8dd1;&#x987a;&#x540e;&#x518d;&#x63a5; API&#x3002;',
        button: '&#x770b;&#x5206;&#x6790;&#x7ed3;&#x679c;&#x533a;',
        focus: 'results'
      }
    };
    let selectedSkill = 'ads';
    function setHtml(id, value) { const el = document.getElementById(id); if (el) el.innerHTML = value || ''; }
    function setSkill(key) {
      selectedSkill = key;
      const data = skillData[key] || skillData.ads;
      skillCards.forEach(card => card.classList.toggle('active', card.dataset.skill === key));
      setHtml('skillBadge', data.badge);
      setHtml('skillTitle', data.title);
      setHtml('skillSubtitle', data.subtitle);
      setHtml('skillInput', data.input);
      setHtml('skillOutput', data.output);
      setHtml('skillNext', data.next);
      setHtml('skillPrimaryAction', data.button);
      setHtml('skillHint', '');
    }
    function focusInput(name) {
      const el = form.querySelector(`[name="${name}"]`);
      if (!el) return;
      el.scrollIntoView({ behavior:'smooth', block:'center' });
      try { el.focus({ preventScroll:true }); } catch (err) { el.focus(); }
      el.classList.add('focus-pulse');
      setTimeout(() => el.classList.remove('focus-pulse'), 1500);
      setHtml('skillHint', '&#x5df2;&#x5b9a;&#x4f4d;&#x5230;&#x5bf9;&#x5e94;&#x8f93;&#x5165;&#x4f4d;&#x7f6e;&#x3002;');
    }
    skillCards.forEach((card, index) => {
      const key = skillKeys[index];
      if (!key) return;
      card.dataset.skill = key;
      card.tabIndex = 0;
      card.setAttribute('role', 'button');
      card.addEventListener('click', () => setSkill(key));
      card.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSkill(key); }
      });
    });
    document.getElementById('skillPrimaryAction').addEventListener('click', () => {
      const data = skillData[selectedSkill] || skillData.ads;
      if (data.focus === 'results') {
        results.scrollIntoView({ behavior:'smooth', block:'center' });
        setHtml('skillHint', '&#x5148;&#x4e0a;&#x4f20;&#x62a5;&#x8868;&#x5e76;&#x8fd0;&#x884c;&#x5206;&#x6790;&#xff0c;&#x8f93;&#x51fa;&#x4f1a;&#x5728;&#x8fd9;&#x91cc;&#x663e;&#x793a;&#x3002;');
      } else {
        focusInput(data.focus);
      }
    });
    document.getElementById('skillShowResults').addEventListener('click', () => {
      results.scrollIntoView({ behavior:'smooth', block:'center' });
      setHtml('skillHint', '&#x5148;&#x4e0a;&#x4f20;&#x62a5;&#x8868;&#x5e76;&#x8fd0;&#x884c;&#x5206;&#x6790;&#xff0c;&#x8f93;&#x51fa;&#x4f1a;&#x5728;&#x8fd9;&#x91cc;&#x663e;&#x793a;&#x3002;');
    });
    setSkill('ads');
    function fmtMoney(v) { if (v === null || v === undefined || Number.isNaN(Number(v))) return '-'; return '$' + Number(v).toFixed(2); }
    function fmtPct(v) { if (v === null || v === undefined || Number.isNaN(Number(v))) return '-'; return (Number(v) * 100).toFixed(2) + '%'; }
    function esc(v) { return String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
    function metricCard(label, value) { return `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`; }
    function table(rows) {
      if (!rows || rows.length === 0) return '<div class="empty">\u6682\u65e0\u6570\u636e</div>';
      const cols = Object.keys(rows[0]);
      return `<div class="table-wrap"><table><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>` + rows.map(row => `<tr>${cols.map(c => `<td>${esc(row[c])}</td>`).join('')}</tr>`).join('') + `</tbody></table></div>`;
    }
    function actionTone(priority) { if (priority === 'P0') return 'p0'; if (priority === 'P1') return 'p1'; return ''; }
    function buildStrategyOverview(actions) {
      if (!actions || !actions.length) return `<div class="section"><h2>\u672c\u6b21\u7b56\u7565\u7ed3\u679c</h2><div class="empty">\u8fd0\u884c Agent \u5206\u6790\u540e\uff0c\u8fd9\u91cc\u4f1a\u663e\u793a\u4eca\u5929\u6700\u91cd\u8981\u7684\u52a8\u4f5c\u3002</div></div>`;
      const topActions = actions.slice(0, 6).map((item, index) => {
        const values = Object.values(item); const priority = values[0] || ''; const action = values[1] || '-'; const target = values[2] || '';
        return `<div class="card"><div class="label">\u52a8\u4f5c ${index + 1}</div><div class="value"><span class="${actionTone(priority)}">${esc(action)}</span></div><div class="label">${esc(target)}</div></div>`;
      }).join('');
      return `<div class="section"><h2>\u672c\u6b21\u7b56\u7565\u7ed3\u679c</h2><p>\u5206\u6790\u5b8c\u6210\u540e\uff0c\u6700\u5173\u952e\u7684\u52a8\u4f5c\u4f1a\u5148\u663e\u793a\u5728\u8fd9\u91cc\u3002</p><div class="cards">${topActions}</div></div>`;
    }
    function render(data) {
      const s = data.summary || {}; const e = data.economics || {};
      downloadLink.href = data.download || '#'; downloadLink.style.display = data.download ? 'inline-flex' : 'none';
      const cards = [metricCard('\u5e7f\u544a\u82b1\u8d39', fmtMoney(s.spend)), metricCard('\u5e7f\u544a\u8ba2\u5355', s.orders ?? '-'), metricCard('CPA', fmtMoney(s.cpa)), metricCard('CVR', fmtPct(s.cvr)), metricCard('\u76ee\u6807 CPC \u4e0a\u9650', fmtMoney(e.max_cpc_at_target))].join('');
      results.innerHTML = `${buildStrategyOverview(data.today_actions)}<div class="cards">${cards}</div><div class="section"><h2>\u4eca\u65e5\u52a8\u4f5c</h2>${table(data.today_actions)}</div><div class="section"><h2>\u51b2\u81ea\u7136\u6392\u540d</h2>${table(data.rank_terms)}</div><div class="section"><h2>\u5426\u8bcd / \u964d bid</h2>${table(data.negatives)}</div><div class="section"><h2>\u5e7f\u544a\u4f4d</h2>${table(data.placements)}</div><div class="section"><h2>\u5e7f\u544a\u7ec4</h2>${table(data.adgroups)}</div><div class="section"><h2>\u7ade\u54c1\u53d8\u5316</h2>${table(data.competitors)}</div><div class="section"><h2>\u5386\u53f2\u5bf9\u6bd4</h2>${table(data.ad_history_notes)}</div>`;
    }
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); const button = form.querySelector('button'); button.disabled = true; statusEl.textContent = '\u6b63\u5728\u5206\u6790 Excel\uff0c\u8bf7\u7a0d\u7b49...';
      try { const res = await fetch('/analyze', { method: 'POST', body: new FormData(form) }); const data = await res.json(); if (!res.ok || data.error) throw new Error(data.error || '\u5206\u6790\u5931\u8d25'); render(data); statusEl.textContent = '\u5206\u6790\u5b8c\u6210\u3002'; }
      catch (err) { statusEl.textContent = err.message; }
      finally { button.disabled = false; }
    });
  </script>
</body>
</html>
"""

APPROVAL_REQUIRED_TEXT = "需你确认"


def normalize_keyword_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def rules_from_form(form: dict, target_cpa: float, target_cvr: float) -> dict:
    return {
        "target_cpa": target_cpa,
        "target_cvr": target_cvr,
        "target_profit": parse_float(form.get("target_profit"), 15),
        "adgroup_no_order_spend": parse_float(form.get("adgroup_no_order_spend"), 10),
        "keyword_no_order_clicks": parse_float(form.get("keyword_no_order_clicks"), 20),
        "max_launch_loss_days": parse_float(form.get("max_launch_loss_days"), 7),
        "approval_required": True,
        "target_keywords": {
            normalize_keyword_text(item)
            for item in split_lines(parse_text(form.get("target_keywords"), DEFAULT_TARGET_KEYWORDS))
        },
    }


def risk_flag(term: str) -> bool:
    low = normalize_keyword_text(term)
    risk_terms = set(RISK_WORDS) | {"spying", "spy camera", "secret camera", "secret cameras"}
    return any(word in low for word in risk_terms)


def classify_term_clean(row: pd.Series, term_col: str, cols: dict[str, str | None], rules: dict) -> tuple[str, str, str]:
    term = str(row.get(term_col, ""))
    normalized = normalize_keyword_text(term)
    spend = float(row.get(cols.get("spend"), 0) or 0)
    clicks = float(row.get(cols.get("clicks"), 0) or 0)
    orders = float(row.get(cols.get("orders"), 0) or 0)
    cpa = row.get("CPA")
    cvr = row.get("CVR")
    target_cpa = rules["target_cpa"]
    target_cvr = rules["target_cvr"]
    target_keywords = rules["target_keywords"]

    if risk_flag(term):
        return "合规风险词", "否词/不放大，需确认", "包含 spy、secret、spying 等敏感表达，可能影响平台合规和买家信任。"
    if orders == 0 and clicks >= rules["keyword_no_order_clicks"]:
        return "否词止损", "精准否定，需确认", f"无订单且点击 {clicks:.0f} 次，达到你设定的 {rules['keyword_no_order_clicks']:.0f} 点击止损线。"
    if orders == 0 and spend >= target_cpa:
        return "否词止损", "精准否定或暂停，需确认", f"无订单且花费 {money(spend)}，已经接近/超过目标 CPA {money(target_cpa)}。"
    if orders >= 1 and pd.notna(cpa) and cpa > target_cpa:
        return "降Bid观察", "降低 bid，需确认", f"CPA {money(cpa)} 超过你设定的 {money(target_cpa)}，按规则必须降价。"
    if normalized in target_keywords and orders >= 1 and pd.notna(cvr) and cvr >= target_cvr:
        return "目标词冲排名", "Exact 独立活动 + Top of Search，需确认", "这是你的重点词，已有订单且 CVR 达标，可用于冲自然排名。"
    if normalized in target_keywords:
        return "目标词观察", "保留低预算观察，需确认", "这是你的重点词，但当前订单/CVR/CPA 还没有同时达标，先保留数据。"
    if orders >= 6 and pd.notna(cpa) and cpa <= target_cpa and pd.notna(cvr) and cvr >= target_cvr:
        return "第一梯队冲排名", "Exact 独立活动 + Top of Search，需确认", "多单、CPA 达标、CVR 达标，适合用广告订单推动自然排名。"
    if orders >= 3 and pd.notna(cpa) and cpa <= target_cpa * 1.2 and pd.notna(cvr) and cvr >= target_cvr:
        return "第二梯队小预算", "Exact 低预算测试，需确认", "转化达标但单量或 CPA 还需观察，先小预算验证。"
    return "样本不足", "继续观察", "数据还不够，不做激进动作。"


def build_search_analysis(search_df: pd.DataFrame | None, target_cpa: float, target_cvr: float, rules: dict | None = None) -> tuple[dict, pd.DataFrame, list[dict], list[dict]]:
    if search_df is None or search_df.empty:
        return {}, pd.DataFrame(), [], []
    rules = rules or {"target_cpa": target_cpa, "target_cvr": target_cvr, "keyword_no_order_clicks": 20, "target_keywords": set()}
    cols = {key: find_col(search_df, key) for key in ["search", "impressions", "clicks", "spend", "sales", "orders"]}
    if not cols["search"]:
        cols["search"] = find_col(search_df, "keyword") or find_col(search_df, "targeting")
    required = [cols[k] for k in ["search", "clicks", "spend", "orders"]]
    if any(c is None for c in required):
        raise ValueError("搜索词报告缺少必要字段：用户搜索词/点击/花费/广告订单")

    numeric_cols = [cols[k] for k in ["impressions", "clicks", "spend", "sales", "orders"]]
    df = ensure_numeric(search_df, numeric_cols)
    agg = {
        cols["clicks"]: "sum",
        cols["spend"]: "sum",
        cols["orders"]: "sum",
    }
    if cols["impressions"]:
        agg[cols["impressions"]] = "sum"
    if cols["sales"]:
        agg[cols["sales"]] = "sum"
    grouped = df.groupby(cols["search"], dropna=False).agg(agg).reset_index()
    if not cols["impressions"]:
        grouped["曝光量"] = 0
        cols["impressions"] = "曝光量"
    if not cols["sales"]:
        grouped["广告销售额"] = 0
        cols["sales"] = "广告销售额"

    grouped = metrics_for(grouped, cols, target_cpa)
    grouped[["分层", "下一步动作", "原因"]] = grouped.apply(
        lambda row: pd.Series(classify_term_clean(row, cols["search"], cols, rules)),
        axis=1,
    )
    grouped["建议竞价"] = grouped.apply(
        lambda r: min(max(float(r["Max CPC"]) if pd.notna(r["Max CPC"]) else 0.25, 0.15), 0.95),
        axis=1,
    )
    grouped["建议日预算"] = grouped.apply(
        lambda r: 40
        if r["分层"] in ["目标词冲排名", "第一梯队冲排名"] and r[cols["orders"]] >= 10
        else (25 if r["分层"] in ["目标词冲排名", "第一梯队冲排名"] else (15 if r["分层"] == "第二梯队小预算" else 0)),
        axis=1,
    )
    priority = {
        "目标词冲排名": 1,
        "第一梯队冲排名": 2,
        "第二梯队小预算": 3,
        "目标词观察": 4,
        "降Bid观察": 5,
        "否词止损": 6,
        "合规风险词": 7,
        "样本不足": 8,
    }
    grouped["排序"] = grouped["分层"].map(priority).fillna(99)
    grouped = grouped.sort_values(["排序", cols["orders"], "CPA"], ascending=[True, False, True])

    total_spend = grouped[cols["spend"]].sum()
    total_orders = grouped[cols["orders"]].sum()
    total_clicks = grouped[cols["clicks"]].sum()
    total_sales = grouped[cols["sales"]].sum()
    summary = {
        "spend": float(total_spend),
        "orders": float(total_orders),
        "sales": float(total_sales),
        "clicks": float(total_clicks),
        "cpa": float(total_spend / total_orders) if total_orders else None,
        "cvr": float(total_orders / total_clicks) if total_clicks else None,
        "acos": float(total_spend / total_sales) if total_sales else None,
        "cpc": float(total_spend / total_clicks) if total_clicks else None,
    }

    output = pd.DataFrame(
        {
            "用户搜索词": grouped[cols["search"]],
            "分层": grouped["分层"],
            "下一步动作": grouped["下一步动作"],
            "点击": grouped[cols["clicks"]],
            "花费-本币": grouped[cols["spend"]],
            "广告销售额-本币": grouped[cols["sales"]],
            "广告订单": grouped[cols["orders"]],
            "CPC": grouped["CPC"],
            "CPA": grouped["CPA"],
            "CVR": grouped["CVR"],
            "ACOS": grouped["ACOS"],
            "建议竞价": grouped["建议竞价"],
            "建议日预算": grouped["建议日预算"],
            "原因": grouped["原因"],
        }
    )
    full = output.copy()
    rank_terms = output[output["分层"].isin(["目标词冲排名", "第一梯队冲排名", "第二梯队小预算", "目标词观察"])].head(100)
    negatives = output[output["分层"].isin(["否词止损", "合规风险词", "降Bid观察"])].head(160)
    return summary, full, rank_terms.to_dict(orient="records"), negatives.to_dict(orient="records")


def build_adgroup_analysis(adgroup_df: pd.DataFrame | None, target_cpa: float, target_cvr: float, rules: dict | None = None) -> list[dict]:
    if adgroup_df is None or adgroup_df.empty:
        return []
    rules = rules or {"adgroup_no_order_spend": 10}
    cols = {key: find_col(adgroup_df, key) for key in ["campaign", "ad_group", "bid", "impressions", "clicks", "spend", "sales", "orders"]}
    if not cols["campaign"] or not cols["ad_group"]:
        return []
    df = ensure_numeric(adgroup_df, [cols[k] for k in ["bid", "impressions", "clicks", "spend", "sales", "orders"]])
    df = metrics_for(df, cols, target_cpa)

    def action(row: pd.Series) -> str:
        orders = float(row.get(cols["orders"], 0) or 0)
        spend = float(row.get(cols["spend"], 0) or 0)
        cpa = row.get("CPA")
        cvr = row.get("CVR")
        if orders == 0 and spend >= rules["adgroup_no_order_spend"]:
            return "否词/暂停止损，需确认"
        if pd.notna(cpa) and cpa > target_cpa:
            return "降 bid，需确认"
        if orders >= 1 and pd.notna(cpa) and cpa <= target_cpa and pd.notna(cvr) and cvr >= target_cvr:
            return "保留或加预算，需确认"
        return "观察"

    df["动作"] = df.apply(action, axis=1)
    df = df.sort_values([cols["orders"], "CPA"], ascending=[False, True])
    output = pd.DataFrame(
        {
            "广告活动": df[cols["campaign"]],
            "广告组": df[cols["ad_group"]],
            "当前竞价-本币": df[cols["bid"]] if cols["bid"] else 0,
            "点击": df[cols["clicks"]],
            "花费-本币": df[cols["spend"]],
            "广告销售额-本币": df[cols["sales"]] if cols["sales"] else 0,
            "广告订单": df[cols["orders"]],
            "CPC": df["CPC"],
            "CPA": df["CPA"],
            "CVR": df["CVR"],
            "ACOS": df["ACOS"],
            "动作": df["动作"],
        }
    )
    return output.head(100).to_dict(orient="records")


def build_placement_analysis(placement_df: pd.DataFrame | None, target_cpa: float, rules: dict | None = None) -> list[dict]:
    if placement_df is None or placement_df.empty:
        return []
    cols = {key: find_col(placement_df, key) for key in ["placement", "impressions", "clicks", "spend", "sales", "orders"]}
    if not cols["placement"]:
        return []
    df = ensure_numeric(placement_df, [cols[k] for k in ["impressions", "clicks", "spend", "sales", "orders"]])
    grouped = df.groupby(cols["placement"], dropna=False).agg(
        {cols["impressions"]: "sum", cols["clicks"]: "sum", cols["spend"]: "sum", cols["sales"]: "sum", cols["orders"]: "sum"}
    ).reset_index()
    grouped = metrics_for(grouped, cols, target_cpa)

    def action(row: pd.Series) -> str:
        placement = str(row.get(cols["placement"], ""))
        cpa = row.get("CPA")
        if pd.notna(cpa) and cpa > target_cpa:
            return "降广告位加权，需确认"
        if ("Top" in placement or "顶部" in placement) and pd.notna(cpa) and cpa <= target_cpa:
            return "排名词加 Top of Search，需确认"
        return "观察"

    grouped["动作"] = grouped.apply(action, axis=1)
    output = pd.DataFrame(
        {
            "广告位": grouped[cols["placement"]],
            "点击": grouped[cols["clicks"]],
            "花费-本币": grouped[cols["spend"]],
            "广告销售额-本币": grouped[cols["sales"]],
            "广告订单": grouped[cols["orders"]],
            "CPC": grouped["CPC"],
            "CPA": grouped["CPA"],
            "CVR": grouped["CVR"],
            "ACOS": grouped["ACOS"],
            "动作": grouped["动作"],
        }
    )
    return output.sort_values("广告订单", ascending=False).to_dict(orient="records")


def build_today_actions(summary: dict, rank_terms: list[dict], negatives: list[dict], placements: list[dict], competitor_changes: list[dict], target_cpa: float, target_cvr: float, rules: dict | None = None) -> list[dict]:
    rules = rules or {"target_profit": 15, "max_launch_loss_days": 7}
    actions = []
    if summary:
        if summary.get("cpa") is not None and summary["cpa"] > target_cpa:
            actions.append({"优先级": "P0", "动作": "整体降本", "对象": "CPA 高于 9 美金的活动/广告组/词", "原因": f"当前整体 CPA {money(summary['cpa'])}，超过你设定的 {money(target_cpa)}，规则要求降价。", "是否自动": APPROVAL_REQUIRED_TEXT})
        if summary.get("cvr") is not None and summary["cvr"] < target_cvr:
            actions.append({"优先级": "P0", "动作": "先修 Listing 转化", "对象": "主图/五点/A+/价格/coupon", "原因": f"当前整体 CVR {pct(summary['cvr'])}，低于目标 {pct(target_cvr)}。", "是否自动": APPROVAL_REQUIRED_TEXT})
    tier = [r for r in rank_terms if r.get("分层") in ["目标词冲排名", "第一梯队冲排名"]]
    if tier:
        terms = "、".join(str(r.get("用户搜索词", "")) for r in tier[:8])
        actions.append({"优先级": "P0", "动作": "冲自然排名", "对象": terms, "原因": "这些词在重点词库内或已满足出单/CVR条件，可建 Exact 活动、调预算、调 bid、调广告位。", "是否自动": APPROVAL_REQUIRED_TEXT})
    hard_stops = [r for r in negatives if r.get("分层") in ["否词止损", "合规风险词"]]
    if hard_stops:
        terms = "、".join(str(r.get("用户搜索词", "")) for r in hard_stops[:10])
        actions.append({"优先级": "P0", "动作": "否词止损", "对象": terms, "原因": "命中无单点击/花费止损线，或存在合规风险。", "是否自动": APPROVAL_REQUIRED_TEXT})
    bid_down = [r for r in negatives if r.get("分层") == "降Bid观察"]
    if bid_down:
        terms = "、".join(str(r.get("用户搜索词", "")) for r in bid_down[:8])
        actions.append({"优先级": "P1", "动作": "降 bid", "对象": terms, "原因": f"CPA 超过 {money(target_cpa)}，按你确认的规则必须降价。", "是否自动": APPROVAL_REQUIRED_TEXT})
    top_place = [p for p in placements if "Top" in str(p.get("广告位", "")) or "顶部" in str(p.get("广告位", ""))]
    if top_place:
        actions.append({"优先级": "P1", "动作": "调广告位", "对象": "Top of Search / 商品页广告位", "原因": "广告位会影响冲自然排名和转化，所有加减权都需要你确认。", "是否自动": APPROVAL_REQUIRED_TEXT})
    if competitor_changes:
        changed = [c for c in competitor_changes if c.get("动作") == "发现变化"]
        if changed:
            actions.append({"优先级": "P1", "动作": "竞品应对", "对象": "、".join(c["ASIN"] for c in changed[:5]), "原因": "竞品价格、coupon、评论、BSR 或主图变化可能影响你的转化。", "是否自动": APPROVAL_REQUIRED_TEXT})
        else:
            actions.append({"优先级": "P2", "动作": "补齐竞品基线", "对象": "竞品 ASIN", "原因": "先建立竞品基线，后续才能判断对手每天做了什么。", "是否自动": "无需执行"})
    actions.append({"优先级": "P2", "动作": "推品周期提醒", "对象": f"{rules['max_launch_loss_days']:.0f} 天观察窗口", "原因": f"你设定前期最大可接受亏损周期为 {rules['max_launch_loss_days']:.0f} 天，单品利润目标约 {money(rules['target_profit'])}。", "是否自动": "规则记录"})
    return actions


def save_excel_report(result: dict, term_df: pd.DataFrame) -> str:
    report_id = now_id()
    path = OUTPUTS / f"agent_daily_report_{report_id}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(result["today_actions"]).to_excel(writer, sheet_name="今日动作", index=False)
        pd.DataFrame(result["rank_terms"]).to_excel(writer, sheet_name="排名冲刺词", index=False)
        pd.DataFrame(result["negatives"]).to_excel(writer, sheet_name="否词降价", index=False)
        pd.DataFrame(result["adgroups"]).to_excel(writer, sheet_name="广告组动作", index=False)
        pd.DataFrame(result["placements"]).to_excel(writer, sheet_name="广告位策略", index=False)
        pd.DataFrame(result["competitors"]).to_excel(writer, sheet_name="竞品变化", index=False)
        pd.DataFrame(result["ad_history_notes"]).to_excel(writer, sheet_name="广告变化复盘", index=False)
        if term_df is not None and not term_df.empty:
            term_df.head(500).to_excel(writer, sheet_name="搜索词全量前500", index=False)
        pd.DataFrame(
            columns=[
                "动作日期",
                "对象类型",
                "活动",
                "广告组",
                "关键词/ASIN/搜索词",
                "动作",
                "旧值",
                "新值",
                "动作原因",
                "是否已确认",
                "24h结果",
                "48h结果",
                "72h结果",
                "7天结果",
                "结论",
            ]
        ).to_excel(writer, sheet_name="动作日志模板", index=False)
    return path.name


def analyze(form: dict, files: dict) -> dict:
    asin = parse_text(form.get("asin"), "B0GWQJMC1V").strip().upper()
    price = parse_float(form.get("price"), 59.99)
    cost_rmb = parse_float(form.get("cost_rmb"), 150)
    fx_rate = parse_float(form.get("fx_rate"), 7.15)
    return_rate = parse_float(form.get("return_rate"), 22) / 100
    target_cpa = parse_float(form.get("target_cpa"), 9)
    target_cvr = parse_float(form.get("target_cvr"), 4) / 100
    competitor_asins = parse_text(form.get("competitor_asins"), DEFAULT_COMPETITOR_ASINS)
    rules = rules_from_form(form, target_cpa, target_cvr)

    search_df = read_table(files.get("search_report"))
    adgroup_df = read_table(files.get("adgroup_report"))
    placement_df = read_table(files.get("placement_report"))
    comp_df = normalize_competitor(read_table(files.get("competitor_report")))

    summary, term_df, rank_terms, negatives = build_search_analysis(search_df, target_cpa, target_cvr, rules)
    adgroups = build_adgroup_analysis(adgroup_df, target_cpa, target_cvr, rules)
    placements = build_placement_analysis(placement_df, target_cpa, rules)
    competitors = compare_competitors(comp_df, competitor_asins)
    ad_history_notes = compare_ad_history(term_df)
    today_actions = build_today_actions(summary, rank_terms, negatives, placements, competitors, target_cpa, target_cvr, rules)
    report_name = save_excel_report(
        {
            "today_actions": today_actions,
            "rank_terms": rank_terms,
            "negatives": negatives,
            "adgroups": adgroups,
            "placements": placements,
            "competitors": competitors,
            "ad_history_notes": ad_history_notes,
        },
        term_df,
    )

    cost_usd = cost_rmb / fx_rate if fx_rate else 0
    gross_before_ad = price - cost_usd
    return_adjusted = gross_before_ad * (1 - return_rate)
    result = {
        "asin": asin,
        "summary": summary,
        "economics": {
            "price": price,
            "cost_usd": cost_usd,
            "gross_before_ad": gross_before_ad,
            "return_adjusted": return_adjusted,
            "target_cpa": target_cpa,
            "target_cvr": target_cvr,
            "target_profit": rules["target_profit"],
            "max_cpc_at_target": target_cpa * target_cvr,
            "adgroup_no_order_spend": rules["adgroup_no_order_spend"],
            "keyword_no_order_clicks": rules["keyword_no_order_clicks"],
            "max_launch_loss_days": rules["max_launch_loss_days"],
        },
        "today_actions": today_actions,
        "rank_terms": rank_terms[:80],
        "negatives": negatives[:100],
        "adgroups": adgroups[:80],
        "placements": placements,
        "competitors": competitors,
        "ad_history_notes": ad_history_notes,
        "download": f"/download?file={report_name}",
    }
    return result


LOGIN_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Amazon Ads Agent Login</title>
  <style>
    :root { --line:#dce2ea; --text:#0f172a; --muted:#64748b; --accent:#2563eb; --bad:#c2410c; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; display:grid; place-items:center; font-family:Inter,"Segoe UI",Arial,sans-serif; color:var(--text); background:#f6f7f9; }
    .box { width:min(420px, calc(100vw - 32px)); background:#fff; border:1px solid var(--line); border-radius:12px; padding:28px; box-shadow:0 18px 40px rgba(15,23,42,.08); }
    .brand { display:flex; align-items:center; gap:12px; font-weight:900; margin-bottom:22px; }
    .mark { width:34px; height:34px; border-radius:9px; display:grid; place-items:center; color:#fff; background:var(--accent); }
    h1 { font-size:22px; margin:0 0 8px; }
    p { margin:0 0 18px; color:var(--muted); line-height:1.6; }
    label { display:block; font-size:13px; font-weight:800; margin:14px 0 6px; }
    input { width:100%; border:1px solid var(--line); border-radius:8px; padding:11px 12px; font:inherit; }
    button { width:100%; border:0; border-radius:8px; background:var(--accent); color:#fff; font-weight:900; padding:12px 14px; margin-top:18px; cursor:pointer; }
    .error { color:var(--bad); font-weight:800; min-height:20px; margin-top:12px; }
  </style>
</head>
<body>
  <form class="box" method="post" action="/login">
    <div class="brand"><div class="mark">A</div><span>Amazon Ads Agent</span></div>
    <h1>&#x8f93;&#x5165;&#x8bbf;&#x95ee;&#x5bc6;&#x7801;</h1>
    <p>&#x8fd9;&#x4e2a; Agent &#x4f1a;&#x5904;&#x7406;&#x5e7f;&#x544a;&#x62a5;&#x8868;&#x548c;&#x7ade;&#x54c1;&#x6570;&#x636e;&#xff0c;&#x8bf7;&#x5148;&#x767b;&#x5f55;&#x518d;&#x4f7f;&#x7528;&#x3002;</p>
    <label>&#x5bc6;&#x7801;</label>
    <input name="password" type="password" autocomplete="current-password" autofocus />
    <button type="submit">&#x8fdb;&#x5165; Agent</button>
    <div class="error">{{ERROR}}</div>
  </form>
</body>
</html>
"""


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "AmazonAgent/0.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def send_bytes(self, content: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: dict, status: int = 200) -> None:
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie", "")
        items: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            items[key.strip()] = value.strip()
        return items

    def is_authenticated(self) -> bool:
        if not AGENT_PASSWORD:
            return True
        return secrets.compare_digest(self.cookies().get(SESSION_COOKIE_NAME, ""), SESSION_TOKEN)

    def require_auth(self) -> bool:
        if self.is_authenticated():
            return True
        if self.command == "POST":
            self.send_json({"error": "Please login first."}, 401)
        else:
            self.send_login()
        return False

    def send_login(self, error: str = "") -> None:
        message = "Password is incorrect." if error else ""
        self.send_bytes(LOGIN_HTML.replace("{{ERROR}}", message).encode("utf-8"))

    def handle_login(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        fields = parse_qs(body)
        password = fields.get("password", [""])[0]
        if AGENT_PASSWORD and secrets.compare_digest(password, AGENT_PASSWORD):
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{SESSION_COOKIE_NAME}={SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Lax")
            self.end_headers()
            return
        self.send_login("bad_password")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self.send_login()
            return
        if not self.require_auth():
            return
        if parsed.path == "/":
            self.send_bytes(HTML.encode("utf-8"))
            return
        if parsed.path == "/reports":
            self.send_json(list_reports())
            return
        if parsed.path == "/download":
            query = parse_qs(parsed.query)
            filename = Path(query.get("file", [""])[0]).name
            path = OUTPUTS / filename
            if not path.exists():
                self.send_json({"error": "文件不存在"}, 404)
                return
            content = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_json({"error": "Not found"}, 404)

    def parse_multipart(self) -> tuple[dict, dict]:
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)
        msg = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        form: dict[str, str] = {}
        files: dict[str, dict] = {}
        for part in msg.iter_parts():
            params = dict(part.get_params(header="content-disposition") or [])
            name = params.get("name")
            if not name:
                continue
            filename = params.get("filename")
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = {"filename": filename, "content": payload}
            else:
                form[name] = payload.decode("utf-8", errors="ignore")
        return form, files

    def do_POST(self) -> None:
        parsed_path = urlparse(self.path).path
        if parsed_path == "/login":
            self.handle_login()
            return
        if not self.require_auth():
            return
        if parsed_path != "/analyze":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            form, files = self.parse_multipart()
            result = analyze(form, files)
            self.send_json(result)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": str(exc)}, 500)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AgentHandler)
    print(f"Amazon Ads Agent running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
