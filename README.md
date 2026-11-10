# GLaDOS

Grounded Logic and Desktop Orchestration System. 내 컴퓨터(Windows·맥·리눅스)에서 로컬 모델로 도는 개인 하네스.
설계와 결정은 GLaDOS 프로젝트 문서(`handoff.md`, `integration-decisions.md`)에 있다.

## 지금 단계: M1 (도구 없이 대화)

| 들어 있는 것 | 내용 |
|---|---|
| 설정 | `GLADOS_HOME` 폴더(없으면 `~/.glados`)의 glados.toml·folders.toml·servers.toml·policy.yaml. 처음 실행 때 없는 파일만 만든다 |
| 정책 엔진 | 위치 세 갈래(일반·보호·안전장치), 층 겹치기, 관리자 권한, 음성·실험 단계 규칙. 정책 시험 78개 (전체 128개) |
| 모델 호출 | Ollama `/api/chat`, JSON 스키마 강제, 생각 모드 끔, 형식 오류 2번까지 다시 요청 |
| 에이전트 루프 | 한도(12번, 같은 호출 3번, 같은 도구 오류 2번), 승인(이번만·이 작업 동안·거절) |
| 터미널 화면 | 대화(REPL)와 한 줄 실행. 사실은 굵게, 위트는 흐리게 |
| 작업 기록 | `tasks.db` + `logs/tasks.jsonl`, 정책 판정은 `logs/policy.jsonl` |
| OS별 손 | `hands/windows`, `hands/mac`, `hands/linux` (도구는 M2부터) |

## 설치·실행

Windows (PowerShell, 관리자가 아닌 창)
```
setx PYTHONUTF8 1
setx GLADOS_HOME "C:\Users\User\code\.glados"
# 새 PowerShell 창을 연 뒤
cd C:\Users\User\code\glados
uv sync
uv run pytest
uv run glados
```

리눅스·맥
```
export GLADOS_HOME=~/.glados      # 생략하면 ~/.glados
cd ~/code/glados
uv sync
uv run pytest
uv run glados
```

- 한 줄로 맡기기: `uv run glados "서버 GPU 비었어?"`
- 명령어: `/help`, `/new`, `/log`, `/quit` (나머지는 다음 단계에서)
- 작업 중 Ctrl+C는 지금 작업만 멈춘다
- 관리자 권한으로 실행하면 경고하고 멈춘다

## 장비별 모델 (glados.toml)

| 장비 | `model.mode` | 모델 | 맥락 |
|---|---|---|---|
| 연구실 리눅스 (GPU 48GB), 맥북 | `normal` | qwen3.5:9b (맥은 9b-mlx) | 32k |
| 집 Windows (GPU 8GB) | `low` | qwen3.5:4b | 16k |

## M1 첫날 한글 입력 시험

`uv run glados`에서 아래를 확인하고 결과를 알려 주세요. (Windows Terminal, 연구실 리눅스 터미널)

1. 한글을 치는 동안 조합 중인 글자가 입력 위치에 보이는지
2. 한/영 전환이 되는지
3. 문장 끝 글자가 엔터를 쳐도 빠지지 않는지 (예: "안녕하세요" 끝의 "요")
4. 백스페이스로 한글이 한 글자씩 지워지는지
5. 긴 문장을 붙여넣기 해도 깨지지 않는지

## 구조

```
src/glados/
  cli.py              터미널 화면 (출력은 여기에만)
  config.py           설정 읽기 (utf-8)
  osinfo.py           OS 구분, 관리자 확인
  core/llm.py         모델 호출은 여기 한 곳
  core/loop.py        에이전트 루프 (함수 하나, 나중에 glados serve가 그대로 부름)
  core/schema.py      출력 계약
  core/prompts.py     시스템 프롬프트 (prompts/persona.md)
  safety/policy.py    정책 엔진 check()
  safety/locations.py 위치 세 갈래 판정
  hands/              손 (공통 + OS별)
  memory/log.py       작업 기록, 정책 판정 기록
  defaults/           처음 실행 때 쓰는 기본 설정
tests/                정책·루프·모델 호출·설정·화면 시험
```

## 다음

M2 파일(ripgrep 찾기·읽기·새 파일·수정+체크포인트, `/undo`) → M3 웹 → M4 서버 읽기 → M5 서버 실행·감시 → M6 이어하기·기억
