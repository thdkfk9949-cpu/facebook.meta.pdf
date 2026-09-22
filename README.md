# metaaudit

메타(페이스북) 광고 계정 **읽기 전용** 감사 도구.

구조적으로 낭비되는 지점을 찾아내고, **표본이 뒷받침하지 못하는 주장은 하지 않습니다.**
계정에 아무것도 쓰지 않습니다 — GET 요청만 보냅니다.

---

## 이 도구가 하는 일과 하지 않는 일

### 찾아내는 것

가장 큰 낭비는 보통 "성과가 나쁜 광고"가 아니라 **산술적으로 성공이 불가능한 구조**입니다.

광고세트가 학습 단계를 벗어나려면 7일에 약 50건의 최적화 이벤트가 필요합니다.
이 조건은 그대로 예산 하한선이 됩니다:

```
필요 일예산 = CPA × 50 ÷ 7
```

CPA가 50,000원이면 광고세트 하나당 하루 357,000원이 필요합니다. 일예산 100,000원짜리
광고세트는 "성과가 부진한" 게 아니라 **애초에 안정화될 수 없습니다.** 크리에이티브를 바꾸든
타겟을 바꾸든 달라지지 않습니다. 해법은 예산을 더 적은 광고세트로 합치는 것이고, 이건 **돈이 들지
않습니다.**

### 하지 못하는 것

- **입찰에 개입하지 않습니다.** 메타 경매는 밀리초 단위이고 입찰 신호는 메타만 가집니다.
- **광고 단가를 깎지 않습니다.** CPM은 경쟁·오디언스 규모·품질순위가 정합니다.
- **오디언스 중복률을 측정하지 않습니다.** 마케팅 API가 그 수치를 제공하지 않습니다.
  대신 *구조적* 중복(동일 타겟팅, 상호 제외 없는 동일 인구통계 기반)을 잡아내고,
  실제 비율은 Ads Manager의 Audience Overlap 도구에서 확인하라고 안내합니다.
- **예산이 작으면 도움이 제한적입니다.** 전환이 주당 수십 건 미만이면 어떤 최적화든
  노이즈를 추적하는 것에 불과합니다. 이 도구는 그 사실 자체를 알려줍니다.

---

## 설치

```bash
git clone <this-repo> && cd facebook.meta.pdf
python3 -m pip install -r requirements.txt   # requests 하나뿐입니다
```

Python 3.10 이상.

## 인증 설정

`ads_read` 권한이 있는 액세스 토큰과 광고 계정 ID가 필요합니다. 감사는 읽기 전용이므로
`ads_management`는 **필요 없습니다.**

```bash
cp .env.example .env
# .env 를 편집해서 토큰과 계정 ID 입력
```

`.env`는 `.gitignore`에 들어 있습니다. 토큰은 **절대 커밋하지 마세요.**

> **토큰 종류 주의**: Graph API Explorer에서 복사한 토큰은 약 1시간 후 만료됩니다.
> 지속적으로 쓰려면 비즈니스 관리자에서 **시스템 사용자(System User)** 토큰을 발급하세요.
> 앱에 "앱 시크릿 필요" 설정이 켜져 있으면 `META_APP_SECRET`도 채워야 합니다.

## 실행

### 0단계: 설정 확인 (먼저 이것부터)

API 호출 3번만 쓰는 점검입니다. 토큰과 권한이 제대로 붙었는지 바로 알려줍니다.
쓰기 권한(`ads_management`)이 있는지도 같이 알려주므로, `--apply`가 정작 필요한
순간에 권한 오류로 실패하는 일을 막습니다.

```bash
python3 -m metaaudit --env-file .env --check-auth
```

성공하면 이렇게 나옵니다:

```
OK  token is valid and can read this ad account.

  Name          : 우리쇼핑몰
  Currency      : KRW  (minor unit divisor 1)
  Timezone      : Asia/Seoul
  Status        : ACTIVE
  Lifetime spend: 48,250,000 KRW

OK  campaigns edge is readable.

  Token scopes  : ads_read
  Writes        : NOT allowed — ads_management is not granted. --plan works; --apply will fail.
```

실패하면 원인과 고칠 지점을 같이 알려줍니다. 계정 노드는 읽히는데 캠페인 목록은 막히는
경우도 잡아냅니다 — 권한 설정에서 흔한 실수입니다.

### 1단계: 감사 실행

```bash
# 기본: 최근 30일, 터미널 출력
python3 -m metaaudit --env-file .env

# 마크다운 리포트 파일로
python3 -m metaaudit --env-file .env --format markdown --out out/audit.md

# 최근 14일만, 특정 계정
python3 -m metaaudit --env-file .env --account act_123456 --window 14

# 항목 목록 보기 / 특정 항목만 실행
python3 -m metaaudit --list-checks
python3 -m metaaudit --env-file .env --only learning.underbudgeted --only structure.fragmentation
```

### 윈도우에서 두 번 클릭으로 돌리기

`.env`를 채워뒀다면 `run-audit.bat`을 더블클릭하면 끝입니다. 인증을 먼저 확인하고,
날짜별 스냅샷과 마크다운 리포트를 만든 뒤 리포트를 열어줍니다.

### API 호출을 아끼는 법

스냅샷을 저장해두면 감사 로직을 몇 번을 다시 돌려도 API를 전혀 쓰지 않습니다.

```bash
python3 -m metaaudit --env-file .env --save-snapshot snapshots/today.json
python3 -m metaaudit --from-snapshot snapshots/today.json --format json
```

스냅샷에는 계정 실적 데이터가 들어 있으므로 `snapshots/`도 기본 `.gitignore` 대상입니다.

## 계정 변경 — 계획 후 적용

감사는 읽기 전용이지만, 찾아낸 것을 **직접 고치게** 할 수도 있습니다. 다만 두 단계로
나뉘어 있고, 사이에 사람이 들어갑니다.

```bash
# 1단계: 변경안 생성 (계정에 아무것도 쓰지 않습니다)
python3 -m metaaudit --env-file .env --plan plans/today.json

# 2단계: 읽어보고, 승인하고, 적용
python3 -m metaaudit --env-file .env --apply plans/today.json
```

### 무엇이 자동 적용 대상인가

**`confidence=structural` 항목만입니다.** 설정의 산술에서 바로 따라나오는 것, 즉 표본
크기와 무관하게 참인 것만 변경안에 들어갑니다. 이 계정의 성과 수치에 기대는 판단은
아무리 명백해 보여도 자동으로 적용하지 않습니다. 이 도구의 논지가 "표본이 뒷받침하지
못하는 주장은 하지 않는다"인데, 그걸 자동화로 어기면 앞뒤가 안 맞습니다.

쓸 수 있는 필드는 `daily_budget`과 `status` **둘뿐**입니다. 이 제한은 두 곳에 걸려
있습니다 — 계획 파일을 읽을 때, 그리고 API 클라이언트가 쓰기를 보낼 때. 계획 파일을
손으로 고쳐도 다른 필드는 나가지 않습니다.

### 돈이 드는 변경은 따로 허락받습니다

광고세트를 합치는 건 **공짜**입니다. 예산 총액이 그대로고 배분만 바뀝니다. 그래서
기본으로 제안합니다.

예산 인상은 다릅니다. 매일 실제로 더 나갑니다. 그래서 **금액을 명시해야만** 변경안에
들어갑니다:

```bash
python3 -m metaaudit --env-file .env --plan plans/today.json --allow-budget-increase 50000
```

허용액을 넘는 인상은 "직접 판단하세요" 목록으로 빠지고, 필요한 금액을 알려줍니다.

### 안전장치

- **쓰기 전에 읽습니다.** 각 변경은 "이 값이었을 것"을 들고 다닙니다. 쓰기 직전에 현재
  값을 확인해서 다르면 **건너뛰고 보고합니다.** 계획을 세운 뒤 계정이 바뀌었다면 그
  계획은 그 객체에 대해 더 이상 유효하지 않습니다.
- **되돌리기 파일이 먼저 저장됩니다.** 첫 API 호출 **전에** 디스크에 씁니다. 중간에
  끊겨도 되돌릴 기록이 남습니다. 그대로 `--apply` 하면 원상복구됩니다.
- **확인을 받습니다.** `apply`를 직접 타이핑해야 진행합니다 (`--yes`로 생략 가능).
- **`--dry-run`** 은 현재 값을 전부 확인만 하고 아무것도 쓰지 않습니다.
- 실패는 변경 단위로 보고됩니다. 하나가 실패해도 나머지는 진행되고, 요약에 전부 나옵니다.

### 자동으로 하지 않는 것

최적화 목표 변경, 픽셀·전환 이벤트 지정처럼 **이 도구가 대신 고를 수 없는 결정**은
변경안에 넣지 않고 "직접 판단하세요" 목록에 이유와 함께 표시합니다. 조용히 빠뜨리지
않습니다 — 건너뛴 건 고쳐진 게 아닙니다.

### CI에서 쓰기

```bash
python3 -m metaaudit --env-file .env --fail-on critical   # CRITICAL 있으면 exit 1
```

---

## 감사 항목 13가지

| ID | 잡아내는 것 |
|---|---|
| `learning.underbudgeted` | 예산이 CPA 대비 낮아 주 50이벤트에 산술적으로 도달 불가능한 광고세트 |
| `learning.stuck` | 메타가 직접 LEARNING / LEARNING_LIMITED로 표시한 광고세트 |
| `structure.fragmentation` | 예산이 감당할 수 있는 것보다 많은 광고세트로 쪼개진 캠페인 |
| `structure.self_competition` | 같은 캠페인 안에서 서로 경쟁하는 광고세트 (2단계 판정) |
| `delivery.fatigue` | 빈도 상승 + **유의미한** CTR 하락 |
| `creative.supply` | 소재가 너무 적거나 사실상 중복인 광고세트 |
| `tracking.goal_mismatch` | 전환 캠페인인데 클릭을 사는 광고세트 |
| `tracking.missing_pixel` | 전환 최적화인데 이벤트 타겟이 없는 광고세트 |
| `tracking.silent` | 지출은 있는데 전환이 0으로 기록되는 구간 |
| `tracking.attribution_mix` | 한 캠페인 안에 기여 기간이 섞여 비교가 불가능한 상태 |
| `tracking.utm` | UTM 없어 외부 분석으로 검증 불가능한 광고 |
| `efficiency.outliers` | 통계적으로 유의하게 캠페인 평균보다 나쁜 광고세트 |
| `efficiency.undecidable` | **판단하기엔 표본이 부족한** 광고세트 (CPA 신뢰구간 제시) |

---

## 리포트 읽는 법

각 발견 항목에는 `confidence` 등급이 붙습니다. 이게 핵심입니다:

- **`structural`** — 계정 설정의 산술에서 바로 따라나오는 사실. 표본 크기와 무관하게 참입니다.
  바로 조치해도 됩니다.
- **`measured`** — 이 계정의 실제 수치로 유의성 검정을 통과한 것. 신뢰해도 됩니다.
- **`heuristic`** — 보통 문제인 패턴. **조치 전에 직접 확인하세요.**

### 조치 순서

1. **`tracking.*` 먼저.** 측정이 틀렸으면 나머지 모든 숫자가 거짓입니다.
2. **다음 `learning.*` 과 `structure.*`.** 주 50이벤트에 못 미치는 광고세트는
   실제 성과와 무관하게 나빠 보입니다. 이걸 고치기 전에 뭔가를 끄면 안 됩니다.
3. **`efficiency.*` 는 마지막.** 그리고 `efficiency.undecidable`에 걸린 광고세트는
   아직 끄지 마세요 — 동전 던지기입니다.

### `efficiency.undecidable`이 가장 자주 돈을 아껴줍니다

전환 10건으로 계산한 CPA는 95% 신뢰구간이 **±62%**입니다. 이 도구는 그 구간을 직접
보여주고, 구간 안에서 뒤집힐 판단은 하지 말라고 말합니다. 광고비를 태우는 가장 흔한 원인은
최적화 부족이 아니라 **표본이 모이기 전에 끄고 켜는 것**입니다.

---

## 임계값 조정

기본값은 법이 아닙니다. 고려 기간이 30일인 상품과 충동구매 상품은 빈도 기준이 달라야 합니다.

```bash
cat > my-thresholds.json <<'JSON'
{
  "frequency_warn": 4.0,
  "min_conversions_for_claim": 50,
  "min_spend_share_to_report": 0.02
}
JSON

python3 -m metaaudit --env-file .env --thresholds my-thresholds.json
```

전체 목록과 각 값의 근거는 `metaaudit/config.py`에 주석으로 달려 있습니다.
알 수 없는 키가 있으면 조용히 무시하지 않고 오류를 냅니다.

---

## 개발

```bash
python3 -m unittest discover -s tests -t .
```

130개 테스트가 네트워크 없이 돕니다. `tests/fixtures.py`의 합성 계정은 결함이
산술적으로 명확하게 설계돼 있어서, 테스트가 "뭔가 떴다"가 아니라 정확한 값을 단언합니다.

### 구조

```
metaaudit/
├── api.py          Graph API 클라이언트 (페이징, 레이트리밋, 토큰 마스킹)
├── fetch.py        계정 전체를 메모리 스냅샷으로 수집
├── stats.py        유의성 검정 — 모든 성과 주장이 여기를 통과해야 함
├── checks/         감사 항목. 순수 함수이며 I/O 없음
├── plan.py         구조 항목 → 검토 가능한 변경안. I/O 없음
├── apply.py        승인된 변경안 적용 — 쓰기 전 읽기, 되돌리기 기록
├── report.py       text / markdown / json 렌더러
└── cli.py          커맨드라인
```

모든 데이터 수집은 앞단에서 한 번에 끝납니다. 덕분에 감사 항목들은 I/O가 없는 순수 함수가 되고,
API 호출 수는 항목 개수가 아니라 계정 크기에만 비례합니다.

### 새 항목 추가

```python
from .base import CheckResult, Confidence, Finding, Severity, register

@register("my.check", "사람이 읽을 제목")
def check_something(snap, th) -> CheckResult:
    result = CheckResult("my.check", "사람이 읽을 제목")
    ...
    return result
```

`metaaudit/checks/__init__.py`에 import만 추가하면 등록됩니다.
판단할 데이터가 부족하면 **조용히 통과시키지 말고** `skipped_reason`을 채우세요.
건너뛴 항목은 리포트에 따로 표시됩니다 — 건너뛴 건 통과가 아닙니다.

---

## Graph API 버전

기본값은 `.env`의 `META_API_VERSION`입니다. 메타는 버전을 주기적으로 폐기하므로,
`Unsupported get request` 또는 에러 코드 2635가 뜨면 **이것부터 올려보세요.**

```bash
python3 -m metaaudit --env-file .env --api-version v24.0
```

## 보안

- 토큰은 환경변수나 `--env-file`로만 받습니다. 명령행 인자로 받지 않으므로 셸 히스토리에 남지 않습니다.
- 로그·예외·리포트에 나가는 모든 URL에서 `access_token`과 `appsecret_proof`를 마스킹합니다.
- 쓰기 요청 경로가 아예 없습니다. 클라이언트는 GET만 구현합니다.
