import streamlit as st
import pandas as pd
from datetime import datetime, date
from zoneinfo import ZoneInfo
import re
import io
import gspread
from google.oauth2.service_account import Credentials

# ===== PDF (可選) =====
REPORTLAB_OK = True
try:
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
except Exception:
    REPORTLAB_OK = False

# ===== 基本設定 =====
st.set_page_config(page_title="秀水高工資訊設備報修", page_icon="🛠️", layout="wide")

REPAIR_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSf3uHqIqLqJyIDHCp1ZyQyP0edOGbNDKNSisHHpt0LtoBPs8w/viewform?usp=header"
TZ = ZoneInfo("Asia/Taipei")
PAGE_SIZE = 10

# ===== Sheets 設定 =====
REPORT_SHEET_NAME = "報修資料"
REPAIR_SHEET_NAME = "維修紀錄"
PASSWORD_SHEET_NAME = "密碼設定"


# ================= 工具 =================
def norm(x):
    if x is None:
        return ""
    return str(x).strip()

def now_ts_full():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def to_ymd(ts):
    s = norm(ts)
    if not s:
        return ""
    d = pd.to_datetime(s, errors="coerce")
    if not pd.isna(d):
        return d.strftime("%Y-%m-%d")
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        y, mo, da = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{y}-{mo:02d}-{da:02d}"
    return ""

def fmt_24h(ts: str) -> str:
    """
    例：2025/12/12 下午 10:01:49  ->  2025/12/12 22:01:49
    轉不了就回傳原字串
    """
    s = norm(ts)
    if not s:
        return ""

    d = pd.to_datetime(s, errors="coerce")
    if pd.isna(d):
        return s

    return d.strftime("%Y/%m/%d %H:%M:%S")


def split_links(cell):
    if not cell:
        return []
    return [x.strip() for x in str(cell).split(",") if x.strip()]

def media_label(url, i):
    u = (url or "").lower()
    if any(u.endswith(e) for e in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
        return f"照片 {i}"
    if any(u.endswith(e) for e in [".mp4", ".mov", ".webm", ".mkv"]):
        return f"影片 {i}"
    return f"檔案 {i}"

def status_icon(s):
    s = s or ""
    if "已完成" in s:
        return "✅"
    if "送修" in s:
        return "🚚"
    if "待料" in s:
        return "📦"
    if "處理中" in s:
        return "🛠️"
    if "退回" in s or "無法" in s:
        return "⛔"
    if "待觀查" in s:
        return "👀"
    return "🔧"

def safe_key(s):
    s = norm(s)
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "_", s)
    return s[:80]


# ================= GSpread =================
SHEET_URL = st.secrets["SHEET_URL"]

@st.cache_resource
def gs_client():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["google_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)

gc = gs_client()


# ================= 讀資料（穩定版，避免 duplicates header） =================
def read_sheet_as_df(ws, headers):
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame(columns=headers)

    header = values[0]
    rows = values[1:]

    idx_map = {}
    for i, h in enumerate(header):
        h = norm(h)
        if h and h not in idx_map:
            idx_map[h] = i

    data = {h: [] for h in headers}
    for r in rows:
        for h in headers:
            i = idx_map.get(h)
            data[h].append(r[i] if i is not None and i < len(r) else "")

    return pd.DataFrame(data)


@st.cache_data(ttl=120)
def load_data():
    sh = gc.open_by_url(SHEET_URL)

    report = read_sheet_as_df(
        sh.worksheet(REPORT_SHEET_NAME),
        ["時間戳記", "班級地點", "損壞設備", "損壞情形描述", "照片或影片", "案件編號"]
    )

    repair = read_sheet_as_df(
        sh.worksheet(REPAIR_SHEET_NAME),
        ["時間戳記", "案件編號", "處理進度", "維修說明"]
    )

    pwd = norm(sh.worksheet(PASSWORD_SHEET_NAME).acell("A1").value)
    return report, repair, pwd


# ================= 寫回（台灣時間完整） =================
def save_repair(case_id, status, note):
    ws = gc.open_by_url(SHEET_URL).worksheet(REPAIR_SHEET_NAME)
    values = ws.get_all_values()
    if not values:
        raise RuntimeError("維修紀錄工作表為空或讀取失敗")
    header = values[0]

    def col(name):
        for i, h in enumerate(header):
            if norm(h) == name:
                return i
        return None

    c_ts, c_case, c_stat, c_note = map(col, ["時間戳記", "案件編號", "處理進度", "維修說明"])
    if None in (c_ts, c_case, c_stat, c_note):
        raise RuntimeError("維修紀錄表頭缺少必要欄位：時間戳記/案件編號/處理進度/維修說明")

    ts = now_ts_full()

    # 同案件多筆：更新最後一筆；沒有就新增
    last = None
    for i in range(1, len(values)):
        row = values[i]
        if c_case < len(row) and norm(row[c_case]) == case_id:
            last = i + 1

    if last:
        ws.update_cell(last, c_ts + 1, ts)
        ws.update_cell(last, c_stat + 1, status)
        ws.update_cell(last, c_note + 1, note)
    else:
        row = [""] * len(header)
        row[c_ts] = ts
        row[c_case] = case_id
        row[c_stat] = status
        row[c_note] = note
        ws.append_row(row, value_input_option="USER_ENTERED")


# ================= PDF 匯出（登入後） =================
def build_export_df(df_all: pd.DataFrame) -> pd.DataFrame:
    """
    欄位：報修時間、班級地點、損壞設備、完工時間、處理進度、維修說明
    報修時間：24 小時制
    完工時間：僅當處理進度含「已完成」才填維修更新時間
    """
    out = pd.DataFrame()
    out["報修時間"] = df_all["報修時間"].apply(fmt_24h)
    out["班級地點"] = df_all["班級地點"].astype(str)
    out["損壞設備"] = df_all["損壞設備"].astype(str)

    def done_time(row):
        s = str(row.get("處理進度", ""))
        if "已完成" in s:
            return str(row.get("維修更新時間", ""))
        return ""

    out["完工時間"] = df_all.apply(done_time, axis=1)
    out["處理進度"] = df_all["處理進度"].astype(str)
    out["維修說明"] = df_all["維修說明"].astype(str)
    return out.fillna("")


def make_pdf_bytes(title: str, df_export: pd.DataFrame) -> bytes:
    """
    PDF：中文字型 + 自動換行（Paragraph）
    """
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    font = "STSong-Light"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24,
        title=title
    )

    styles = getSampleStyleSheet()
    styleN = styles["Normal"]
    styleN.fontName = font
    styleN.fontSize = 8.8
    styleN.leading = 11

    styleH = styles["Heading2"]
    styleH.fontName = font

    elements = []
    elements.append(Paragraph(title, styleH))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(f"匯出時間：{now_ts_full()}", styleN))
    elements.append(Spacer(1, 10))

    headers = ["報修時間", "班級地點", "損壞設備", "完工時間", "處理進度", "維修說明"]
    data = [[Paragraph(h, styleN) for h in headers]]

    def P(x):
        s = norm(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = s.replace("\n", "<br/>")
        return Paragraph(s, styleN)

    for _, r in df_export.iterrows():
        data.append([
            P(r.get("報修時間", "")),
            P(r.get("班級地點", "")),
            P(r.get("損壞設備", "")),
            P(r.get("完工時間", "")),
            P(r.get("處理進度", "")),
            P(r.get("維修說明", "")),
        ])

    col_widths = [85, 85, 85, 85, 60, 165]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 8.8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F0F0")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("WORDWRAP", (0, 0), (-1, -1), "CJK"),
    ]))

    elements.append(table)
    doc.build(elements)
    return buf.getvalue()


# ================= 主程式 =================
def main():
    report, repair, correct_pwd = load_data()

    # ==== 合併資料（先合併再篩選） ====
    r = report.copy()
    r["案件編號"] = r["案件編號"].astype(str).str.strip()
    r["_ts"] = pd.to_datetime(r["時間戳記"], errors="coerce")
    r = r.sort_values("_ts").groupby("案件編號", as_index=False).tail(1).drop(columns=["_ts"])
    r["報修日期"] = r["時間戳記"].apply(to_ymd)
    r = r.rename(columns={"時間戳記": "報修時間"})  # 保留完整報修時間

    w = repair.copy()
    w["案件編號"] = w["案件編號"].astype(str).str.strip()
    w["_ts"] = pd.to_datetime(w["時間戳記"], errors="coerce")
    w = w.sort_values("_ts").groupby("案件編號", as_index=False).tail(1).drop(columns=["_ts"])
    w = w.rename(columns={"時間戳記": "維修更新時間"})

    df_all = r.merge(
        w[["案件編號", "維修更新時間", "處理進度", "維修說明"]],
        on="案件編號",
        how="left"
    ).fillna("")
    df_all = df_all.sort_values("報修日期", ascending=False)

    # ==== Sidebar：登入/查詢/匯出 ====
    with st.sidebar:
        st.title("管理 / 查詢")

        st.subheader("管理登入")
        pwd = st.text_input("密碼", type="password", placeholder="輸入密碼")
        authed = (correct_pwd == "") or (pwd == correct_pwd)
        st.caption("登入後可編修維修進度與匯出 PDF。")

        st.divider()
        st.subheader("查詢 / 篩選")
        keyword = st.text_input("關鍵字", placeholder="地點 / 設備 / 描述 / 維修")

        status_list = sorted(set(df_all["處理進度"].fillna("").astype(str).tolist()))
        status_filter = st.multiselect("處理進度", status_list, default=[])

        st.divider()
        st.subheader("匯出維修紀錄（PDF）")

        all_dates = pd.to_datetime(df_all["報修日期"], errors="coerce")
        min_d = all_dates.min().date() if pd.notna(all_dates.min()) else date.today()
        max_d = all_dates.max().date() if pd.notna(all_dates.max()) else date.today()

        start_d = st.date_input("報修日期起", value=min_d)
        end_d = st.date_input("報修日期迄", value=max_d)

        if not authed:
            st.warning("需登入後才能匯出。")
        else:
            if start_d > end_d:
                st.error("日期範圍錯誤：起始日期不可大於結束日期。")
            else:
                dcol = pd.to_datetime(df_all["報修日期"], errors="coerce").dt.date
                df_range = df_all[(dcol >= start_d) & (dcol <= end_d)].copy()
                exp_df = build_export_df(df_range)

                if REPORTLAB_OK:
                    if st.button("產生 PDF", type="primary"):
                        title = f"維修紀錄（{start_d.strftime('%Y-%m-%d')} ～ {end_d.strftime('%Y-%m-%d')}）"
                        pdf_bytes = make_pdf_bytes(title, exp_df)
                        filename = f"維修紀錄_{start_d.strftime('%Y%m%d')}-{end_d.strftime('%Y%m%d')}.pdf"
                        st.download_button("下載 PDF", data=pdf_bytes, file_name=filename, mime="application/pdf")
                else:
                    st.error("目前環境未安裝 reportlab，無法產生 PDF。")
                    st.caption("解法：requirements.txt 加上 reportlab，重新部署即可。")
                    csv_bytes = exp_df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button("下載 CSV（備援）", data=csv_bytes, file_name="維修紀錄.csv", mime="text/csv")

    # ==== 頁首：標題 + 右側超大報修按鈕（同標題等級） ====
    left, right = st.columns([7, 3])
    with left:
        st.title("秀水高工資訊設備報修")
    with right:
        st.markdown(
            f"""
            <div style="display:flex;justify-content:flex-end;align-items:center;height:76px;">
              <a href="{REPAIR_FORM_URL}" target="_blank"
                 style="
                    font-size: 2.1rem; font-weight: 800; line-height: 1;
                    padding: 10px 18px; border-radius: 14px;
                    border: 2px solid rgba(255,255,255,.35);
                    background: rgba(31,119,180,.18);
                    text-decoration: none;
                    ">
                 報修
              </a>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ==== KPI ====
    total = len(df_all)
    done = (df_all["處理進度"].astype(str).str.contains("已完成", na=False)).sum()
    inprog = (df_all["處理進度"].astype(str).str.contains("處理中", na=False)).sum()
    watch = (df_all["處理進度"].astype(str).str.contains("待觀查", na=False)).sum()
    pending = (df_all["處理進度"].astype(str).str.contains("待料|送修", regex=True, na=False)).sum()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("全部案件", total)
    c2.metric("已完成", done)
    c3.metric("處理中", inprog)
    c4.metric("待觀查", watch)
    c5.metric("待料/送修", pending)

    st.divider()

    # ==== 套用搜尋/篩選（僅影響畫面，不影響匯出） ====
    df = df_all.copy()
    if keyword:
        k = keyword.lower()

        def hit(row):
            text = " ".join([
                str(row.get("報修時間", "")),
                str(row.get("班級地點", "")),
                str(row.get("損壞設備", "")),
                str(row.get("損壞情形描述", "")),
                str(row.get("維修說明", "")),
                str(row.get("處理進度", "")),
            ]).lower()
            return k in text

        df = df[df.apply(hit, axis=1)]

    if status_filter:
        df = df[df["處理進度"].astype(str).isin(status_filter)]

    # ==== 分頁（10 筆） ====
    total_show = len(df)
    pages = max(1, (total_show + PAGE_SIZE - 1) // PAGE_SIZE)
    page = st.number_input("頁碼", 1, pages, 1)

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_df = df.iloc[start:end]

    st.caption(f"共 {total_show} 筆，顯示第 {start+1}–{min(end, total_show)} 筆（第 {page}/{pages} 頁）")

    # ==== 列表 ====
    for i, row in enumerate(page_df.to_dict("records")):
        icon = status_icon(row.get("處理進度", ""))
        last_update = norm(row.get("維修更新時間", ""))
        update_tag = f"｜維修更新：{last_update}" if last_update else "｜維修更新：—"

        title = (
            f'{row.get("報修日期","")}｜{row.get("班級地點","")}｜{row.get("損壞設備","")}'
            f'｜{icon} {row.get("處理進度","")}{update_tag}'
        ).strip()

        case_id = norm(row.get("案件編號", ""))
        form_key = f"f_{safe_key(case_id)}_{page}_{i}"

        with st.expander(title, expanded=False):
            st.markdown(f"**報修時間**：{row.get('報修時間','')}")
            st.markdown(f"**損壞情形**：{row.get('損壞情形描述','')}")

            links = split_links(row.get("照片或影片", ""))
            if links:
                st.markdown("**照片 / 影片（點連結查看）**")
                for j, url in enumerate(links, 1):
                    st.markdown(f"- [{media_label(url, j)}]({url})")

            st.divider()

            if last_update:
                st.caption(f"維修更新時間（完整）：{last_update}")
            else:
                st.caption("維修更新時間（完整）：（尚無維修紀錄）")

            if not authed:
                st.markdown(f"**處理進度**：{row.get('處理進度','')}")
                st.markdown(f"**維修說明**：{row.get('維修說明','')}")
                continue

            with st.form(form_key):
                options = ["", "待觀查", "處理中", "待料", "送修", "已完成", "退回/無法處理"]
                cur = row.get("處理進度", "") if row.get("處理進度", "") in options else ""
                status = st.selectbox("處理進度", options, index=options.index(cur))
                note = st.text_area("維修說明", row.get("維修說明", ""))

                st.caption(f"本次儲存時間（台灣）：{now_ts_full()}")

                if st.form_submit_button("儲存", type="primary"):
                    save_repair(case_id, status, note)
                    st.success("已儲存")
                    st.cache_data.clear()
                    st.rerun()


if __name__ == "__main__":
    main()

