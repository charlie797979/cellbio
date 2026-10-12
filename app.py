import io
import re
from datetime import datetime, time
from pathlib import Path

import pandas as pd
import streamlit as st
from openpyxl import load_workbook

import news_search as ns
import excel_writer as xw

st.set_page_config(page_title="뉴스 클리핑 모니터링", page_icon="📰", layout="wide")

BASE_DIR = Path(__file__).parent

# 원래의 조회 시작 시각 함수를 보관 (재실행되어도 덮어쓰이지 않도록 한 번만 저장)
if not hasattr(ns, "_orig_window_start"):
    ns._orig_window_start = ns.window_start

# ---------- 제외 규칙 (필요하면 단어를 추가/삭제하세요) ----------
STOCK_WORDS = [
    "주가", "목표주가", "목표가", "투자의견", "투자 의견", "증권", "코스피", "코스닥",
    "종목", "증시", "상한가", "하한가", "급등", "급락", "순매수", "순매도",
    "매매동향", "시가총액", "테마주", "특징주", "컨센서스", "공매도", "ETF",
    "개미", "외국인 매수", "외국인 매도", "기관 매수", "기관 매도", "리포트",
    "매수 의견", "매도 의견", "주식", "배당",
]
# 제목에 '매출/영업이익/실적' 등이 '증가/성장/호조...'와 함께 나오는 단순 실적 기사
REVENUE_RE = re.compile(
    r"(매출|영업이익|영업익|순이익|실적|수익|흑자)[^,.…]{0,12}"
    r"(증가|상승|성장|개선|호조|급증|늘|최대|돌파|훨훨|껑충|기대|전망|웃돌)"
)
EARNINGS_WORDS = ["어닝", "서프라이즈", "분기 실적", "잠정실적", "잠정 실적"]


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=ns.KST)


def exclude_reason(article, kw):
    """제외해야 하는 기사면 사유(문자열), 아니면 None."""
    title = str(article.get("title", ""))
    text = title + " " + str(ns.draft_summary(article, kw))
    for w in STOCK_WORDS:
        if w in title:
            return f"주식 관련 ('{w}')"
    # 요약에는 주식 단어가 2개 이상 있을 때만 제외 (오탐 방지)
    hits = [w for w in STOCK_WORDS if w in text]
    if len(hits) >= 2:
        return f"주식 관련 ('{hits[0]}', '{hits[1]}')"
    if REVENUE_RE.search(title) or any(w in title for w in EARNINGS_WORDS):
        return "단순 실적/매출 증가"
    return None


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

    # ---------- 3. 조회 범위 (직접 변경 가능) ----------
    st.header("3. 조회 범위")
    now = datetime.now(ns.KST)
    default_start = _aware(ns._orig_window_start(now))

    c1, c2 = st.columns(2)
    start_date = c1.date_input("시작 날짜", value=default_start.date(), key="start_date")
    start_time = c2.time_input(
        "시작 시각", value=time(default_start.hour, default_start.minute), key="start_time"
    )

    c3, c4 = st.columns(2)
    end_date = c3.date_input("종료 날짜", value=now.date(), key="end_date")
    end_time = c4.time_input("종료 시각", value=time(now.hour, now.minute), key="end_time")
    end_dt = datetime.combine(end_date, end_time).replace(tzinfo=ns.KST)

    start_dt = datetime.combine(start_date, start_time).replace(tzinfo=ns.KST)
    if start_dt >= end_dt:
        st.error("시작 일시가 종료 일시보다 빠르거나 같아야 합니다.")
        st.stop()

    st.info(f"조회 범위\n\n{start_dt:%Y.%m.%d %H:%M} ~ {end_dt:%Y.%m.%d %H:%M}")

    # ---------- 4. 제외 설정 ----------
    st.header("4. 기사 제외")
    use_filter = st.checkbox("주식·단순 매출 증가 기사 제외", value=True)

    work_date = st.text_input("업무일자 (0000.00.00)", value=now.strftime("%Y.%m.%d"))

# ---------- 기사 검색 ----------
if st.button("🔍 기사 검색", type="primary"):
    if not chosen:
        st.warning("키워드를 하나 이상 선택하세요.")
    else:
        res, excluded = {}, {}
        prog = st.progress(0.0)
        # 검색 동안만 조회 시작 시각을 사용자가 고른 값으로 바꿨다가 원래대로 되돌림
        ns.window_start = lambda _now, _s=start_dt: _s
        try:
            for i, kw in enumerate(chosen):
                try:
                    arts = ns.collect(kw, cid, csec)
                except Exception as e:
                    st.warning(f"'{kw}' 검색 실패: {e}")
                    arts = []

                # 선택한 기간 안의 기사만 남김
                arts = [a for a in arts if start_dt <= _aware(a["pub"]) <= end_dt]

                kept, dropped = [], []
                for a in arts:
                    reason = exclude_reason(a, kw) if use_filter else None
                    if reason:
                        dropped.append((a, reason))
                    else:
                        kept.append(a)
                res[kw] = kept
                excluded[kw] = dropped
                prog.progress((i + 1) / len(chosen))
        finally:
            ns.window_start = ns._orig_window_start
        st.session_state["results"] = res
        st.session_state["excluded"] = excluded
        st.session_state.pop("written", None)

results = st.session_state.get("results")
if not results:
    st.write("왼쪽에서 키워드를 고른 뒤 **기사 검색**을 누르세요.")
    st.stop()

excluded = st.session_state.get("excluded", {})

# ---------- 검토·수정 ----------
st.subheader("검토 후 업무파일에 넣을 기사를 선택하세요")
st.caption(
    "제목에 키워드가 있으면 '높음', 요약에만 있으면 '검토'입니다. "
    "기사의 **주 내용이 키워드와 관련 있는지**는 반드시 직접 확인하고, 요약(1~2문장)을 수정하세요."
)

edited = {}
for kw, arts in results.items():
    n_drop = len(excluded.get(kw, []))
    label = f"{kw}  ({len(arts)}건" + (f", 제외 {n_drop}건)" if n_drop else ")")
    with st.expander(label, expanded=bool(arts)):
        if not arts:
            st.write("해당 기간에 조건에 맞는 기사가 없습니다.")
        else:
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

        if n_drop:
            st.caption("제외된 기사 (잘못 제외됐는지 확인용)")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"제외 사유": r, "제목": a["title"], "URL": a["url"]}
                        for a, r in excluded[kw]
                    ]
                ),
                hide_index=True,
                column_config={"URL": st.column_config.LinkColumn()},
            )

# ---------- 엑셀 생성 ----------
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
