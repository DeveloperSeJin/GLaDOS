"""화면 글 두 벌 (한국어·영어). 화면 언어는 glados.toml의 [ui] language.

- 화면 글은 모두 이 표에서 가져온다. 화면(cli, 나중의 glados serve)이 지금 언어로 만든다
- 엔진(루프·정책·위치 판정·손)은 화면 글을 직접 만들지 않는다. 판정·이벤트·오류에 키와 값을 담고,
  화면이 이 표로 그 언어의 글을 만든다
- 엔진 안쪽은 영어로 통일한다 (M2.5): 모델이 보는 글(시스템 프롬프트, 도구 결과·오류, 안내)은 이 표의
  영어("en")나 영어 상수다. 모델은 영어로 생각하고 답하고, 엔진이 마지막에 연구원님의 언어로 번역한다
  (core/translate.py). 정책 판정 기록(policy.jsonl)의 이유·규칙 이름은 예전대로 한국어 식별자다
- 새 화면 글을 넣을 때는 두 언어를 함께 넣는다. 시험이 빠진 언어와 자리표시 차이를 잡는다
"""

from __future__ import annotations

from typing import Any

LANGS = ("ko", "en")
LANG_NAMES = {"ko": "한국어", "en": "English"}
DEFAULT_LANG = "ko"

_current = DEFAULT_LANG


def set_language(lang: str) -> None:
    global _current
    if lang not in LANGS:
        raise ValueError(f"지원하지 않는 언어입니다: {lang}")
    _current = lang


def get_language() -> str:
    return _current


def t(key: str, lang: str | None = None, **params: Any) -> str:
    """키의 글을 지금 언어(또는 lang)로. 표에 없는 키는 키를 그대로 돌려준다."""
    entry = TEXT.get(key)
    if entry is None:
        return key
    text = entry.get(lang or _current) or entry[DEFAULT_LANG]
    return text.format(**params)


def localized(obj: Any, fallback: str = "", lang: str | None = None) -> str:
    """키(key)와 값(params)을 가진 판정·오류를 지금 언어 글로. 키가 없으면 fallback(또는 str)."""
    key = getattr(obj, "key", "") or ""
    if not key:
        return fallback or str(obj)
    params = dict(getattr(obj, "params", ()) or ())
    if "why_key" in params:                                   # 보호 위치 설명은 그 자체가 키
        params["why"] = t(params.pop("why_key"), lang, arg=params.pop("why_arg", ""))
    if "what_keys" in params:                                 # 개인 정보 종류: 키 여러 개 (privacy.*)
        params["what"] = ", ".join(t(k, lang) for k in str(params.pop("what_keys")).split(",") if k)
    return t(key, lang, **params)


class KeyedError(Exception):
    """화면 언어로 바꿀 수 있는 오류: 한국어 글(기록용) + 키와 값."""

    def __init__(self, message: str = "", key: str = "", **params: Any):
        super().__init__(message)
        self.key = key
        self.params = params


# ---------------------------------------------------------------------- 기록 표시 (/log)
_WHO = {"정책": "who.policy", "연구원님:이번만": "who.once", "연구원님:이 작업 동안": "who.task",
        "연구원님:거절": "who.denied", "연구원님": "who.you", "연구원님:직접": "who.direct"}
_RULE_EN = {
    "코드": "code", "기본": "default", "연구원님": "user", "없음": "none", "명령": "command",
    "서버": "server", "저장소": "repo",
    "관리자권한": "admin", "미등록도구": "unregistered", "읽기전용단계": "readonly_stage", "음성": "voice",
    "안전장치": "safety", "관리자표시": "admin_flag", "공유서버": "shared_server",
    "안전장치폴더": "safety_folder", "명령잇기": "chained", "보호위치명령": "protected_command",
    "개인정보": "private_data", "내부주소": "local_address", "환경변수": "env_vars", "남의파일": "others_files", "서버지우기": "server_delete",
}


def who_text(value: str, lang: str | None = None) -> str:
    key = _WHO.get(value)
    return t(key, lang) if key else value


def rule_text(rule: str, lang: str | None = None) -> str:
    """규칙 이름은 기록용 식별자라 한국어로 저장한다. 영어 화면에서는 아는 조각만 바꿔 보여 준다."""
    if (lang or _current) == "ko":
        return rule
    return ":".join(_RULE_EN.get(part, part) for part in rule.split(":"))


# ---------------------------------------------------------------------- 표
TEXT: dict[str, dict[str, str]] = {
    # 시작·끝
    "usage": {
        "ko": "사용법\n  glados              대화 시작 (TUI 화면)\n  glados --plain      줄 화면으로 대화 (TUI에서 한글 입력이 이상할 때)\n"
              "  glados \"할 일\"      한 줄로 맡기고 답을 받은 뒤 끝냄\n"
              "  glados --version    버전\n\n설정·기록 폴더: GLADOS_HOME 환경 변수, 없으면 ~/.glados\n"
              "화면 언어: glados.toml의 [ui] language = \"ko\" 또는 \"en\"\n"
              "화면 종류: glados.toml의 [ui] screen = \"tui\" 또는 \"plain\"",
        "en": "Usage\n  glados              start a conversation (TUI screen)\n"
              "  glados --plain      use the line screen (if typing misbehaves in the TUI)\n"
              "  glados \"task\"       run one task, print the answer, exit\n"
              "  glados --version    version\n\nSettings and logs folder: GLADOS_HOME, or ~/.glados if unset\n"
              "Screen language: [ui] language = \"ko\" or \"en\" in glados.toml\n"
              "Screen type: [ui] screen = \"tui\" or \"plain\" in glados.toml",
    },
    "header": {"ko": "GLaDOS v0 · 모델 {model}{mode} · {os} · 기록 {home}",
               "en": "GLaDOS v0 · model {model}{mode} · {os} · logs {home}"},
    "header.low": {"ko": " (낮은 모드)", "en": " (low mode)"},
    "greet.first": {"ko": "처음 뵙겠습니다. 설정 파일을 {home}에 만들었습니다. 무엇을 실험할까요?",
                    "en": "Hello. I created the settings files in {home}. What shall we test?"},
    "greet.again": {"ko": "다시 오셨군요. 무엇을 실험할까요?", "en": "You're back. What shall we test?"},
    "bye": {"ko": "기록은 남겨 두었습니다.", "en": "Everything has been logged."},
    "admin.refused": {
        "ko": "관리자 권한으로 실행되어 멈췄습니다. GLaDOS는 일반 권한으로 실행해야 합니다. 관리자가 아닌 창에서 다시 실행하세요.",
        "en": "Stopped: running with administrator rights. GLaDOS must run as a normal user. "
              "Start it again from a non-administrator window.",
    },
    "config.error": {"ko": "설정을 읽을 수 없습니다. {detail}", "en": "Cannot read the settings. {detail}"},
    "utf8.hint": {
        "ko": "참고: PYTHONUTF8이 꺼져 있습니다. 'setx PYTHONUTF8 1' 후 새 창에서 실행하면 한글 처리가 더 안전합니다.",
        "en": "Note: PYTHONUTF8 is off. Run 'setx PYTHONUTF8 1' and open a new window for safer handling of Korean text.",
    },

    # 모델 확인·호출
    "model.missing": {"ko": "모델 {model}이(가) 없습니다. 'ollama pull {model}'으로 받은 뒤 다시 실행하세요.",
                      "en": "Model {model} is not installed. Run 'ollama pull {model}' and start again."},
    "ollama.unreachable": {"ko": "Ollama에 연결할 수 없습니다 ({host}). Ollama가 켜져 있는지 확인하세요.",
                           "en": "Cannot reach Ollama ({host}). Check that Ollama is running."},
    "ollama.check_failed": {"ko": "Ollama를 확인할 수 없습니다. {detail}", "en": "Could not check Ollama. {detail}"},
    "llm.http": {"ko": "Ollama 응답 오류 {status}: {body}", "en": "Ollama error {status}: {body}"},
    "llm.bad_response": {"ko": "Ollama 응답을 읽을 수 없습니다: {detail}", "en": "Could not read the Ollama response: {detail}"},
    "llm.format": {"ko": "형식 오류가 {count}번 이어졌습니다: {detail}", "en": "Malformed output {count} times in a row: {detail}"},
    "task.interrupted": {"ko": "멈췄습니다.", "en": "Stopped."},
    "task.llm_failed": {"ko": "모델을 부를 수 없습니다. {detail}", "en": "Could not call the model. {detail}"},

    # 작업 화면
    "render.working": {"ko": "작업 중...", "en": "Working..."},
    "render.plan": {"ko": "계획", "en": "Plan"},
    "render.sources": {"ko": "출처: {sources}", "en": "Sources: {sources}"},
    "render.sources_more": {"ko": "출처: {sources} 외 {count}개", "en": "Sources: {sources} (+{count} more)"},
    "render.blocked": {"ko": "[차단] {tool}: {reason}", "en": "[Blocked] {tool}: {reason}"},
    "render.egress_search": {"ko": "↗ 외부 전송 · {host} · 검색어 \"{what}\"", "en": "↗ sent outside · {host} · query \"{what}\""},
    "render.egress_fetch": {"ko": "↗ 외부 전송 · {host} · 페이지 읽기 {what}", "en": "↗ sent outside · {host} · fetch {what}"},
    "guard.sources": {"ko": "답의 출처 주소가 도구 결과에 없어 다시 확인하게 했습니다",
                      "en": "The answer cited URLs that no tool returned, so it was sent back to check"},
    "guard.evidence": {"ko": "확인하지 않은 답이라 다시 확인하게 했습니다",
                       "en": "Unverified answer; asked the model to check first"},
    "guard.act": {"ko": "계획만 쓰고 시작하지 않아 첫 걸음을 떼게 했습니다",
                  "en": "It only wrote a plan; asked it to take the first step"},
    "render.untranslated": {"ko": "(번역하지 못해 영어 그대로 보여 드립니다)",
                            "en": "(could not translate; showing the original)"},
    "guard.english": {"ko": "모델이 영어로 쓰지 않아 다시 쓰게 했습니다",
                      "en": "The model did not write in English; asked for a rewrite"},
    "guard.promise": {"ko": "하겠다고만 하고 하지 않아서 지금 하게 했습니다",
                      "en": "The model only promised to act; asked it to act now"},
    "stop.format": {"ko": "모델 출력 형식이 계속 맞지 않아 멈췄습니다.",
                    "en": "Stopped: the model kept returning malformed output."},
    "stop.same_call": {"ko": "같은 호출({tool})이 {count}번 이어져 멈췄습니다.",
                       "en": "Stopped: the same call ({tool}) repeated {count} times."},
    "stop.tool_errors": {"ko": "{tool} 오류가 {count}번 나서 멈췄습니다.", "en": "Stopped: {tool} failed {count} times."},
    "stop.max_steps": {"ko": "한 작업 최대 {limit}번을 넘어 멈췄습니다.",
                       "en": "Stopped: reached the limit of {limit} steps for one task."},
    "stop.error_detail": {"ko": "  오류: {detail}", "en": "  Error: {detail}"},
    "stop.user": {"ko": "연구원님이 멈췄습니다.", "en": "Stopped by the Researcher."},
    "stop.denied": {"ko": "정책에 {count}번 막혀 멈췄습니다.", "en": "Stopped: blocked by policy {count} times."},

    # 도구 오류 (모델에게는 영어로, 화면에는 설정 언어로)
    "tool.unavailable": {"ko": "이 컴퓨터에서는 {tool}을(를) 쓸 수 없습니다", "en": "{tool} is not available on this computer"},
    "tool.timeout": {"ko": "{tool} 실행이 {seconds}초를 넘었습니다", "en": "{tool} took longer than {seconds}s"},
    "tool.failed": {"ko": "{tool} 실패: {detail}", "en": "{tool} failed: {detail}"},
    "tool.path_empty": {"ko": "경로가 비어 있습니다", "en": "The path is empty"},
    "tool.not_number": {"ko": "숫자가 아닙니다: {value}", "en": "Not a number: {value}"},
    "tool.binary": {"ko": "글 파일이 아닙니다 (이진 파일)", "en": "Not a text file (binary)"},
    "tool.too_big": {"ko": "파일이 너무 큽니다 (30MB 넘음)", "en": "The file is too large (over 30MB)"},
    "tool.content_needs_query": {"ko": "content=true이면 query에 찾을 글을 적어야 합니다",
                                 "en": "content=true needs the text to look for in query"},
    "tool.no_roots": {"ko": "찾을 폴더가 없습니다: {folders}", "en": "None of the folders exist: {folders}"},
    "tool.inspect_question": {"ko": "fs.inspect에는 파일마다 물을 질문(question)이 있어야 합니다",
                              "en": "fs.inspect needs a question to ask about each file"},
    "tool.inspect_model": {"ko": "파일을 살펴볼 모델이 연결되어 있지 않습니다",
                           "en": "No model is connected to inspect the files"},
    "tool.inspect_failed": {"ko": "파일을 살펴보다가 모델을 부르지 못했습니다: {detail}",
                            "en": "Could not ask the model while inspecting files: {detail}"},
    "tool.pages_format": {"ko": "pages는 \"3\" 또는 \"2-4\" 형태로 적습니다: {pages}",
                          "en": "pages must look like \"3\" or \"2-4\": {pages}"},
    "tool.pages_range": {"ko": "쪽 범위가 맞지 않습니다 (전체 {total}쪽): {pages}",
                         "en": "Page range out of bounds ({total} pages in total): {pages}"},
    "tool.pdf_open": {"ko": "PDF를 열 수 없습니다 (암호가 걸렸거나 깨진 파일): {detail}",
                      "en": "Cannot open the PDF (encrypted or damaged): {detail}"},
    "tool.docx_open": {"ko": "docx를 열 수 없습니다: {detail}", "en": "Cannot open the docx: {detail}"},
    "tool.no_file": {"ko": "파일이 없습니다: {path}", "en": "No such file: {path}"},
    # 원격 손 (M4)
    "tool.remote_unknown": {"ko": "{server}는 servers.toml에 없는 서버입니다 (있는 서버: {servers})",
                            "en": "{server} is not a configured server (configured: {servers})"},
    "tool.remote_hostkey": {"ko": "{server}의 서버 키가 known_hosts에 없습니다. 터미널에서 한 번 `ssh {server}`로 접속해 키를 확인해 주세요",
                            "en": "The host key of {server} is not in known_hosts. Tell the Researcher to connect once "
                                  "with `ssh {server}` in a terminal and accept the key"},
    "tool.remote_auth": {"ko": "{server}에 키로 로그인하지 못했습니다. GLaDOS는 비밀번호를 쓰지 않습니다: ssh-agent에 키를 넣거나(ssh-add) ~/.ssh/config의 IdentityFile을 확인해 주세요",
                         "en": "Key login to {server} failed. GLaDOS does not use passwords: the Researcher needs the key "
                               "in ssh-agent (ssh-add) or an IdentityFile in ~/.ssh/config"},
    "tool.remote_unreachable": {"ko": "{server}에 연결할 수 없습니다: {detail} (학교 밖이면 VPN을 확인해 주세요)",
                                "en": "Cannot reach {server}: {detail} (outside the campus network a VPN may be needed)"},
    "tool.remote_timeout": {"ko": "{server}에서 {seconds}초 안에 끝나지 않았습니다", "en": "Nothing came back from {server} within {seconds}s"},
    "tool.remote_failed": {"ko": "{server}에서 실패했습니다: {detail}", "en": "Failed on {server}: {detail}"},
    "tool.remote_no_gpu": {"ko": "{server}에서 nvidia-smi를 찾지 못했습니다 (GPU나 드라이버가 없음)",
                           "en": "nvidia-smi is not available on {server} (no NVIDIA GPU or driver)"},
    "tool.remote_no_file": {"ko": "{server}에 없는 경로입니다: {path}", "en": "No such file or folder on {server}: {path}"},
    "tool.remote_outside": {"ko": "{server}의 {path}는 내 홈·작업 폴더 밖이라 읽지 않습니다",
                            "en": "{path} on {server} is outside your home and work folders, so it was not read"},
    "tool.remote_empty": {"ko": "명령이 비어 있습니다", "en": "The command is empty"},
    # 웹 손 (M3). 영어는 모델이 보는 글 (연구원님께 전할 것을 함께 적음)
    "tool.web_off": {"ko": "웹 검색이 아직 설정되지 않았습니다. glados.toml의 [web] search에 검색 서비스를 적어 주세요 (README의 M3 설정)",
                     "en": "Web search is not set up on this computer. Tell the Researcher to choose a search service "
                           "under [web] search in glados.toml (see the README, M3 setup)"},
    "tool.web_key": {"ko": "{name} 검색을 쓰려면 환경 변수 {var}에 API 키가 있어야 합니다 (넣은 뒤 GLaDOS를 다시 시작)",
                     "en": "Web search uses {name}, but its API key is missing. Tell the Researcher to set the {var} "
                           "environment variable and restart GLaDOS"},
    "tool.web_query": {"ko": "검색어가 비어 있습니다", "en": "The query is empty"},
    "tool.web_query_long": {"ko": "검색어가 너무 깁니다 ({count}자). 낱말 몇 개로 줄이세요",
                            "en": "The query is too long ({count} chars). Use a few keywords"},
    "tool.web_time_range": {"ko": "time_range는 day, week, month, year 가운데 하나입니다: {value}",
                            "en": "time_range must be day, week, month, or year: {value}"},
    "tool.web_auth": {"ko": "{name}가 API 키를 받지 않았습니다 (HTTP {status}). 환경 변수 {var}를 확인하세요",
                      "en": "{name} rejected the API key (HTTP {status}). Tell the Researcher to check {var}"},
    "tool.web_quota": {"ko": "{name} 검색 한도에 걸렸습니다 (HTTP {status}). 잠시 뒤에 다시 하거나 요금제를 확인하세요",
                       "en": "{name} search limit reached (HTTP {status}). Try later; the Researcher may need to check the plan"},
    "tool.web_http": {"ko": "{name} 검색이 실패했습니다 (HTTP {status})", "en": "{name} search failed (HTTP {status})"},
    "tool.web_searxng_json": {"ko": "SearXNG가 요청을 거절했습니다 (403). settings.yml의 search: formats에 json이 없거나, "
                                    "봇 차단(limiter)이 막았습니다",
                              "en": "SearXNG refused the request (403): JSON output is off (the Researcher needs json in "
                                    "search: formats in its settings.yml) or its bot limiter blocked the request"},
    "tool.web_unreachable": {"ko": "{host}에 연결할 수 없습니다: {detail}", "en": "Cannot reach {host}: {detail}"},
    "tool.web_timeout": {"ko": "{host}가 {seconds}초 안에 답하지 않았습니다", "en": "{host} did not answer within {seconds}s"},
    "tool.web_bad_reply": {"ko": "{name}의 답을 읽을 수 없습니다", "en": "Could not read the reply from {name}"},
    "tool.web_url": {"ko": "http나 https 주소만 읽을 수 있습니다: {url}", "en": "Only http and https URLs can be read: {url}"},
    "tool.web_dns": {"ko": "주소를 찾을 수 없습니다: {host}", "en": "Cannot resolve the host name: {host}"},
    "tool.web_local_dns": {"ko": "{host}는 이 컴퓨터나 내부 네트워크를 가리켜서 읽지 않았습니다",
                           "en": "{host} points to this computer or the local network, so it was not read"},
    "tool.web_redirect_local": {"ko": "이 주소가 내부 네트워크 주소로 넘겨서 따라가지 않았습니다: {url}",
                                "en": "The page redirected to a local network address, which was not followed: {url}"},
    "tool.web_redirects": {"ko": "다른 주소로 너무 많이 넘깁니다 ({count}번 넘음)", "en": "Too many redirects (more than {count})"},
    "tool.is_dir": {"ko": "폴더입니다. fs.search로 안의 파일을 찾으세요: {path}",
                    "en": "That is a folder. Use fs.search to find files inside it: {path}"},
    "tool.unsupported": {"ko": ".{ext} 파일은 아직 못 읽습니다 (v1)", "en": "Cannot read .{ext} files yet (v1)"},
    "tool.unsupported_doc": {"ko": "옛 워드(.doc) 파일은 아직 못 읽습니다. docx로 저장해 주세요",
                             "en": "Old Word (.doc) files cannot be read yet. Save it as .docx"},
    "tool.too_many_names": {"ko": "같은 이름의 파일이 너무 많습니다: {path}", "en": "Too many files with the same name: {path}"},
    "tool.no_content": {"ko": "content가 없습니다", "en": "content is missing"},
    "tool.is_dir_name": {"ko": "폴더 이름입니다. 파일 이름까지 적어 주세요: {path}",
                         "en": "That is a folder name. Include a file name: {path}"},
    "tool.cannot_write_ext": {"ko": ".{ext} 파일은 아직 만들 수 없습니다. .md나 .txt로 저장하세요",
                              "en": "Cannot create .{ext} files yet. Save as .md or .txt"},
    "tool.name_taken": {"ko": "방금 같은 이름의 파일이 생겼습니다. 다시 시도하세요: {path}",
                        "en": "A file with that name just appeared. Try again: {path}"},
    "tool.edit_doc": {"ko": "문서 원본은 고치지 않습니다. fs.read로 읽고 fs.write_new로 새 파일을 만드세요",
                      "en": "Original documents are not edited. Read with fs.read and create a new file with fs.write_new"},
    "tool.old_empty": {"ko": "old(바꿀 원래 글)가 비어 있습니다", "en": "old (the text to replace) is empty"},
    "tool.new_missing": {"ko": "new(새 글)가 없습니다", "en": "new (the replacement text) is missing"},
    "tool.unknown_encoding": {"ko": "인코딩을 알 수 없는 파일이라 고치지 않습니다", "en": "Unknown encoding; the file was not edited"},
    "tool.old_not_found": {"ko": "old를 파일에서 찾지 못했습니다. fs.read로 다시 읽고 글자 그대로 적으세요",
                           "en": "old was not found in the file. Read it again with fs.read and copy the text exactly"},
    "tool.old_many": {"ko": "old가 {count}곳에 있습니다. 앞뒤 글을 더 붙여 한 곳만 맞게 하거나 replace_all=true를 쓰세요",
                      "en": "old appears {count} times. Add surrounding text so it matches once, or use replace_all=true"},
    "tool.no_checkpoint": {"ko": "체크포인트를 남길 수 없어 고치지 않습니다", "en": "Cannot save a checkpoint, so the file was not edited"},
    "tool.admin_not_yet": {"ko": "관리자 권한 실행은 아직 없습니다 (v1). 연구원님께 관리자 창에서 직접 실행해 달라고 하세요",
                           "en": "Running with administrator rights is not available yet (v1). Ask the Researcher to run it from an administrator window"},

    # 승인 카드
    "approve.title": {"ko": "[승인 필요]", "en": "[Approval needed]"},
    "approve.options": {"ko": "  1) 이번만   3) 거절", "en": "  1) Once   3) Deny"},
    "approve.options_task": {"ko": "  1) 이번만   2) 이 작업 동안   3) 거절", "en": "  1) Once   2) For this task   3) Deny"},
    "choose.123": {"ko": "1, 2, 3 중에서 골라 주세요.", "en": "Choose 1, 2, or 3."},
    "choose.13": {"ko": "1 또는 3을 골라 주세요.", "en": "Choose 1 or 3."},

    # !명령 (연구원님이 직접 실행)
    "direct.header": {"ko": "[실행] {command} · 종료 코드 {code} · {seconds:.1f}초 · {cwd}",
                      "en": "[Run] {command} · exit {code} · {seconds:.1f}s · {cwd}"},
    "direct.timeout": {"ko": "[실행] {command} · {seconds:.0f}초가 지나 멈췄습니다 · {cwd}",
                       "en": "[Run] {command} · stopped after {seconds:.0f}s · {cwd}"},
    "direct.stderr": {"ko": "[오류 출력]", "en": "[stderr]"},
    "direct.no_output": {"ko": "(출력 없음)", "en": "(no output)"},
    "direct.more": {"ko": "...({count}줄 더 있음. 전체는 대화 기억과 작업 기록에 있습니다)",
                    "en": "...({count} more lines; the full output is in memory and the task log)"},
    "direct.interrupted": {"ko": "명령을 멈췄습니다.", "en": "Command stopped."},
    "direct.failed": {"ko": "실행할 수 없습니다: {detail}", "en": "Could not run: {detail}"},
    "direct.empty": {"ko": "사용법: !명령 (예: !git status, !cd 폴더)", "en": "Usage: !command (e.g. !git status, !cd folder)"},
    "shell.empty": {"ko": "명령이 비어 있습니다", "en": "The command is empty"},
    "shell.no_cwd": {"ko": "작업 폴더가 없습니다: {path}", "en": "The working folder does not exist: {path}"},
    "shell.cannot_run": {"ko": "명령을 실행할 수 없습니다: {detail}", "en": "Cannot start the command: {detail}"},
    "cd.now": {"ko": "작업 폴더: {path}", "en": "Working folder: {path}"},
    "cd.missing": {"ko": "폴더가 없습니다: {path}", "en": "No such folder: {path}"},
    "cwd.moved": {"ko": "작업 폴더: {path} (GLaDOS 자기 폴더에서는 파일을 만들거나 명령을 돌릴 수 없어 옮겼습니다. 바꾸려면 !cd 폴더)",
                  "en": "Working folder: {path} (moved out of GLaDOS's own folder, where files and commands are blocked. "
                        "Change it with !cd folder)"},
    "cwd.safety_note": {"ko": "참고: 작업 폴더가 GLaDOS 자기 폴더라서, GLaDOS가 부르는 명령은 읽기 명령만 돕니다. !명령은 그대로 됩니다.",
                        "en": "Note: the working folder is GLaDOS's own folder, so commands GLaDOS runs are limited to read-only ones. !commands still work."},

    # 명령
    "help": {
        "ko": "명령어\n  /help     이 도움말\n  /new      새 대화 (앞의 대화를 잊음)\n  /context  지금 기억하고 있는 대화\n"
              "  /log      최근 정책 판정 10개\n  /sent     이 세션에서 밖으로 보낸 것 (웹 검색어, 읽은 주소)\n"
              "  /undo     마지막 파일 수정(fs.edit) 되돌리기\n"
              "  /lang     화면 언어 바꾸기 (/lang ko, /lang en. 이번 세션만)\n  /quit     끝내기\n"
              "  !명령     명령을 바로 실행 (예: !git status, !dir, !cd 폴더). 승인 없이 돌고 결과는 대화 기억에 남습니다\n"
              "  /servers  서버 목록과 연결·GPU 상태 (servers.toml)\n"
              "  /runs /resume   다음 단계에서 만듭니다\n작업 중 Ctrl+C를 누르면 지금 작업만 멈춥니다.\n"
              "답은 물은 언어로 합니다 (한국어로 물으면 한국어, 영어로 물으면 영어).",
        "en": "Commands\n  /help     this help\n  /new      new conversation (forget the earlier one)\n"
              "  /context  what is in memory now\n  /log      last 10 policy decisions\n"
              "  /sent     what was sent outside this session (web queries, fetched URLs)\n"
              "  /undo     undo the last file edit (fs.edit)\n  /lang     change the screen language (/lang ko, /lang en; this session only)\n"
              "  /quit     exit\n  !command  run a command directly (e.g. !git status, !dir, !cd folder); no approval, and the output stays in memory\n"
              "  /servers  servers with connection and GPU state (servers.toml)\n"
              "  /runs /resume   coming in later stages\nCtrl+C during a task stops only that task.\n"
              "Answers follow the language you ask in (Korean or English).",
    },
    "command.later": {"ko": "{cmd}는 {stage}에서 만듭니다.", "en": "{cmd} arrives in {stage}."},
    "command.unknown": {"ko": "모르는 명령입니다: {cmd}. /help를 보세요.", "en": "Unknown command: {cmd}. See /help."},
    "new.done": {"ko": "새 대화를 시작합니다. 앞의 대화는 잊었습니다 (작업 기록 파일에는 남아 있습니다).",
                 "en": "Starting a new conversation. I have forgotten the earlier one (the task log still has it)."},
    "lang.now": {"ko": "화면 언어: {name}. 이번 세션에만 적용됩니다. 계속 쓰려면 glados.toml의 [ui] language를 바꾸세요.",
                 "en": "Screen language: {name}. This session only; to keep it, set [ui] language in glados.toml."},
    "lang.usage": {"ko": "사용법: /lang ko 또는 /lang en (지금: {name})", "en": "Usage: /lang ko or /lang en (now: {name})"},
    "servers.empty": {"ko": "설정된 서버가 없습니다. GLADOS_HOME의 servers.toml에 alias(~/.ssh/config의 Host 이름)와 workdir(내 작업 폴더)를 적고 GLaDOS를 다시 여세요.",
                      "en": "No servers are set up. Add alias (the Host name in ~/.ssh/config) and workdir (your work folder) "
                            "to servers.toml in GLADOS_HOME, then reopen GLaDOS."},
    "servers.head": {"ko": "서버 {count}대 (servers.toml, 접속은 ~/.ssh/config와 ssh-agent)",
                     "en": "{count} server(s) (servers.toml; connection details from ~/.ssh/config and ssh-agent)"},
    "servers.line": {"ko": "  {alias}  {state} · GPU {gpus}장 · 작업 폴더 {workdir}", "en": "  {alias}  {state} · {gpus} GPUs · work folder {workdir}"},
    "servers.connected": {"ko": "연결됨", "en": "connected"},
    "servers.idle": {"ko": "연결 안 됨", "en": "not connected"},
    "servers.seen": {"ko": "    마지막 GPU 확인 {time}: 빔 {free}, 확인 중 {checking}, 내 것 {mine}, 사용 중 {busy}",
                     "en": "    last GPU check {time}: free {free}, checking {checking}, yours {mine}, in use {busy}"},
    "servers.hint": {"ko": "GPU를 보려면 \"{alias} GPU 비었어?\"처럼 물어보세요.", "en": "To check GPUs, ask \"Is a GPU free on {alias}?\""},
    "sent.empty": {"ko": "이 세션에서 이 컴퓨터 밖으로 보낸 것이 없습니다.", "en": "Nothing has been sent outside this computer in this session."},
    "sent.head": {"ko": "이 세션의 외부 전송 {sent}건 (웹 검색 {searches}건)", "en": "Sent outside in this session: {sent} (web searches: {searches})"},
    "sent.search": {"ko": "검색", "en": "search"},
    "sent.fetch": {"ko": "읽기", "en": "fetch "},
    "sent.log": {"ko": "전체 기록: {path}", "en": "Full log: {path}"},
    "web.on": {"ko": "웹 검색: {name} ({host}). 검색어와 읽은 주소는 외부 전송으로 세어 /sent에 보여 드립니다.",
               "en": "Web search: {name} ({host}). Queries and fetched URLs count as sent outside; see /sent."},
    "web.off": {"ko": "웹 검색이 꺼져 있습니다 (페이지 읽기는 됩니다). 켜는 법은 README의 M3 설정.",
                "en": "Web search is off (reading pages still works). To turn it on, see the README, M3 setup."},
    "web.nokey": {"ko": "웹 검색: {name}을(를) 골랐지만 환경 변수 {var}에 API 키가 없습니다.",
                  "en": "Web search: {name} is chosen, but the {var} environment variable has no API key."},

    # /context
    "context.empty": {"ko": "기억하고 있는 대화가 없습니다.", "en": "No conversation in memory."},
    "context.head": {"ko": "지난 작업 {count}개를 기억합니다. 다음 작업에 넣을 것: {summary}",
                     "en": "Remembering {count} past tasks. The next task will include: {summary}"},
    "context.size": {"ko": " (약 {used:,}자 / {budget:,}자)", "en": " (about {used:,} / {budget:,} characters)"},
    "context.count": {"ko": "{level} {count}개", "en": "{count} {level}"},
    "level.full": {"ko": "전체", "en": "full"},
    "level.clipped": {"ko": "줄임", "en": "clipped"},
    "level.brief": {"ko": "요약", "en": "summary"},
    "level.dropped": {"ko": "뺌", "en": "dropped"},

    # /log
    "log.empty": {"ko": "아직 정책 판정 기록이 없습니다.", "en": "No policy decisions yet."},
    "who.policy": {"ko": "정책", "en": "policy"},
    "who.once": {"ko": "연구원님:이번만", "en": "you: once"},
    "who.task": {"ko": "연구원님:이 작업 동안", "en": "you: this task"},
    "who.denied": {"ko": "연구원님:거절", "en": "you: denied"},
    "who.you": {"ko": "연구원님", "en": "you"},
    "who.direct": {"ko": "연구원님:직접", "en": "you: typed"},

    # /undo
    "undo.none": {"ko": "되돌릴 파일 수정이 없습니다.", "en": "No file edit to undo."},
    "undo.title": {"ko": "[되돌리기]", "en": "[Undo]"},
    "undo.confirm": {"ko": "{path} 을(를) {when}에 고치기 전으로 되돌립니다.",
                     "en": "Restore {path} to how it was before the edit at {when}."},
    "undo.options": {"ko": "  1) 되돌리기   3) 그만두기", "en": "  1) Restore   3) Cancel"},
    "undo.done": {"ko": "되돌렸습니다: {path}", "en": "Restored: {path}"},
    "undo.kept": {"ko": "그대로 두었습니다.", "en": "Left unchanged."},

    # 정책 판정 이유 (safety/policy.py의 Decision.key)
    "policy.admin_shell": {"ko": "관리자 권한으로 실행합니다 (OS 확인 창이 한 번 더 뜹니다)",
                           "en": "Runs with administrator rights (the OS will ask you to confirm once more)"},
    "policy.unregistered": {"ko": "등록되지 않은 도구입니다 ({tool})", "en": "Unregistered tool ({tool})"},
    "policy.readonly_stage": {"ko": "가설·설계 단계에서는 쓰기·실행을 하지 않습니다",
                              "en": "No writing or running during the hypothesis and design stages"},
    "policy.voice": {"ko": "음성 요청으로는 정해진 명령만 실행합니다", "en": "Voice requests can only run the fixed command set"},
    "policy.safety_path": {"ko": "GLaDOS 안전장치는 고칠 수 없습니다 ({why})", "en": "GLaDOS safety files cannot be changed ({why})"},
    "policy.safety_command": {"ko": "GLaDOS 안전장치 위치를 건드리는 명령입니다", "en": "This command touches a GLaDOS safety location"},
    "policy.admin_inline": {"ko": "관리자 권한은 명령 안에서 올리지 않고 admin 표시로 요청해야 합니다",
                            "en": "Administrator rights must be requested with the admin flag, not inside the command"},
    "policy.remote_sudo": {"ko": "공유 서버에서 sudo·su는 쓰지 않습니다", "en": "sudo and su are not used on shared servers"},
    "policy.remote_proc": {"ko": "공유 서버에서 프로세스·시스템을 건드리는 명령은 하지 않습니다 (내 실행 멈추기는 M5)",
                           "en": "Commands that control processes or the system are not run on shared servers "
                                 "(stopping your own runs comes in M5)"},
    "policy.remote_others": {"ko": "다른 사람의 폴더입니다 ({path}). 공유 서버에서 다른 사람 파일은 읽지 않습니다",
                             "en": "That is another user's folder ({path}). Other users' files are never read on shared servers"},
    "policy.remote_destructive": {"ko": "서버에서 지우거나 옮기거나 덮어쓰는 명령입니다 (되돌릴 수 없음)",
                                  "en": "This command deletes, moves, or overwrites files on the server (cannot be undone)"},
    "policy.remote_outside": {"ko": "내 홈·작업 폴더 밖입니다 ({path})", "en": "Outside your home and work folders ({path})"},
    "policy.safety_cwd": {"ko": "GLaDOS 안전장치 폴더({why}) 안에서는 허용 목록의 읽기 명령만 실행합니다",
                          "en": "Inside a GLaDOS safety folder ({why}) only allowlisted read-only commands run"},
    "policy.ask_compound": {"ko": "명령을 잇거나(; && |) 출력을 돌리는(>) 명령이라 승인이 필요합니다",
                            "en": "Chained (; && |) or redirected (>) commands need approval"},
    "policy.no_rule": {"ko": "맞는 정책 규칙이 없어 차단합니다", "en": "Blocked: no matching policy rule"},
    "policy.bad_value": {"ko": "정책 값이 잘못되었습니다 ({verdict})", "en": "Invalid policy value ({verdict})"},
    "policy.allow": {"ko": "허용", "en": "Allowed"},
    "policy.deny": {"ko": "정책에 따라 차단합니다", "en": "Blocked by policy"},
    "policy.wait": {"ko": "조건이 맞을 때까지 기다립니다", "en": "Waiting until the conditions are met"},
    "policy.ask_protected": {"ko": "보호 위치입니다: {why}", "en": "Protected location: {why}"},
    "policy.ask_edit": {"ko": "원본 파일을 고칩니다 (고치기 전에 체크포인트를 남깁니다)",
                        "en": "Edits the original file (a checkpoint is saved first)"},
    "policy.ask_trash": {"ko": "파일을 휴지통으로 옮깁니다", "en": "Moves files to the trash"},
    "policy.ask_private": {"ko": "개인 정보나 이 컴퓨터의 파일 내용이 밖으로 나갑니다 ({what}): {sample}",
                           "en": "This would send personal details or file content outside ({what}): {sample}"},
    "policy.ask_local_url": {"ko": "이 컴퓨터나 내부 네트워크의 주소입니다 ({host})",
                             "en": "This address is on this computer or the local network ({host})"},
    "policy.ask_env": {"ko": "환경 변수를 읽는 명령입니다 (API 키가 들어 있을 수 있음)",
                       "en": "This command reads environment variables (they may hold API keys)"},
    "privacy.email": {"ko": "이메일 주소", "en": "email address"},
    "privacy.phone": {"ko": "전화번호", "en": "phone number"},
    "privacy.rrn": {"ko": "주민등록번호", "en": "resident registration number"},
    "privacy.card": {"ko": "카드 번호", "en": "card number"},
    "privacy.secret": {"ko": "비밀 키·토큰", "en": "secret key or token"},
    "privacy.path": {"ko": "파일 경로", "en": "file path"},
    "privacy.name": {"ko": "등록한 이름", "en": "a name from [privacy] names"},
    "privacy.local_text": {"ko": "이 대화에서 읽은 파일의 글", "en": "text from files read in this conversation"},
    "privacy.file_name": {"ko": "이 컴퓨터의 파일 이름", "en": "a file name from this computer"},
    "policy.ask_shell": {"ko": "허용 목록 밖의 명령입니다", "en": "The command is not on the allowlist"},
    "policy.ask_default": {"ko": "승인이 필요한 작업입니다", "en": "This action needs approval"},

    # 위치 설명 (safety/locations.py의 Location.why_key)
    "loc.glados_home": {"ko": "GLaDOS 설정·기록 폴더", "en": "GLaDOS settings and logs folder"},
    "loc.glados_source": {"ko": "GLaDOS 소스 폴더", "en": "GLaDOS source folder"},
    "loc.repo_policy": {"ko": "저장소의 GLaDOS 정책 폴더(.glados)", "en": "the repository's GLaDOS policy folder (.glados)"},
    "loc.keys": {"ko": "키·비밀번호 ({arg})", "en": "keys and passwords ({arg})"},
    "loc.key_file": {"ko": "키·비밀번호 파일 ({arg})", "en": "key or password file ({arg})"},
    "loc.shell_config": {"ko": "셸 설정 ({arg})", "en": "shell configuration ({arg})"},
    "loc.autostart": {"ko": "시스템·자동 실행 ({arg})", "en": "system or autostart location ({arg})"},
    "loc.system_file": {"ko": "시스템 파일 ({arg})", "en": "system file ({arg})"},
    "loc.system_dir": {"ko": "시스템 폴더 ({arg})", "en": "system folder ({arg})"},
    "loc.extra": {"ko": "추가 보호 위치 ({arg})", "en": "extra protected location ({arg})"},

    # 설정 오류 (config.py의 ConfigError.key)
    "config.unreadable": {"ko": "{name}을(를) 읽을 수 없습니다: {detail}", "en": "Cannot read {name}: {detail}"},
    "config.policy_shape": {"ko": "{name}의 최상위는 '도구: {{패턴: 판정}}' 형태여야 합니다",
                            "en": "The top level of {name} must look like 'tool: {{pattern: verdict}}'"},
    "config.mode": {"ko": "glados.toml의 model.mode는 \"normal\" 또는 \"low\"여야 합니다 (지금: {value})",
                    "en": "model.mode in glados.toml must be \"normal\" or \"low\" (now: {value})"},
    "config.language": {"ko": "glados.toml의 ui.language는 \"ko\" 또는 \"en\"이어야 합니다 (지금: {value})",
                        "en": "ui.language in glados.toml must be \"ko\" or \"en\" (now: {value})"},
    "config.motion": {"ko": "glados.toml의 ui.motion은 true 또는 false여야 합니다 (지금: {value})",
                      "en": "ui.motion in glados.toml must be true or false (now: {value})"},
    "config.web_search": {"ko": "glados.toml의 [web] search 값이 잘못되었습니다: {value} ({choices} 가운데 하나, 또는 빈칸)",
                          "en": "Invalid [web] search in glados.toml: {value} (use one of {choices}, or leave it empty)"},
    "config.web_results": {"ko": "glados.toml의 [web] results는 1~10이어야 합니다 (지금: {value})",
                           "en": "[web] results in glados.toml must be 1-10 (now: {value})"},
    "config.screen": {"ko": "glados.toml의 ui.screen은 \"tui\" 또는 \"plain\"이어야 합니다 (지금: {value})",
                      "en": "ui.screen in glados.toml must be \"tui\" or \"plain\" (now: {value})"},

    # TUI 화면 (tui.py) — Workbench 콘솔 디자인을 따른다
    "tui.placeholder": {"ko": "무엇이든 입력하세요", "en": "Ask anything"},
    "tui.placeholder.next": {"ko": "이어서 말씀하세요  ·  /help 명령어  ·  !명령 바로 실행",
                             "en": "Go on  ·  /help for commands  ·  !command to run it"},
    "tui.send": {"ko": "보내기", "en": "Send"},
    "tui.stop": {"ko": "멈춤", "en": "Stop"},
    "tui.new_chat": {"ko": "+ 새 대화", "en": "+ New chat"},
    "tui.nav.running": {"ko": "진행 중", "en": "IN PROGRESS"},
    "tui.nav.session": {"ko": "이번 대화", "en": "THIS CONVERSATION"},
    "tui.nav.empty": {"ko": "아직 없습니다", "en": "None yet"},
    "tui.activity": {"ko": "과정", "en": "PROCESS"},
    "tui.plan": {"ko": "계획", "en": "plan"},
    "tui.memory": {"ko": "기억", "en": "MEMORY"},
    "tui.aside.session": {"ko": "이번 세션", "en": "THIS SESSION"},
    "tui.aside.machine": {"ko": "이 컴퓨터", "en": "THIS COMPUTER"},
    "tui.aside.scope": {"ko": "찾은 범위", "en": "SEARCHED"},
    "tui.aside.sources": {"ko": "출처", "en": "SOURCES"},
    "tui.aside.egress": {"ko": "밖으로 보낸 것", "en": "SENT OUTSIDE"},
    "tui.stat.done": {"ko": "완료", "en": "done"},
    "tui.stat.stopped": {"ko": "멈춤", "en": "stopped"},
    "tui.stat.commands": {"ko": "명령", "en": "commands"},
    "tui.local_note": {"ko": "외부 전송 {sent}건 (웹 검색 {searches}건). 모델은 이 컴퓨터에서 돕니다.",
                       "en": "Sent outside: {sent} (web searches: {searches}). The model runs on this computer."},
    "tui.sent_note": {"ko": "외부 전송 {sent}건 (웹 검색 {searches}건).", "en": "Sent outside: {sent} (web searches: {searches})."},
    "tui.remote_note": {"ko": "모델은 다른 컴퓨터({host})에서 돕니다. 질문과 도구 결과가 그쪽으로 갑니다.",
                        "en": "The model runs on another computer ({host}). Questions and tool results go there."},
    "tui.chip.local": {"ko": "외부 전송 {sent} · 웹 검색 {searches}", "en": "sent outside {sent} · web searches {searches}"},
    "tui.chip.remote": {"ko": "모델 원격 · {host}", "en": "remote model · {host}"},
    "tui.machine": {"ko": "{model}{mode} · {os}", "en": "{model}{mode} · {os}"},
    "tui.folder": {"ko": "작업 폴더 {path}", "en": "working folder {path}"},
    "tui.greet.again": {"ko": "다시 오셨군요.", "en": "You're back."},
    "tui.greet.again_sub": {"ko": "무엇을 실험할까요?", "en": "What shall we test?"},
    "tui.greet.first": {"ko": "처음 뵙겠습니다.", "en": "Hello."},
    "tui.greet.first_sub": {"ko": "설정 파일을 {home}에 만들었습니다. 무엇을 실험할까요?",
                            "en": "I created the settings files in {home}. What shall we test?"},
    "tui.suggest.label": {"ko": "제안 · {kind}", "en": "SUGGESTION · {kind}"},
    "tui.suggest.kind_file": {"ko": "파일", "en": "FILES"},
    "tui.suggest.kind_cmd": {"ko": "명령", "en": "COMMAND"},
    "tui.suggest.file": {"ko": "다운로드에서 논문 찾아줘", "en": "Find the paper in my Downloads"},
    "tui.suggest.file_wit": {"ko": "찾기만 합니다. 읽는 건 따로.", "en": "I only look. Reading is separate."},
    "tui.suggest.cmd": {"ko": "!git status", "en": "!git status"},
    "tui.suggest.cmd_wit": {"ko": "!는 바로 실행합니다. 결과는 기억합니다.",
                            "en": "A leading ! runs it now. I remember the output."},
    "tui.suggest.click": {"ko": "누르면 입력창에 넣습니다", "en": "Click to put it in the input"},
    "tui.chips.label": {"ko": "종류는 GLaDOS가 판단합니다", "en": "GLaDOS decides the kind"},
    "tui.chips": {"ko": "파일 찾기|읽기·요약|새 파일|고치기|명령 실행|웹 검색",
                  "en": "Find|Read·summarize|New file|Edit|Commands|Web search"},
    "tui.k.task": {"ko": "작업", "en": "TASK"},
    "tui.k.command": {"ko": "명령", "en": "COMMAND"},
    "tui.k.slash": {"ko": "명령어", "en": "BUILT-IN"},
    "tui.k.info": {"ko": "안내", "en": "NOTE"},
    "tui.k.running": {"ko": "진행 중 · {step}단계 · {time}", "en": "running · step {step} · {time}"},
    "tui.k.waiting": {"ko": "승인 대기", "en": "awaiting approval"},
    "tui.k.done": {"ko": "완료 · {seconds}초 · 도구 {tools}번", "en": "done · {seconds}s · {tools} tool calls"},
    "tui.k.done_plain": {"ko": "완료 · {seconds}초", "en": "done · {seconds}s"},
    "tui.k.stopped": {"ko": "멈춤 · {seconds}초", "en": "stopped · {seconds}s"},
    "tui.k.cmd_running": {"ko": "실행 중 · {time}", "en": "running · {time}"},
    "tui.time.s": {"ko": "{s}초", "en": "{s}s"},
    "tui.time.ms": {"ko": "{m}분 {s}초", "en": "{m}m {s}s"},
    "tui.live.unit": {"ko": "토큰", "en": "tokens"},
    "tui.live.reading": {"ko": "모델이 읽는 중…", "en": "The model is reading…"},
    "tui.activity.total": {"ko": "= {time} · ↑ {sent}  ↓ {received} 토큰", "en": "= {time} · ↑ {sent}  ↓ {received} tokens"},
    "tui.k.cmd_done": {"ko": "종료 코드 {code} · {seconds}초", "en": "exit {code} · {seconds}s"},
    "tui.k.cmd_short": {"ko": "종료 {code} · {seconds}초", "en": "exit {code} · {seconds}s"},
    "tui.k.cmd_timeout": {"ko": "시간 초과 · {seconds}초", "en": "timed out · {seconds}s"},
    "tui.k.cmd_stopped": {"ko": "멈춤", "en": "stopped"},
    "tui.k.cmd_failed": {"ko": "실행 못 함", "en": "could not run"},
    "tui.state.idle": {"ko": "대기 중", "en": "Idle"},
    "tui.state.checking": {"ko": "모델 확인 중", "en": "Checking the model"},
    "tui.state.no_model": {"ko": "모델 없음", "en": "No model"},
    "tui.state.thinking": {"ko": "작업 중 · 생각 · {step}단계", "en": "Working · thinking · step {step}"},
    "tui.state.tool": {"ko": "작업 중 · {tool} · {step}단계", "en": "Working · {tool} · step {step}"},
    "tui.state.approval": {"ko": "결정 대기 · {tool}", "en": "Decision needed · {tool}"},
    "tui.state.command": {"ko": "명령 실행 중", "en": "Running a command"},
    "tui.state.stopping": {"ko": "멈추는 중...", "en": "Stopping..."},
    "tui.state.done": {"ko": "완료 · {seconds}초", "en": "Done · {seconds}s"},
    "tui.state.stopped": {"ko": "멈춤", "en": "Stopped"},
    "tui.memory_line": {"ko": "기억 {count}개 · {pct}%", "en": "memory {count} · {pct}%"},
    "tui.keys.idle": {"ko": "Enter 보내기 · F1 도움말 · F2 맥락 · F3 작업 목록 · Ctrl+Q 끝내기",
                      "en": "Enter send · F1 help · F2 context · F3 tasks · Ctrl+Q quit"},
    "tui.keys.busy": {"ko": "Esc 멈춤 · F2 맥락 · F3 작업 목록 · Ctrl+Q 끝내기",
                      "en": "Esc stop · F2 context · F3 tasks · Ctrl+Q quit"},
    "tui.busy": {"ko": "작업 중입니다. 끝나거나 Esc로 멈춘 뒤에 해 주세요.",
                 "en": "A task is running. Wait for it, or press Esc to stop it."},
    "tui.quit_hint": {"ko": "끝내려면 Ctrl+Q 또는 /quit", "en": "Ctrl+Q or /quit to exit"},
    "tui.ime_hint": {"ko": "한글 입력이 이상하면 줄 화면으로: glados --plain",
                     "en": "If typing misbehaves, try the line screen: glados --plain"},
    "tui.help_keys": {
        "ko": "TUI 키\n  Enter 보내기 · ↑↓ 보낸 글 다시 쓰기 · Esc/Ctrl+C 지금 작업 멈춤\n"
              "  F1 도움말 · F2 맥락(과정·출처·기억) · F3 작업 목록 · PgUp/PgDn 넘기기 · Ctrl+Q 끝내기\n"
              "  작업 목록을 누르면 그 작업으로 갑니다. 글을 마우스로 끌어 고른 뒤 Ctrl+C로 복사합니다",
        "en": "TUI keys\n  Enter send · ↑↓ recall what you sent · Esc/Ctrl+C stop the current task\n"
              "  F1 help · F2 context (process, sources, memory) · F3 task list · PgUp/PgDn scroll · Ctrl+Q quit\n"
              "  Click a task in the list to jump to it. Drag with the mouse to select text, then Ctrl+C to copy",
    },
    "tui.thought": {"ko": "생각", "en": "thought"},
    "tui.verdict.allow": {"ko": "허용", "en": "allowed"},
    "tui.verdict.deny": {"ko": "차단", "en": "blocked"},
    "tui.verdict.ask": {"ko": "승인", "en": "approval"},
    "tui.verdict.wait": {"ko": "대기", "en": "waiting"},
    "tui.decided": {"ko": "결정", "en": "decision"},
    "tui.approve.kicker": {"ko": "승인 필요", "en": "APPROVAL NEEDED"},
    "tui.approve.once": {"ko": "이번만 허용", "en": "Allow once"},
    "tui.approve.task": {"ko": "이 작업 동안 허용", "en": "Allow for this task"},
    "tui.approve.deny": {"ko": "거절", "en": "Deny"},
    "tui.approve.keys": {"ko": "1·2·3을 누르거나, Esc는 거절", "en": "Press 1, 2 or 3. Esc denies"},
    "tui.approve.keys13": {"ko": "1·3을 누르거나, Esc는 거절", "en": "Press 1 or 3. Esc denies"},
    "tui.undo.kicker": {"ko": "되돌리기", "en": "UNDO"},
    "tui.undo.restore": {"ko": "되돌리기", "en": "Restore"},
    "tui.undo.cancel": {"ko": "그만두기", "en": "Cancel"},
    "tui.undo.keys": {"ko": "1·3을 누르거나, Esc는 그만두기", "en": "Press 1 or 3. Esc cancels"},
    "tui.answered": {"ko": "답함", "en": "answered"},
    "tui.translated": {"ko": "답함 · 번역", "en": "answered · translated"},
    "tui.pasted": {"ko": "[붙여넣은 글 {n} · {lines}줄]", "en": "[pasted text {n} · {lines} lines]"},
    "tui.narrow": {"ko": "창이 좁아 숨겼습니다 (가로 {width}칸 이상에서 보입니다)",
                   "en": "Hidden: the window is too narrow (needs {width} columns)"},
    "tui.error": {"ko": "예상하지 못한 오류가 났습니다: {detail}", "en": "Unexpected error: {detail}"},
}
