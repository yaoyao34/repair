import streamlit as st
import pandas as pd
from datetime import datetime, date
from zoneinfo import ZoneInfo
import re
import gspread
from google.oauth2.service_account import Credentials

# ====== 基本設定 ======
st.set_page_config(page_title="報修 / 維修整合系統", page_icon="🛠️", layout="wide")

REPAIR_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSf3uHqIqLqJyIDHCp1ZyQyP0edOGbNDKNSisHHpt0LtoBPs8w/viewform?usp=header"
TZ = ZoneInfo("Asia/Taipei")
PAGE_SIZE = 10


# ================= 工具 =================
def norm(x):
    if x is None:
        return ""
    return str(x).strip()

def now_ts_full():
    # 台灣時間（含秒）
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


# ================= 讀資料（穩定版） =================
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
        sh.worksheet("報修資料"),
        ["時間戳記", "班級地點", "損壞設備", "損壞情形描述", "照片或影片", "案件編號"]
    )

    repair = read_sheet_as_df(
        sh.worksheet("維修紀錄"),
        ["時間戳記", "案件編號", "處理進度", "維修說明"]
    )

    pwd = norm(sh.worksheet("密碼設定").acell("A1").value)
    return report, repair, pwd


# ================= 寫回（台灣時間完整） =================
def save_repair(case_id, status, note):
    ws = gc.open_by_url(SHEET_URL).worksheet("維修紀錄")
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


# ================= UI 小元件 =================
def kpi_cards(df_all: pd.DataFrame):
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


# ================= 主程式 =================
def main():
    st.markdown("## 報修 / 維修整合系統")

    report, repair, correct_pwd = load_data()

    # ---- 合併（先不做篩選，方便 KPI 正確）----
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

    # ===== 置頂操作區：報修按鈕（免登入）+ 登入 =====
    top1, top2 = st.columns([2, 1])
    with top1:
        st.markdown(
            f"""
            <div style="padding:12px 14px;border:1px solid #e6e6e6;border-radius:12px;background:#fafafa;">
              <div style="font-size:16px;font-weight:700;margin-bottom:6px;">快速報修</div>
              <div style="font-size:13px;color:#444;margin-bottom:10px;">
                不需要登入，直接填 Google 表單報修（可上傳多張照片/影片）。
              </div>
              <a href="{REPAIR_FORM_URL}" target="_blank"
                 style="display:inline-block;padding:10px 14px;border-radius:10px;
                        background:#1f77b4;color:white;text-decoration:none;font-weight:700;">
                開啟報修表單
              </a>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with top2:
        st.markdown(
            """
            <div style="padding:12px 14px;border:1px solid #e6e6e6;border-radius:12px;background:#ffffff;">
              <div style="font-size:16px;font-weight:700;margin-bottom:6px;">管理登入</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        pwd = st.text_input("密碼", type="password", label_visibility="collapsed", placeholder="輸入密碼")
        authed = (correct_pwd == "") or (pwd == correct_pwd)
        st.caption("登入後可編修維修進度與匯出。")

    st.divider()

    # ===== KPI =====
    kpi_cards(df_all)

    st.divider()

    # ---- Sidebar：搜尋/篩選（更清楚）----
    with st.sidebar:
        st.subheader("查詢 / 篩選")
        keyword = st.text_input("關鍵字", placeholder="例如：電腦教室 / 投影機 / 無法開機")
        status_list = sorted(set(df_all["處理進度"].fillna("").astype(str).tolist()))
        status_filter = st.multiselect("處理進度", status_list, default=[])

        st.divider()
        st.subheader("匯出")
        if not authed:
            st.caption("需登入後才可匯出（之後可再加 PDF）。")
        else:
            st.caption("登入狀態：可匯出（目前先保留介面位置）")

    # ---- 套用搜尋/篩選 ----
    df = df_all.copy()

    if keyword:
        k = keyword.lower()
        def hit(row):
            text = " ".join([
                str(row.get("報修時間","")),
                str(row.get("班級地點","")),
                str(row.get("損壞設備","")),
                str(row.get("損壞情形描述","")),
                str(row.get("維修說明","")),
                str(row.get("處理進度","")),
            ]).lower()
            return k in text
        df = df[df.apply(hit, axis=1)]

    if status_filter:
        df = df[df["處理進度"].astype(str).isin(status_filter)]

    # ---- 分頁：固定 10 筆 ----
    total = len(df)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = st.number_input("頁碼", 1, pages, 1)

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_df = df.iloc[start:end]

    st.caption(f"共 {total} 筆，顯示第 {start+1}–{min(end, total)} 筆（第 {page}/{pages} 頁）")

    # ---- 列表 ----
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
            # 報修資訊
            st.markdown(f"**報修時間**：{row.get('報修時間','')}")
            st.markdown(f"**損壞情形**：{row.get('損壞情形描述','')}")

            # 報修照片/影片連結
            links = split_links(row.get("照片或影片", ""))
            if links:
                st.markdown("**照片 / 影片（點連結查看）**")
                for j, url in enumerate(links, 1):
                    st.markdown(f"- [{media_label(url, j)}]({url})")

            st.divider()

            # 維修資訊（顯示完整時間）
            if last_update:
                st.caption(f"維修更新時間（完整）：{last_update}")
            else:
                st.caption("維修更新時間（完整）：（尚無維修紀錄）")

            if not authed:
                st.markdown(f"**處理進度**：{row.get('處理進度','')}")
                st.markdown(f"**維修說明**：{row.get('維修說明','')}")
                continue

            # 登入後可編修
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
