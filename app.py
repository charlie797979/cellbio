import os
import re
import io
import time
import hashlib
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qsl, urlencode

import requests
import feedparser
import streamlit as st
from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.styles import Font
from openpyxl.worksheet.hyperlink import Hyperlink
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
KEYWORDS = ["셀바이오휴먼텍", "코스맥스", "한국콜마", "LG생활건강", "이미인", "코스메카코리아"]
TEMPLATE_NAME = "(주)셀바이오휴먼텍 뉴스클리핑 업무파일(10월_한철희).xlsx"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"

st.set_page_config(page_title="뉴스클리핑 업무 자동화", page_icon="📰", layout="wide")

def clean_url(url):
    """Remove common tracking parameters for duplicate detection; retain a usable article URL."""
    try:
        p = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(query), ""))
    except Exception:
        return url

def parse_date(entry):
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=KST)
                return dt.astimezone(KST)
            except Exception:
                pass
    return None

def fetch_google_news(keyword, start_dt, end_dt, max_items=40):
    query = f'"{keyword}" after:{start_dt.strftime("%Y-%m-%d")} before:{(end_dt + timedelta(days=1)).strftime("%Y-%m-%d")}'
    url = GOOGLE_NEWS_RSS.format(query=quote_plus(query))
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsClippingApp/1.0)"}
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    found = []
    for entry in feed.entries[:max_items]:
        published = parse_date(entry)
        if not published or published < start_dt or published > end_dt:
            continue
        title = BeautifulSoup(entry.get("title", ""), "html.parser").get_text(" ", strip=True)
        link = clean_url(entry.get("link", ""))
        source = ""
        if entry.get("source"):
            source = entry.source.get("title", "")
        summary_html = entry.get("summary", "")
        snippet = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)
        if " - " in title and not source:
            title, source = title.rsplit(" - ", 1)
        found.append({
            "keyword": keyword, "title": title.strip(), "url": link,
            "published": published, "source": source, "snippet": snippet
        })
    return found

def get_article_text(url):
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=18)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
            tag.decompose()
        selectors = [
            "article", "[itemprop='articleBody']", ".article-body", ".article_view",
            ".article-view-content-div", ".newsct_article", "#articleBody", ".article_txt",
            ".view_cont", ".news_view"
        ]
        candidates = []
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if len(text) > 250:
                    candidates.append(text)
        if not candidates:
            paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
            candidates = [" ".join(p for p in paragraphs if len(p) > 30)]
        text = max(candidates, key=len) if candidates else ""
        text = re.sub(r"\s+", " ", text).strip()
        return text[:12000]
    except Exception:
        return ""

def gemini_summarize(api_key, keyword, title, article_text):
    if not api_key:
        return "", "GEMINI_API_KEY가 설정되지 않았습니다."
    if len(article_text) < 250:
        return "", "기사 본문을 충분히 확보하지 못했습니다."
    prompt = f"""당신은 한국 기업 뉴스클리핑 담당자입니다.
아래 기사 본문을 읽고 지정 키워드와 직접 관련된 핵심만 한국어로 1~2문장으로 요약하세요.

규칙:
- 제목을 바꿔 쓰거나 제목만 반복하지 마세요.
- 기사 본문에 실제로 나온 사실만 사용하세요. 추측, 평가, 과장 금지.
- 기업의 사업, 제품, 생산, 실적, 투자, 계약, 공장, 인허가, 인사, 업계에 미치는 영향 중 핵심을 우선하세요.
- 키워드가 기사 주제와 실질적으로 관련 없으면 정확히 NOT_RELEVANT만 출력하세요.
- 요약은 1~2문장, 대략 80~180자 이내로 작성하세요.
- 머리말, 번호, 따옴표, '요약:' 같은 라벨은 붙이지 마세요.

키워드: {keyword}
기사 제목: {title}
기사 본문:
{article_text}
"""
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    try:
        r = requests.post(
            endpoint,
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.1, "maxOutputTokens": 220}},
            timeout=35
        )
        if r.status_code == 429:
            return "", "Gemini 무료 사용 한도에 도달했습니다. 잠시 후 다시 시도하세요."
        r.raise_for_status()
        data = r.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        result = " ".join(p.get("text", "") for p in parts).strip()
        result = re.sub(r"^\s*(요약\s*:\s*|[-•]\s*)", "", result)
        if "NOT_RELEVANT" in result:
            return "", "키워드와 직접 관련성이 낮은 기사"
        sentences = re.split(r"(?<=[.!?。])\s+|(?<=다)\s+(?=[가-힣])", result)
        result = " ".join(s.strip() for s in sentences if s.strip())[:500].strip()
        if not result:
            return "", "요약 결과가 비어 있습니다."
        return result, ""
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = r.text[:400]
        except Exception:
            pass
        return "", f"Gemini API 오류: {e} {detail}"
    except Exception as e:
        return "", f"요약 오류: {e}"

def article_id(item):
    normalized = clean_url(item["url"]).lower()
    if normalized:
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return hashlib.sha256(item["title"].lower().encode("utf-8")).hexdigest()

def load_template(uploaded):
    if uploaded is not None:
        return load_workbook(uploaded)
    if os.path.exists(TEMPLATE_NAME):
        return load_workbook(TEMPLATE_NAME)
    return None

def get_sheet_keywords(wb):
    result = {}
    for ws in wb.worksheets:
        if ws.title == "월말보고서":
            continue
        value = ws["A1"].value or ""
        keyword = str(value).split(":", 1)[-1].strip() if ":" in str(value) else ws.title
        result[ws.title] = keyword
    return result

def write_items(wb, sheet_keywords, records, work_date):
    # Do not alter sheet dimensions, merged cells, or workbook layout. Fill existing blank rows first.
    for sheet_name, keyword in sheet_keywords.items():
        ws = wb[sheet_name]
        rows = [r for r in range(3, ws.max_row + 1) if ws.cell(r, 1).value is not None]
        if not rows:
            rows = list(range(3, ws.max_row + 1))
        # Clear only data cells in the designated data area; keep row numbering and formatting intact.
        for r in range(3, ws.max_row + 1):
            if ws.cell(r, 1).value is not None and isinstance(ws.cell(r, 1).value, (int, float)):
                for c in range(2, 7):
                    ws.cell(r, c).value = None
                    ws.cell(r, c).hyperlink = None
        selected = [x for x in records if x.get("keyword") == keyword and x.get("summary")]
        target_rows = [r for r in range(3, ws.max_row + 1)
                       if isinstance(ws.cell(r, 1).value, (int, float))]
        if not target_rows:
            target_rows = list(range(3, ws.max_row + 1))
        for idx, item in enumerate(selected):
            if idx >= len(target_rows):
                break
            r = target_rows[idx]
            ws.cell(r, 1).value = idx + 1
            ws.cell(r, 2).value = work_date.strftime("%Y.%m.%d")
            ws.cell(r, 3).value = item["published"].strftime("%Y.%m.%d")
            ws.cell(r, 4).value = keyword
            ws.cell(r, 5).value = item["summary"]
            link_cell = ws.cell(r, 6)
            link_cell.value = item["title"]
            link_cell.hyperlink = item["url"]
            link_cell.style = ws.cell(3, 6).style
            # Preserve the template's font family/size while enforcing non-bold text.
            old = link_cell.font
            link_cell.font = Font(name=old.name or "맑은 고딕", sz=old.sz or 10,
                                  bold=False, italic=old.italic, color="0000FF",
                                  underline="single")
    return wb

st.title("📰 뉴스클리핑 업무 자동화")
st.caption("전일 13:00부터 실행 시점까지의 기사 검색 · 본문 기반 요약 · 기존 엑셀 양식 유지")

with st.sidebar:
    st.subheader("실행 설정")
    api_key = st.secrets.get("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", ""))
    if api_key:
        st.success("Gemini API 키가 설정되어 있습니다.")
    else:
        st.warning("GEMINI_API_KEY가 설정되지 않았습니다. 요약 기능을 사용하려면 Streamlit Secrets에 무료 API 키를 등록하세요.")
    uploaded_template = st.file_uploader("업무 엑셀 파일 업로드", type=["xlsx"])
    work_date = st.date_input("업무일자", value=datetime.now(KST).date())
    run_button = st.button("뉴스 검색 및 엑셀 작성", type="primary", use_container_width=True)

st.markdown("**모니터링 키워드**")
st.write(" · ".join(KEYWORDS))
st.info("기사 발행 시각이 RSS에 제공되지 않거나 본문을 확보하지 못한 기사는 자동 입력하지 않고 검토 대상으로 남깁니다. 링크와 요약은 다운로드 전 표에서 확인하세요.")

if run_button:
    now = datetime.now(KST)
    start_dt = (now - timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
    end_dt = now
    wb = load_template(uploaded_template)
    if wb is None:
        st.error(f"엑셀 템플릿을 찾을 수 없습니다. {TEMPLATE_NAME} 파일을 업로드하거나 app.py와 같은 GitHub 저장소에 추가하세요.")
        st.stop()
    sheet_keywords = get_sheet_keywords(wb)
    all_items = []
    errors = []
    progress = st.progress(0, text="뉴스 검색 준비 중...")
    for i, keyword in enumerate(sheet_keywords.values()):
        try:
            found = fetch_google_news(keyword, start_dt, end_dt)
            all_items.extend(found)
        except Exception as e:
            errors.append(f"{keyword}: 검색 실패 ({e})")
        progress.progress((i + 1) / max(1, len(sheet_keywords)), text=f"뉴스 검색 중: {keyword}")
        time.sleep(0.3)

    # Deduplicate by URL, then by normalized title. Keep the first item found.
    deduped = []
    seen = set()
    for item in sorted(all_items, key=lambda x: x["published"], reverse=True):
        key = article_id(item)
        title_key = re.sub(r"[^0-9a-z가-힣]", "", item["title"].lower())
        if key in seen or (title_key and title_key in seen):
            continue
        seen.add(key)
        if title_key:
            seen.add(title_key)
        deduped.append(item)

    st.subheader("검색 결과 요약")
    st.write(f"검색 시간: **{start_dt.strftime('%Y.%m.%d %H:%M')} ~ {end_dt.strftime('%Y.%m.%d %H:%M')}**")
    st.write(f"검색 결과 {len(all_items)}건 · 중복 제거 후 {len(deduped)}건")
    if errors:
        for err in errors:
            st.warning(err)

    results = []
    review = []
    p2 = st.progress(0, text="기사 본문 확인 및 요약 중...")
    for i, item in enumerate(deduped):
        text_body = get_article_text(item["url"])
        summary, reason = gemini_summarize(api_key, item["keyword"], item["title"], text_body)
        item["summary"] = summary
        item["review_reason"] = reason
        item["body_found"] = bool(text_body)
        if summary:
            results.append(item)
        else:
            review.append(item)
        p2.progress((i + 1) / max(1, len(deduped)), text=f"기사 처리 중: {i+1}/{len(deduped)}")
        time.sleep(0.15)

    st.success(f"엑셀 입력 대상 {len(results)}건 · 검토 필요 {len(review)}건")
    if results:
        st.dataframe([{
            "키워드": x["keyword"],
            "기사발행일": x["published"].strftime("%Y.%m.%d"),
            "기사 제목": x["title"],
            "요약": x["summary"],
            "링크": x["url"]
        } for x in results], use_container_width=True, hide_index=True)
    if review:
        with st.expander(f"자동 입력하지 않은 기사 {len(review)}건 확인"):
            st.dataframe([{
                "키워드": x["keyword"],
                "기사발행일": x["published"].strftime("%Y.%m.%d"),
                "기사 제목": x["title"],
                "제외/검토 사유": x["review_reason"],
                "링크": x["url"]
            } for x in review], use_container_width=True, hide_index=True)

    wb = write_items(wb, sheet_keywords, results, work_date)
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"㈜셀바이오휴먼텍 뉴스클리핑 업무파일({work_date.strftime('%m월_%d일')}).xlsx"
    st.download_button(
        "완성된 뉴스클리핑 엑셀 다운로드",
        data=output.getvalue(),
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary"
    )
    st.caption("다운로드 후 각 시트의 기사 제목 링크를 직접 클릭해 연결 여부를 최종 확인하세요.")
