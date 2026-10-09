import html
import io
import os
import re
import time
from datetime import datetime, timedelta, time as dttime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from openpyxl import load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

KST = ZoneInfo("Asia/Seoul")
NAVER_API_URL = "https://openapi.naver.com/v1/search/news.json"

# 회사 서식의 탭 이름과 검색 키워드
COMPANIES = {
    "셀바이오휴먼텍": "셀바이오휴먼텍",
    "코스맥스": "코스맥스",
    "한국콜마": "한국콜마",
    "LG생활건강": "LG생활건강",
    "이미인": "이미인",
    "코스메카코리아": "코스메카코리아",
}
HEADER_ROW = 15  # 실제 서식의 제목 행은 각 탭에서 자동 탐색합니다.
DATA_START_ROW = 18  # 예시 행(적합/부적합)은 보존하고 실제 데이터 영역부터 덮어씁니다.


def clean_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).replace("\xa0", " ").strip()


def parse_pubdate(value):
    """네이버 API의 RFC 2822 발행일을 한국 시간으로 변환."""
    dt = parsedate_to_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(KST)


def naver_news(keyword, client_id, client_secret, max_results=1000):
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    collected = []
    for start in range(1, min(max_results, 1000) + 1, 100):
        params = {"query": keyword, "display": min(100, max_results - len(collected)), "start": start, "sort": "date"}
        if params["display"] <= 0:
            break
        try:
            response = requests.get(NAVER_API_URL, headers=headers, params=params, timeout=25)
            if response.status_code != 200:
                detail = ""
                try:
                    detail = response.json().get("errorMessage", "")
                except Exception:
                    detail = response.text[:200]
                raise RuntimeError(f"네이버 API 오류 ({response.status_code}): {detail}")
            items = response.json().get("items", [])
            if not items:
                break
            collected.extend(items)
            if len(items) < params["display"]:
                break
            time.sleep(0.12)
        except requests.RequestException as exc:
            raise RuntimeError(f"네이버 API 연결 오류: {exc}") from exc
    return collected


SUMMARY_ERRORS = []


def fallback_summary(title):
    """AI 요약이 불가능할 때 설명문을 통째로 붙이지 않고 제목만 간결하게 사용."""
    text = re.sub(r"\s+", " ", clean_html(title)).strip().strip(" .。!?！？")
    # 제목의 매체명/괄호 부제를 일부 정리
    text = re.sub(r"\s*[-|｜]\s*[^-|｜]{1,25}$", "", text).strip()
    if not text:
        return "기사 제목을 확인해 주세요."
    if len(text) > 75:
        text = text[:72].rsplit(" ", 1)[0].rstrip(" ,，:：-—") + "…"
    return text if text.endswith((".", "!", "?", "다", "했다", "됐다", "밝혔다", "전했다", "추진한다", "확대한다", "강화한다", "기록했다")) else text + "."


def one_sentence_summary(title, description, gemini_key=None):
    """제목·설명 기반 AI 요약. API 오류를 숨기지 않고 기록한다."""
    title = clean_html(title)
    description = clean_html(description)
    if not gemini_key:
        SUMMARY_ERRORS.append("Gemini API 키가 없어 제목 기반 대체문을 사용했습니다.")
        return fallback_summary(title)

    prompt = f"""너는 한국 기업의 뉴스 클리핑 보고서 편집자다.
기사 제목과 설명에 명시된 사실만 사용해 아래 조건에 맞춰 요약하라.
- 한국어 한 문장, 공백 포함 40~60자 내외(최대 70자).
- 가장 중요한 사실 하나만 선택하고, 회사와 직접 관련된 내용 위주로 쓴다.
- 인사 기사면 인사 내용, 실적 기사면 실적 수치, 주가 기사면 주가 변동만 요약한다. 서로 다른 주제를 섞지 않는다.
- 기사 제목이나 설명을 길게 이어 붙이지 않는다.
- 기사에 없는 평가, 전망, 원인, 수치를 추측하지 않는다.
- 결과 문장만 출력하고 '요약:'이나 따옴표를 붙이지 않는다.
기사 제목: {title}
기사 설명: {description}
한 문장 요약:"""
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": gemini_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 100},
            },
            timeout=30,
        )
        if response.status_code != 200:
            try:
                detail = response.json().get("error", {}).get("message", response.text[:180])
            except Exception:
                detail = response.text[:180]
            raise RuntimeError(f"Gemini API 오류 ({response.status_code}): {detail}")

        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini가 요약 결과를 반환하지 않았습니다.")
        parts = candidates[0].get("content", {}).get("parts", [])
        summary = " ".join(part.get("text", "") for part in parts).strip()
        summary = re.sub(r"^(요약[:：]\s*|['\"“”‘’]+)", "", summary)
        summary = re.sub(r"\s+", " ", summary).strip().strip("'\"“”‘’ ")
        # 한 문장만 취하고, 너무 긴 결과는 잘라 왜곡하지 말고 오류로 처리한다.
        pieces = re.split(r"(?<=[.!?。！？])\s+", summary)
        summary = pieces[0].strip() if pieces else summary
        if not summary:
            raise RuntimeError("Gemini가 빈 요약을 반환했습니다.")
        if len(summary) > 90:
            raise RuntimeError(f"요약이 너무 깁니다({len(summary)}자). 제목 기반 대체문을 사용했습니다.")
        if summary[-1] not in ".!?。！？":
            summary += "."
        return summary
    except Exception as exc:
        SUMMARY_ERRORS.append(str(exc))
        return fallback_summary(title)


def infer_press(url):
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    host = host.removeprefix("www.")
    mapping = {
        "yna.co.kr": "연합뉴스", "chosun.com": "조선일보", "hani.co.kr": "한겨레",
        "khan.co.kr": "경향신문", "donga.com": "동아일보", "joongang.co.kr": "중앙일보",
        "mk.co.kr": "매일경제", "hankyung.com": "한국경제", "edaily.co.kr": "이데일리",
        "mt.co.kr": "머니투데이", "fnnews.com": "파이낸셜뉴스", "newsis.com": "뉴시스",
    }
    for domain, name in mapping.items():
        if host.endswith(domain):
            return name
    return host or "기타언론"


def collect_company_news(keyword, start_dt, end_dt, client_id, client_secret, gemini_key=None):
    items = naver_news(keyword, client_id, client_secret)
    results, seen = [], set()
    for item in items:
        title = clean_html(item.get("title", ""))
        description = clean_html(item.get("description", ""))
        try:
            published = parse_pubdate(item["pubDate"])
        except Exception:
            continue
        if not (start_dt <= published <= end_dt):
            continue
        # 키워드가 제목/설명에 실제로 포함되는 기사만 남김
        if keyword.casefold() not in (title + " " + description).casefold():
            continue
        url = item.get("originallink") or item.get("link") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "published": published,
            "keyword": keyword,
            "summary": one_sentence_summary(title, description, gemini_key),
            "url": url,
            "title": title,
            "press": infer_press(url),
        })
    results.sort(key=lambda x: x["published"])
    return results


def find_header_row(ws):
    for row in range(1, min(ws.max_row, 30) + 1):
        vals = [str(ws.cell(row, col).value or "").strip() for col in range(1, min(ws.max_column, 10) + 1)]
        if "순번" in vals and "업무일자" in vals and "기사발행일" in vals and "요약" in vals and "링크" in vals:
            return row
    return None


def find_data_start(ws, header_row):
    # 회사 제공 서식의 '적합 예시/부적합 예시' 행은 유지
    for row in range(header_row + 1, min(ws.max_row, header_row + 15) + 1):
        marker = str(ws.cell(row, 1).value or "").strip()
        if marker in ("적합 예시", "부적합 예시"):
            continue
        if isinstance(ws.cell(row, 1).value, (int, float)):
            # 실제 데이터가 있으면 이 지점부터 덮어쓰기
            return row
    # 예시행 바로 다음 행부터 기록
    row = header_row + 1
    while row <= ws.max_row and str(ws.cell(row, 1).value or "").strip() in ("적합 예시", "부적합 예시"):
        row += 1
    return row


def write_results_to_template(template_bytes, results_by_company, run_date):
    wb = load_workbook(io.BytesIO(template_bytes))
    summary_counts = {}
    for company, keyword in COMPANIES.items():
        if company not in wb.sheetnames:
            raise ValueError(f"서식 파일에 '{company}' 탭이 없습니다.")
        ws = wb[company]
        header_row = find_header_row(ws)
        if not header_row:
            raise ValueError(f"'{company}' 탭에서 순번/업무일자/기사발행일/요약/링크 제목 행을 찾지 못했습니다.")
        data_start = find_data_start(ws, header_row)
        results = results_by_company.get(company, [])

        # 기존 실제 수집 데이터 영역을 비우되, 예시 행과 서식은 보존
        # 서식에 미리 준비된 행 수를 넘어가면 필요한 행을 추가
        clear_end = max(ws.max_row, data_start + len(results) + 5)
        for row in range(data_start, clear_end + 1):
            for col in range(1, 7):
                cell = ws.cell(row, col)
                cell.value = None
                cell.hyperlink = None

        for idx, item in enumerate(results, start=1):
            row = data_start + idx - 1
            # 열 순서: 순번, 업무일자, 기사발행일, 키워드, 요약, 링크
            values = [
                idx,
                run_date.strftime("%Y.%m.%d"),
                item["published"].strftime("%Y.%m.%d"),
                item["keyword"],
                item["summary"],
                item["url"],
            ]
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row, col, value)
                if col == 6:
                    cell.hyperlink = item["url"]
                    cell.style = ws.cell(data_start, 6).style
                    cell.font = Font(name=cell.font.name or "맑은 고딕", size=cell.font.sz or 9, color="0563C1", underline="single")
            # 요약은 줄바꿈이 생기지 않도록 셀 너비/행 높이는 원본 서식을 존중
        summary_counts[company] = len(results)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out.getvalue(), summary_counts


def configured_secret(name):
    try:
        return st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        return os.getenv(name, "")


st.set_page_config(page_title="회사 뉴스 클리핑", page_icon="📰", layout="wide")
st.title("📰 회사 뉴스 클리핑 자동화")
st.caption("버튼을 누르면 전날 13:00부터 실행 시각까지 6개 기업의 뉴스를 검색하고, 회사 제공 엑셀 서식에 정리합니다.")

now = datetime.now(KST)
default_start = datetime.combine((now - timedelta(days=1)).date(), dttime(13, 0), tzinfo=KST)

with st.sidebar:
    st.subheader("네이버 API 설정")
    client_id = st.text_input("네이버 Client ID", value=configured_secret("NAVER_CLIENT_ID"))
    client_secret = st.text_input("네이버 Client Secret", type="password", value=configured_secret("NAVER_CLIENT_SECRET"))
    st.caption("API 키를 코드에 직접 입력하지 마세요. 배포 시 Streamlit Secrets 또는 환경변수를 권장합니다.")
    st.subheader("요약 설정")
    gemini_key = st.text_input("Gemini API 키 (무료 등급 가능)", type="password", value=configured_secret("GEMINI_API_KEY"))
    st.caption("키가 있으면 Gemini가 제목·설명을 한 문장으로 요약합니다. 무료 사용 한도와 이용 가능 여부는 Google 계정에 따라 달라질 수 있습니다.")

st.markdown("### 1. 회사 제공 엑셀 서식")
uploaded_template = st.file_uploader("회사 서식 파일(.xlsx)을 선택하세요. 아래 기본 서식 파일이 있으면 자동 사용됩니다.", type=["xlsx"])

default_template_path = os.path.join(os.path.dirname(__file__), "회사_뉴스클리핑_서식.xlsx")
template_bytes = None
template_name = None
if uploaded_template is not None:
    template_bytes = uploaded_template.getvalue()
    template_name = uploaded_template.name
elif os.path.exists(default_template_path):
    with open(default_template_path, "rb") as f:
        template_bytes = f.read()
    template_name = os.path.basename(default_template_path)
    st.success(f"기본 서식 사용 중: {template_name}")
else:
    st.warning("회사 서식 파일을 업로드해 주세요.")

st.markdown("### 2. 이번 실행의 검색 기간")
col1, col2 = st.columns(2)
with col1:
    st.text_input("시작 시각 (한국 시간)", value=default_start.strftime("%Y-%m-%d %H:%M"), disabled=True)
with col2:
    st.text_input("종료 시각 (한국 시간)", value=now.strftime("%Y-%m-%d %H:%M:%S"), disabled=True)
st.caption("시작은 매일 전날 13:00, 종료는 스크랩 버튼을 누를 때의 시각입니다. 실행 중 시간이 바뀌어도 종료 시각은 실행 시작 시점으로 고정됩니다.")

st.markdown("### 3. 검색 대상")
st.dataframe(pd.DataFrame([{"엑셀 탭": name, "검색 키워드": keyword} for name, keyword in COMPANIES.items()]), use_container_width=True, hide_index=True)

if st.button("🚀 6개 기업 뉴스 스크랩 및 엑셀 생성", type="primary", use_container_width=True):
    if not client_id or not client_secret:
        st.error("네이버 Client ID와 Client Secret을 입력해 주세요.")
    elif not template_bytes:
        st.error("회사 제공 엑셀 서식 파일을 업로드하거나 앱 폴더에 넣어 주세요.")
    else:
        run_now = datetime.now(KST)
        start_dt = datetime.combine((run_now - timedelta(days=1)).date(), dttime(13, 0), tzinfo=KST)
        end_dt = run_now
        if end_dt <= start_dt:
            st.error("현재 시각이 시작 시각보다 빠릅니다. 날짜/시간 설정을 확인해 주세요.")
        else:
            SUMMARY_ERRORS.clear()
            all_results = {}
            errors = {}
            progress = st.progress(0)
            status = st.empty()
            for i, (company, keyword) in enumerate(COMPANIES.items(), start=1):
                status.write(f"검색 중 ({i}/6): **{company}**")
                try:
                    all_results[company] = collect_company_news(
                        keyword, start_dt, end_dt, client_id.strip(), client_secret.strip(), gemini_key.strip() or None
                    )
                except Exception as exc:
                    all_results[company] = []
                    errors[company] = str(exc)
                progress.progress(i / len(COMPANIES))
            try:
                excel_bytes, counts = write_results_to_template(template_bytes, all_results, run_now)
                st.success(f"엑셀 생성 완료 · 총 {sum(counts.values())}건")
                st.write(f"검색 기간: **{start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M:%S} (KST)**")
                st.dataframe(pd.DataFrame([{"기업": k, "수집 건수": v} for k, v in counts.items()]), use_container_width=True, hide_index=True)
                if errors:
                    st.warning("일부 기업 검색 중 오류가 발생했습니다. 해당 기업은 0건으로 기록되었습니다.")
                    for company, message in errors.items():
                        st.error(f"{company}: {message}")
                if SUMMARY_ERRORS:
                    unique_summary_errors = list(dict.fromkeys(SUMMARY_ERRORS))
                    st.warning(f"요약 과정에서 {len(SUMMARY_ERRORS)}건의 문제가 발생했습니다. 문제가 있는 기사는 제목 기반 대체문으로 저장했습니다.")
                    with st.expander("요약 오류 자세히 보기"):
                        for message in unique_summary_errors[:10]:
                            st.write(f"- {message}")
                filename = f"뉴스클리핑_{run_now:%Y%m%d_%H%M}.xlsx"
                st.download_button(
                    "📥 완성된 회사 보고서 엑셀 다운로드",
                    data=excel_bytes,
                    file_name=filename,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
                    use_container_width=True,
                )
            except Exception as exc:
                st.exception(exc)
