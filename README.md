# SWING Portfolio 자동 업데이트 (GitHub Actions + Notion API)

매매일지를 보고 보유주식 → 총자산을 자동으로 갱신하고, 총자산 추이 차트를
GitHub에 직접 저장해서 노션에 항상 안정적으로 보여주는 자동화입니다.
(Colab + Imgur 대신 GitHub Actions + GitHub 이미지 호스팅을 사용합니다.)

## 0. 미리 만들어 둔 것 (이미 완료)
- 노션 페이지: SWING Portfolio
- 데이터베이스 3개: 총자산 / 보유주식 / 매매일지 (각 데이터소스 ID는 스크립트에 이미 입력되어 있습니다)

## 1. GitHub 저장소 만들기 (사람이 직접, 2분)
1. https://github.com/new 접속 (로그인 필요)
2. Repository name: `swing-portfolio` (원하는 이름으로 변경 가능)
3. Public 또는 Private 선택 (Private 추천 — 어차피 토큰은 Secrets로 따로 보관합니다)
4. "Create repository" 클릭

## 2. 이 폴더의 파일 올리기
이 압축 파일 안의 구조를 그대로 저장소에 올리면 됩니다.

```
swing-portfolio/
├─ scripts/update_portfolio.py
├─ requirements.txt
└─ .github/workflows/update.yml
```

GitHub 웹 화면에서 "Add file → Upload files"로 폴더/파일을 그대로 드래그해서 올려도 되고,
익숙하면 `git clone` 후 파일 복사 → `git push`로 올려도 됩니다.

## 3. Notion 연동 토큰 만들기
1. https://www.notion.so/profile/integrations 접속
2. "New integration" → 이름 입력(예: swing-portfolio-bot) → 워크스페이스 선택 → 생성
3. 생성된 **Internal Integration Token** 복사 (`secret_...`로 시작)
4. 노션의 **총자산 / 보유주식 / 매매일지** 데이터베이스 3개 각각에서
   우측 상단 `···` → `Connections`(연결) → 방금 만든 integration을 추가
   (이 단계를 빠뜨리면 API가 403/404 에러를 냅니다)

## 4. GitHub Secrets에 토큰 등록
1. 저장소 → Settings → Secrets and variables → Actions
2. "New repository secret" → Name: `NOTION_TOKEN` → Value: 3번에서 복사한 토큰 → Add secret

## 5. 테스트 실행
1. 저장소 → Actions 탭 → "Update SWING Portfolio" 워크플로 선택
2. "Run workflow" 버튼 클릭 (수동 실행)
3. 실행 로그에서 에러 없이 끝나는지 확인 → 노션의 보유주식/총자산이 갱신됐는지 확인

## 6. 자동 실행 주기
`update.yml`의 cron이 평일 한국시간 16:00(KRX 마감 후)에 자동 실행되도록 되어 있습니다.
시간을 바꾸고 싶으면 `.github/workflows/update.yml`의 `cron` 값을 수정하세요.

## 7. 매매일지를 새로 등록했을 때
지금 구조에서는 노션에 새 매매일지를 적었다고 자동으로 즉시 실행되지는 않습니다
(노션 → GitHub로 실시간 신호를 보내려면 노션의 유료 Automations/웹훅 기능이 추가로 필요합니다).
대신 매매일지를 등록한 직후 GitHub의 Actions 탭에서 "Run workflow"를 한 번 눌러주면
보유주식 → 총자산 → 차트까지 그 즉시 전체가 갱신됩니다.

## 8. 차트를 노션에 표시하기
1. 처음 한 번 워크플로를 실행해서 저장소에 `charts/total_assets.png`가 생성되면
2. 다음 주소를 복사합니다 (raw 이미지 주소, 본인 계정/저장소명으로 바꿔서):
   `https://raw.githubusercontent.com/내깃허브계정/swing-portfolio/main/charts/total_assets.png`
3. 노션 SWING Portfolio 페이지에 이미지 블록을 추가하고 위 주소를 붙여넣습니다.
   파일 경로가 같으면 주소가 바뀌지 않으므로, 이후에는 이미지 블록을 다시 손댈 필요 없이
   실행할 때마다 그림 내용만 갱신됩니다 (Imgur보다 안정적인 이유).

## 참고: 계산 방식 메모
- 평균매입단가는 이동평균법(매수마다 누적 평균을 다시 계산)으로 처리합니다.
- 현재가는 FinanceDataReader로 KRX 종가를 가져옵니다. 시세 조회가 실패하면
  평균매입가를 임시로 사용하니, 그 경우 수익/수익률은 0으로 표시됩니다.
- 전량 매도해서 보유수량이 0이 되어도 보유주식 행은 삭제하지 않고 0으로 갱신만 합니다.
- 총자산은 실행할 때마다 새 행을 "추가"하는 방식이라 과거 기록이 계속 쌓입니다(삭제 없음).
