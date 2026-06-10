아래 내용을 기존 eDM 자동화 프롬프트에 그대로 추가한다.

---

추가 필수 검증/예외 처리:

1. Drive 원본 다운로드 검증
- Google Drive 플러그인 raw fetch 결과나 임시 URL을 로컬에 저장한 뒤 반드시 `file`, 파일 크기, 이미지 치수를 확인한다.
- 저장된 파일이 PNG/JPG/WebP가 아니라 XML/HTML 오류 응답이면 원본 다운로드 실패로 처리한다.
- 실패한 오류 응답 파일을 `source.png` 등 원본 이미지명으로 사용하지 않는다.
- 기존 캐시나 Git 히스토리의 파일을 대체 원본으로 사용할 때는 같은 Drive file ID, 파일명, 이미지 치수, SHA-256 해시 근거를 로그에 남긴다.

2. EML 생성 규칙
- EML은 문자열을 `newline="\r\n"`로 다시 변환하는 방식으로 쓰지 않는다. `\r\r\n`가 생겨 Outlook/메일 앱에서 열리지 않을 수 있다.
- Python 표준 `email.message.EmailMessage`와 `policy=policy.SMTP`를 사용해 `as_bytes()` 결과를 그대로 저장한다.
- HTML 본문은 `msg.set_content(html, subtype="html", charset="utf-8", cte="quoted-printable")` 방식으로 넣는다.
- Outlook에서 초안처럼 열리도록 `X-Unsent: 1` 헤더를 넣는다.
- 생성 후 반드시 아래를 검수한다.
  - `CRCRLF` 개수 0
  - MIME 파서 defects 없음
  - `Content-Type: text/html`
  - `Content-Transfer-Encoding: quoted-printable`
  - `X-Unsent: 1`

3. OFT 생성 규칙
- 자체 제작한 최소 CFB/MAPI 파일을 성공 OFT로 간주하지 않는다.
- `file` 명령에서 `CDFV2 Microsoft Outlook Message`로 보여도 Outlook에서 실제로 열리지 않으면 실패다.
- Mac Outlook AppleScript의 `save ... as "oft"`는 실제 OFT가 아니라 HTML 문서를 `.oft` 확장자로 저장할 수 있으므로 성공으로 간주하지 않는다.
- OFT는 Outlook for Windows에서 실제로 열고 `Save As > Outlook Template (*.oft)`로 저장해 검증한 경우에만 산출물로 포함한다.
- Mac 환경에서 검증 가능한 진짜 OFT를 만들 수 없으면 `.oft`를 업로드하지 말고 `*_oft_unavailable.txt`를 생성해 실패 사유와 Windows 생성 절차를 기록한다.
- 깨진 `.oft` 파일을 최종 산출물 또는 Drive 업로드 대상에 포함하지 않는다.

4. Drive 업로드 규칙
- Google Drive 플러그인에 raw 파일 업로드/폴더 생성 도구가 없으면 Google Drive Desktop 동기화 폴더를 우선 확인한다.
- 예: `~/Library/CloudStorage/GoogleDrive-*/내 드라이브/eDM_test`
- Google Drive Desktop 폴더에 결과 폴더를 만들 때도 기존 폴더 존재 여부를 확인한다.
- 기존 결과 폴더가 있으면 덮어쓰기 전에 `폴더명_backup_YYYYMMDD_HHMMSS`로 백업하거나 새 버전 폴더를 만든다.
- Drive 업로드에는 검증된 파일만 포함한다.
  - `이미지명.html`
  - `이미지명.eml`
  - `source.확장자`
  - `images.zip`
  - `이미지명_local.html`
  - `processing.log`
  - `summary.json`
  - OFT 실패 시 `이미지명_oft_unavailable.txt`
- 깨진 `.oft`, Outlook이 HTML로 저장한 가짜 `.oft`, 임시 테스트 파일은 업로드하지 않는다.
- 복사 후 Drive Desktop 동기화 경로에 실제 파일이 존재하는지 파일 크기와 목록을 확인한다.

5. Computer Use / GUI 업로드 규칙
- 사용자가 Drive 업로드를 명시적으로 요청하면 Computer Use로 Finder/Drive UI를 확인해도 된다.
- 파일 업로드는 외부 전송이므로 사용자 요청에 Drive 목적지와 업로드 파일이 명확히 포함되어 있어야 한다.
- GUI 업로드가 필요하면 업로드 직전 파일 목록을 확정하고, 최종 전송 후 Drive 폴더에서 파일이 보이는지 확인한다.

6. HTML/이미지 검수 규칙
- 서버 전달용 HTML은 모든 이미지가 실제 접근 가능한 `https` 절대 URL이어야 한다.
- 네트워크/DNS/Browser 정책 때문에 URL 접근성 검수가 불가능하면 성공으로 쓰지 말고 `not_verified`로 기록한다.
- Browser가 `file://`를 차단하거나 로컬 서버 포트 바인딩이 실패하면 그 사실을 로그에 남기고 HTML 정적 검수와 픽셀 재구성 검수로 대체한다.
- 이미지맵, `<map>`, `<area>`, `<script>`, `<div>`, `position:absolute`, `background-image`가 있으면 실패 처리한다.
- 버튼 조각 하나만 `<a href="">`로 감싸고, 나머지 이미지 조각에는 링크가 없어야 한다.

7. Git/URL 검수 규칙
- 이미지 slice를 Git에 추가하고 push한 뒤에만 GitHub raw/GitHub Pages URL을 최종 서버 URL로 확정한다.
- `git push`가 DNS/네트워크 문제로 실패하면 원격 반영 완료로 기록하지 않는다.
- raw 이미지 URL 접근성을 실제로 확인하지 못했으면 `image_url_accessibility_check=not_verified`로 남긴다.

8. 최종 완료 조건
- EML이 열리지 않거나 OFT가 열리지 않는다는 사용자 피드백이 있으면 먼저 파일 포맷을 재검증하고 산출물을 수정한다.
- 열리지 않는 파일을 Drive에 업로드하지 않는다.
- OFT 생성이 불가능하면 HTML/EML은 완료 처리하되 OFT는 실패 사유를 명확히 기록한다.

---
