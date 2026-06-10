# test_eDM

GitHub에 eDM 폴더가 올라오면 폴더 안의 HTML을 기반으로 Outlook template(`.oft`)을 생성하는 CI/CD 구성이 포함되어 있습니다.

## 동작 방식

1. `*.html`, `*.htm`, 또는 이미지 파일이 push되면 `.github/workflows/build-oft.yml`이 실행됩니다.
2. 워크플로가 변경된 HTML 파일의 부모 폴더를 찾고, 이미지가 바뀐 경우에는 가장 가까운 HTML 포함 폴더를 찾습니다.
3. `scripts/build-oft.ps1`이 Windows Outlook COM을 사용해 같은 폴더에 `파일명.oft`를 생성합니다.
4. 생성 결과와 메타데이터는 `파일명_oft_build.json`에 기록됩니다.
5. `.oft`와 `*_oft_build.json`은 GitHub Actions artifact로 업로드되고, 기본값으로 현재 브랜치에도 커밋됩니다.

## 필수 runner 조건

진짜 OFT는 Outlook for Windows의 `SaveAs(..., olTemplate)` 결과여야 하므로 GitHub-hosted runner가 아니라 Windows self-hosted runner가 필요합니다.

- Windows PC 또는 VM
- Microsoft Outlook desktop 설치
- Outlook 기본 메일 프로필 설정 완료
- GitHub Actions self-hosted runner 설치
- runner label에 `outlook` 추가
- runner가 Outlook을 실행할 수 있는 로그인 사용자 세션에서 동작

워크플로는 다음 label을 가진 runner를 찾습니다.

```yaml
runs-on: [self-hosted, Windows, outlook]
```

## 폴더 업로드 규칙

폴더 안에 서버 발송용 HTML을 넣어 push하면 됩니다.

```text
campaign_001/
  campaign_001.html
  images/
    img_01.png
    cta_button.png
```

상대 이미지 경로(`images/img_01.png`)는 CI에서 현재 commit SHA 기준 GitHub raw URL로 변환한 뒤 Outlook에 입력합니다. 이미 `https://...` 절대 URL인 이미지는 그대로 둡니다.

같은 폴더에 `campaign_001.html`과 `campaign_001_local.html`이 같이 있으면 `_local.html`은 건너뛰고 서버용 HTML을 우선 처리합니다.

## 수동 실행

GitHub Actions에서 `Build Outlook templates` 워크플로를 수동 실행하고 `folder`에 처리할 폴더명을 입력할 수 있습니다.

Windows Outlook이 설치된 로컬 checkout에서도 직접 실행할 수 있습니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-oft.ps1 `
  -Folder campaign_001 `
  -Repository YunseoLsj/test_eDM `
  -RefName main `
  -RewriteRelativeImageSources
```

## 참고

- GitHub-hosted Windows runner의 [기본 설치 목록](https://github.com/actions/runner-images/blob/main/images/windows/Windows2022-Readme.md)에는 Outlook desktop이 포함되어 있지 않습니다.
- Outlook의 `MailItem.SaveAs`는 `olTemplate` 형식으로 `.oft` 저장을 지원하며, `olTemplate` 값은 Microsoft 문서상 `2`입니다. 참고: [MailItem.SaveAs](https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.saveas), [OlSaveAsType](https://learn.microsoft.com/en-us/office/vba/api/outlook.olsaveastype)
- Microsoft는 [Office 앱 자동화가 사용자 인터랙션을 전제로 설계되어 있다](https://learn.microsoft.com/en-us/office/client-developer/integration/considerations-unattended-automation-office-microsoft-365-for-unattended-rpa)고 안내합니다. 따라서 이 워크플로는 Outlook 프로필이 준비된 Windows self-hosted runner에서 검증하는 방식으로 구성되어 있습니다.
