import io
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from openpyxl import load_workbook

import news_search as ns
import excel_writer as xw

st.set_page_config(page_title="뉴스 클리핑 모니터링", page_icon="📰", layout="wide")

BASE_DIR = Path(__file__).parent

# ---------- 접속 비밀번호(선택) ----------
pw = st.secrets.get("APP_PASSWORD", "")
if pw:
    if not st.session_state.get("ok"):
        x = st.text_input("비밀번호", type="password")
        if x == pw:
            st.session_state["ok"] = True
            st.rerun()
        st.stop()

st.title("📰 ㈜셀바이오휴먼텍 뉴스클리핑 업무")

cid = st.secrets.get("NAVER_CLIENT_ID", "")
csec = st.secrets.get("NAVER_CLIENT_SECRET", "")
if not cid or not csec:
    st.error("네이버 검색 API 키가 설정되지 않았습니다. Streamlit 앱 설정의 Secrets를 확인하세요.")
    st.stop()


def find_template():
    for p in (BASE_DIR / "template.xlsx", BASE_DIR / "template" / "template.xlsx"):
        if p.exists():
            return p
    return None


# ---------- 1. 업무파일 ----------
with st.sidebar:
    st.header("1. 업무파일")
    up = st.file_uploader(
        "이번 달 업무파일(xlsx) 업로드",
        type=["xlsx"],
        help="어제 작업한 파일을 올리면 이어서 기록됩니다. 없으면 기본 양식을 사용합니다.",
    )
    if up:
        base_bytes = up.getvalue()
        src_name = up.name
        st.caption(f"사용 중: {up.name}")
    else:
        tpl = find_template()
        if tpl is None:
            st.error("기본 양식(template.xlsx)이 저장소에 없습니다. 위에서 업무파일을 직접 업로드하세요.")
            st.stop()
        base_bytes = tpl.read_bytes()
        src_name = None
        st.caption("사용 중: 기본 양식(template.xlsx)")

    # 업로드 파일이 바뀌면 작업 파일 초기화
    if "work_bytes" not in st.session_state or st.session_state.get("src") != src_name:
        st.session_state["work_bytes"] = base_bytes
        st.session_state["src"] = src_name
        st.session_state.pop("written", None)

    wb = load_workbook(io.BytesIO(st.session_state["work_bytes"]))
    sheets = [s for s in wb.sheetnames if s != "월말보고서"]

    st.header("2. 키워드(시트)")
    chosen = st.multiselect("모니터링할 키워드", sheets, default=sheets)

    now = datetime.now(ns.KST)
    st.info(f"조회 범위\n\n{ns.window_start(now):%Y.%m.%d %H:%M} ~ {now:%Y.%m.%d %H:%M}")
    work_date = st.text_input("업무일자 (0000.00.00)", value=now.strftime("%Y.%m.%d"))

# ---------- 2. 기사 검색 ----------
if st.button("🔍 기사 검색", type="primary"):
    if not chosen:
        st.warning("키워드를 하나 이상 선택하세요.")
    else:
        res = {}
        prog = st.progress(0.0)
        for i, kw in enumerate(chosen):
            try:
                res[kw] = ns.collect(kw, cid, csec)
            except Exception as e:
                st.warning(f"'{kw}' 검색 실패: {e}")
                res[kw] = []
            prog.progress((i + 1) / len(chosen))
        st.session_state["results"] = res
        st.session_state.pop("written", None)

results = st.session_state.get("results")
if not results:
    st.write("왼쪽에서 키워드를 고른 뒤 **기사 검색**을 누르세요.")
    st.stop()

# ---------- 3. 검토·수정 ----------
st.subheader("검토 후 업무파일에 넣을 기사를 선택하세요")
st.caption(
    "제목에 키워드가 있으면 '높음', 요약에만 있으면 '검토'입니다. "
    "기사의 **주 내용이 키워드와 관련 있는지**는 반드시 직접 확인하고, 요약(1~2문장)을 수정하세요."
)

edited = {}
for kw, arts in results.items():
    with st.expander(f"{kw}  ({len(arts)}건)", expanded=bool(arts)):
        if not arts:
            st.write("해당 시간대 기사가 없습니다.")
            continue
        df = pd.DataFrame(
            [
                {
                    "선택": a["relevance"] == "높음",
                    "관련도": a["relevance"],
                    "기사발행일": a["pub"].strftime("%Y.%m.%d"),
                    "발행시각": a["pub"].strftime("%H:%M"),
                    "제목": a["title"],
                    "요약": ns.draft_summary(a, kw),
                    "URL": a["url"],
                }
                for a in arts
            ]
        )
        edited[kw] = st.data_editor(
            df,
            key=f"ed_{kw}",
            hide_index=True,
            column_config={
                "선택": st.column_config.CheckboxColumn(width="small"),
                "URL": st.column_config.LinkColumn(),
                "요약": st.column_config.TextColumn(width="large"),
            },
            disabled=["관련도", "기사발행일", "발행시각", "URL"],
        )

# ---------- 4. 엑셀 생성 ----------
if st.button("📥 업무파일에 기록하고 다운로드 준비"):
    by_sheet, problems = {}, []
    for kw, df in edited.items():
        sel = df[df["선택"]]
        items = []
        for _, r in sel.iterrows():
            if not str(r["제목"]).strip() or not str(r["URL"]).startswith("http") or not str(r["요약"]).strip():
                problems.append(f"{kw}: 제목/URL/요약이 비어 있는 행이 있어 제외했습니다.")
                continue
            items.append(
                {
                    "pub_date": r["기사발행일"],
                    "keyword": kw,
                    "summary": str(r["요약"]).strip(),
                    "title": str(r["제목"]).strip(),
                    "url": r["URL"],
                }
            )
        by_sheet[kw] = items
    for p in problems:
        st.warning(p)
    out, written = xw.write_articles(st.session_state["work_bytes"], by_sheet, work_date)
    st.session_state["work_bytes"] = out
    st.session_state["written"] = written
    st.session_state["done_name"] = (
        f"㈜셀바이오휴먼텍 뉴스클리핑 업무파일({datetime.now(ns.KST).month}월_한철희).xlsx"
    )

if st.session_state.get("written") is not None:
    w = st.session_state["written"]
    if w:
        st.success("기록 완료: " + ", ".join(f"{k} {v}건" for k, v in w.items()))
    else:
        st.info("선택된 기사가 없습니다.")
    st.download_button(
        "⬇️ 업무파일 다운로드",
        st.session_state["work_bytes"],
        file_name=st.session_state["done_name"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    st.warning("제출 전 확인: 다운로드한 파일에서 하이퍼링크를 클릭해 정상 연결되는지 확인하세요.")
