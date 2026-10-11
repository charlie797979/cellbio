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
MODEL = "gemini-3.8-flash"

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
    """원문 본문을 최대한 추출한다. 제목만 확보된 경우 빈 문자열을 반환한다."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=18, allow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        descriptions = []
        for selector in (
            'meta[property="og:description"]',
            'meta[name="description"]',
            'meta[name="twitter:description"]',
        ):
            tag = soup.select_one(selector)
            if tag and tag.get("content"):
                descriptions.append(tag["content"].strip())

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
            tag.decompose()

        containers = soup.select("article, main, [itemprop='articleBody']")
        paragraphs = []
        for container in containers[:4]:
            paragraphs.extend(p.get_text(" ", strip=True) for p in container.find_all("p"))

        if not paragraphs:
            paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]

        paragraphs = [re.sub(r"\\s+", " ", p).strip() for p in paragraphs]
        paragraphs = [p for p in paragraphs if len(p) >= 25]
        body = "\\n".join(dict.fromkeys(paragraphs))

        if len(body) < 350 and descriptions:
            body = "\\n".join(descriptions + ([body] if body else []))

        body = body[:18000]
        return body if len(body.strip()) >= 180 else ""
    except Exception:
        return ""

def summarize_one(client, title, article_text):
    if not article_text or len(article_text.strip()) < 180:
        raise ValueError("기사 본문을 충분히 확보하지 못했습니다.")

    prompt = f"""
너는 한국어 경제·산업 뉴스 전문 편집자다. 아래 기사 본문을 읽고 핵심 내용을 정확히 두 문장의 서술형 요약으로 작성하라.

작성 규칙:
- 반드시 정확히 두 문장으로 작성한다.
- 제목을 단순히 바꿔 쓰지 말고 본문의 구체적인 사실을 담는다.
- 첫 문장에는 핵심 사건·발표·결정과 주요 주체를 설명한다.
- 두 번째 문장에는 본문에 나온 구체적인 수치, 세부 내용, 배경, 결과 또는 향후 계획 중 중요한 정보를 담는다.
- 기사에 실제로 적힌 정보만 사용하고, 수치·원인·전망을 만들어내지 않는다.
- 같은 내용을 반복하지 않는다.
- 한국어 서술형으로 자연스럽게 작성한다.
- 머리말, 목록, 번호, '요약:'은 쓰지 않는다.
- 문장을 두 줄로 나눠 출력한다.
- 정보가 부족하면 추측으로 채우지 않는다.

기사 제목:
{title}

기사 본문:
{article_text}
"""
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.15,
            max_output_tokens=220,
        ),
    )
    result = (response.text or "").strip().replace("\\r", " ")
    result = re.sub(r"^요약\\s*[:：]\\s*", "", result, flags=re.I)
    result = result.strip().strip('"“”')
    # 모델이 줄바꿈만으로 두 문장을 구분한 경우도 처리한다.
    result = re.sub(r"\\s*\\n+\\s*", " ", result)
    sentences = re.findall(r"[^.!?。！？]+[.!?。！？]?", result)
    sentences = [sentence.strip() for sentence in sentences if sentence.strip()]
    if len(sentences) < 2:
        raise ValueError("AI가 두 문장 요약을 반환하지 않았습니다.")
    sentences = sentences[:2]
    return " ".join(
        sentence if re.search(r"[.!?。！？]$", sentence) else sentence + "."
        for sentence in sentences
    )

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
                if "본문을 충분히 확보" in message:
                    article["요약"] = "원문 본문을 가져오지 못해 요약을 생략했습니다. 원문 링크를 확인해 주세요."
                    article["상태"] = "본문 확보 실패"
                else:
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
