import io
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

import feedparser
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

KST = ZoneInfo("Asia/Seoul")
MAX_ARTICLES = 10
MODEL = "gemini-3.5-flash-lite"

st.set_page_config(page_title="무료 뉴스 한 문장 요약", page_icon="📰", layout="wide")
st.title("📰 뉴스 검색 · 한 문장 요약")
st.caption("한국 시간 기준 어제 13:00부터 현재까지 · Google News RSS + Gemini 무료 등급")

def kst_now():
    return datetime.now(KST)

def parse_published(entry):
    # feedparser converts RSS date to a UTC time tuple when available.
    if getattr(entry, "published_parsed", None):
        import calendar
        ts = calendar.timegm(entry.published_parsed)
        return datetime.fromtimestamp(ts, KST)
    if getattr(entry, "updated_parsed", None):
        import calendar
        ts = calendar.timegm(entry.updated_parsed)
        return datetime.fromtimestamp(ts, KST)
    return None

def search_news(keyword, start, end, max_items=MAX_ARTICLES):
    # RSS does not require a news API key. Date filtering is done locally in KST.
    query = f'{keyword} after:{start.strftime("%Y-%m-%d")} before:{(end + timedelta(days=1)).strftime("%Y-%m-%d")}'
    url = "https://news.google.com/rss/search?q=" + quote(query) + "&hl=ko&gl=KR&ceid=KR:ko"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsClipper/1.0)"}
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    rows, seen = [], set()
    for entry in feed.entries:
        published = parse_published(entry)
        if not published or published < start or published > end:
            continue
        title = BeautifulSoup(entry.get("title", ""), "html.parser").get_text(" ", strip=True)
        link = entry.get("link", "")
        # Remove exact duplicate titles and URLs.
        key = re.sub(r"\W+", "", title).lower()
        if not title or key in seen or link in seen:
            continue
        seen.add(key)
        seen.add(link)
        source = ""
        if getattr(entry, "source", None):
            source = entry.source.get("title", "")
        rows.append({
            "기사 제목": title,
            "발행 시각": published.strftime("%Y-%m-%d %H:%M"),
            "언론사": source,
            "원문 링크": link,
            "요약": ""
        })
        if len(rows) >= max_items:
            break
    return rows

def get_article_text(url, title):
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        # A restrained extraction fallback; do not summarize navigation or unrelated text.
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) >= 35]
        text = "\n".join(paragraphs)
        if len(text) > 12000:
            text = text[:12000]
        return text or title
    except Exception:
        return title

def summarize_one(client, title, article_text):
    prompt = f"""
너는 한국어 뉴스 클리핑 편집자다. 아래 기사에 근거해서 핵심을 정확히 한 문장으로 요약하라.

엄격한 규칙:
- 한국어 문장 하나만 출력한다.
- 제목만 제공된 경우 제목에서 확인 가능한 사실만 요약하고 세부 사실을 추측하지 않는다.
- 기사에 없는 사실, 원인, 전망, 평가를 만들어내지 않는다.
- 핵심 주체와 행동/결과를 우선하고 군더더기를 뺀다.
- 목록, 머리말, 따옴표, '요약:' 같은 라벨은 출력하지 않는다.
- 최대 100자 안팎으로 간결하게 쓴다.
- 문장 끝은 마침표로 마무리한다.

기사 제목: {title}

기사 본문:
{article_text}
"""
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=100,
        ),
    )
    result = (response.text or "").strip()
    result = re.sub(r"^(요약\s*[:：]\s*)", "", result, flags=re.I)
    result = result.replace("\n", " ").strip().strip('"“”')
    # Keep only the first sentence as a defensive formatting guard.
    match = re.search(r"^(.+?[.!?。！？])(?:\s|$)", result)
    if match:
        result = match.group(1).strip()
    if not result:
        raise ValueError("AI가 빈 요약을 반환했습니다.")
    return result

with st.sidebar:
    st.header("검색 설정")
    keyword = st.text_input("뉴스 키워드", placeholder="예: 셀바이오휴먼텍")
    max_articles = st.slider("최대 기사 수", min_value=1, max_value=10, value=5)
    st.info("무료 API 한도를 보호하기 위해 한 번 검색할 때 최대 10건으로 제한합니다.")
    st.caption("API 키는 Streamlit Secrets에 저장해야 합니다.")

try:
    api_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    api_key = ""

if not api_key:
    st.warning("먼저 Streamlit 앱의 Settings → Secrets에 GEMINI_API_KEY를 등록하세요.")
    st.markdown("Google AI Studio에서 무료 API 키를 만들 수 있습니다: [Google AI Studio](https://aistudio.google.com/apikey)")
    st.code('GEMINI_API_KEY = "여기에_발급받은_키"', language="toml")
    st.stop()

st.markdown("**검색 기간**")
now = kst_now()
start = (now - timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
# If it is before 13:00 today, the previous day's 13:00 is still the start.
if now.hour < 13:
    start = (now - timedelta(days=2)).replace(hour=13, minute=0, second=0, microsecond=0)
st.write(f"{start:%Y-%m-%d %H:%M} ~ {now:%Y-%m-%d %H:%M} (한국 시간)")

if st.button("🔎 뉴스 검색 및 한 문장 요약", type="primary", disabled=not keyword.strip()):
    try:
        with st.spinner("뉴스 검색 중..."):
            articles = search_news(keyword.strip(), start, now, max_articles)
        if not articles:
            st.info("해당 기간에 검색된 기사가 없습니다. 키워드를 바꾸거나 잠시 후 다시 시도해 주세요.")
            st.stop()

        client = genai.Client(api_key=api_key)
        results = []
        progress = st.progress(0, text="요약 준비 중...")
        for i, article in enumerate(articles):
            try:
                with st.spinner(f"{i+1}/{len(articles)} 기사 요약 중..."):
                    body = get_article_text(article["원문 링크"], article["기사 제목"])
                    summary = summarize_one(client, article["기사 제목"], body)
                article["요약"] = summary
                article["상태"] = "완료"
            except Exception as e:
                message = str(e)
                if "429" in message or "RESOURCE_EXHAUSTED" in message or "quota" in message.lower():
                    article["요약"] = "무료 API 사용 한도에 도달했습니다. 나머지 기사는 요약하지 않았습니다."
                    article["상태"] = "한도 초과"
                    results.append(article)
                    break
                article["요약"] = "요약 실패: " + message[:180]
                article["상태"] = "실패"
            results.append(article)
            progress.progress(min((i + 1) / len(articles), 1.0), text=f"{i+1}/{len(articles)} 처리 완료")
            time.sleep(0.3)  # reduce burst requests against free-tier rate limits

        df = pd.DataFrame(results)
        st.session_state["news_results"] = df
        st.success(f"{len(df)}건의 검색 결과를 처리했습니다.")
    except requests.RequestException as e:
        st.error(f"뉴스 검색에 실패했습니다. 네트워크 또는 Google News RSS 상태를 확인해 주세요. ({e})")
    except Exception as e:
        st.error(f"오류가 발생했습니다: {e}")

if "news_results" in st.session_state:
    df = st.session_state["news_results"]
    st.subheader("검색 결과")
    for _, row in df.iterrows():
        with st.container(border=True):
            st.markdown(f"**{row['기사 제목']}**")
            st.caption(f"{row['발행 시각']} · {row['언론사'] or '언론사 정보 없음'}")
            st.write(row["요약"])
            st.markdown(f"[원문 기사 열기]({row['원문 링크']})")
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="뉴스 요약")
    st.download_button(
        "📥 엑셀 파일 다운로드",
        data=buffer.getvalue(),
        file_name=f"news_summary_{kst_now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
