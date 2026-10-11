# 뉴스클리핑 모니터링 앱 (Streamlit)

네이버 뉴스 검색 API로 키워드별 기사를 모아(전일 13시~현재, 중복 제거) 검토·수정 후
업무파일(xlsx)의 해당 키워드 시트에 이어서 기록합니다. 기존 서식은 유지됩니다.

## 1. 네이버 API 키 발급 (무료)
1. https://developers.naver.com → Application 등록 → 사용 API: **검색**
2. Client ID / Client Secret 확인

## 2. GitHub에 올리기
이 폴더 전체를 새 저장소에 push (`.streamlit/secrets.toml` 은 올리지 않습니다 — .gitignore 처리됨).
> 회사 업무파일 양식이 들어 있으므로 저장소는 **Private** 권장.

## 3. Streamlit Community Cloud 배포
1. https://share.streamlit.io → New app → 저장소 선택 → Main file: `app.py`
2. Advanced settings → **Secrets** 에 `.streamlit/secrets.toml.example` 내용을 값 채워 붙여넣기

## 사용 순서
1. (선택) 어제 작업한 월 업무파일 업로드 → 이어서 기록 / 없으면 기본 양식
2. 키워드 선택 → **기사 검색**
3. 표에서 기사 선택·요약 수정 → **기록** → 다운로드
4. 하이퍼링크 클릭 확인 후 제출
