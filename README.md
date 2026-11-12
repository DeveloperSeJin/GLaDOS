# GLaDOS

Grounded Logic and Desktop Orchestration System: a personal harness that runs on a local model on your own computer (Windows, macOS, Linux).
Design notes and decisions live in the GLaDOS project documents (`handoff.md`, `integration-decisions.md`).

## Current stage: M8.3 English inside, experiments as scripts, after v0 (file hands + conversation memory + commands + English-only internals + TUI + model-made plans + web hands + server reads + server runs + experiment sessions + resuming conversations + long-term memory + the `glados` command)

| Feature | What it does |
|---|---|
| Settings | glados.toml, folders.toml, servers.toml, and policy.yaml in the `GLADOS_HOME` folder (`~/.glados` if unset). The first run creates only the files that are missing |
| Policy engine | Three kinds of location (normal, protected, GLaDOS safety), layered rules, admin rights, voice and experiment-stage rules |
| Model calls | Ollama `/api/chat` with a forced JSON schema (down to each tool's arguments), thinking mode off, up to 2 retries on format errors |
| Agent loop | Limits (12 steps, 3 identical calls, 2 errors from the same tool), approvals (once, for this task, deny) |
| Conversation memory (M2.2) | Remembers earlier tasks in full: tool calls and results (found files, text that was read) go into the next request as they were. Past the model's context length (32k at home and in the lab; 16k at home before M7.4), older tasks are shrunk step by step: full → clipped (start of each result) → brief (answer and work notes) → dropped. The length is checked before every call so Ollama never silently cuts the front of the conversation. `/context` shows it, `/new` starts a new conversation (the earlier one is saved, see below) |
| English inside (M2.5) | Everything inside the engine is English: system prompt, the model's reasoning, search queries passed to tools, tool results and errors, memory. The model answers in English and the engine translates the facts into the language the Researcher wrote in; the one-sentence wit stays in English (M6.3) (Korean: polite 합니다 style, technical terms kept in English, no Chinese characters; if translation fails, the English answer is shown with a note). Questions in English get answers in English without translation |
| English matching for search (M2.5) | Words in other languages inside file and folder names (학위논문, 請求書, März) are turned into English keywords (thesis, dissertation, paper) by the model and compared with the query in English. Translated words are cached in `GLADOS_HOME/cache/names-en.json`. No hand-made word lists. Result lines show the English meaning as `en: ...`. If no name matches every word, the 10 closest files in the same scope and kind are shown |
| Screen language (M2.3) | `[ui] language = "ko"` or `"en"` in glados.toml, `/lang en` for this session only. Screen language and answer language are separate (asking in Korean on an English screen gets a Korean answer). Approval cards, block and stop messages, `/log`, `/context`, `/undo`, and tool errors all exist in both languages |
| Commands (M2.4) | `!command`: the Researcher runs it directly from the chat (no approval; the output stays in memory). `!cd folder` changes the working folder. `shell.run`: GLaDOS picks and runs a command. Read-only commands (git status, dir, version checks, ...) run at once; anything else shows an approval card. Chained or redirected commands (; && \| >) always need approval, touching a protected location needs approval every time, and inside GLaDOS's own folders only read-only commands run. PowerShell on Windows, bash/zsh on macOS and Linux. Running as admin comes in v1 |
| Small-model safety nets (M2.1, M2.5) | Evidence check (an answer about files that is not based on tool results from this task or earlier ones), English check (the model did not write in English), and promise-only answers ("I will try...") are each sent back once. Server names from servers.toml (such as `temp`) are never translated as words; if a translation changes one, it is redone once, then the English answer is shown All checks work on English text (anything else is translated first). Repeated identical results get a hint, and a stopped task still answers with what was confirmed. The language note goes at the very end of the system message. The prompt also says to answer from what it already states (the time, the servers, the notes) without tools (M6.2) |
| Broken characters (M6.2) | Some PDFs and Windows file names hold broken characters (half of an emoji, a damaged font table). They are replaced with � where text is extracted, in tool results and errors, and in what is sent to the model, so a single such file no longer stops a whole content search (`UnicodeEncodeError ... surrogates not allowed`) |
| File hands (M2) | `fs.search` find, `fs.read` read (text, pdf, docx), `fs.write_new` new files (never overwrites), `fs.edit` edit (approval + checkpoint) |
| Content index (M7.1) | While GLaDOS is open, a separate low-priority process (`python -m glados.hands.index`) reads the documents in the `search_default` folders of folders.toml (Downloads, Documents, Desktop): pdf, docx, and text files, the whole text up to 400,000 characters or 30 seconds per file, newest files first. It skips the same things file search skips (hidden folders, `node_modules` and the like, GLaDOS's own folders, protected locations, cloud-only files) and keeps everything in `GLADOS_HOME/cache/index.sqlite` (SQLite FTS5 with the trigram tokenizer; never leaves this computer; safe to delete). Changed files are read again every 10 minutes and deleted ones are dropped. `fs.search` with `content=true` takes indexed, unchanged files from the index (whole text, nothing opened, with the page number of the match in a pdf: `in the text (p.2)`) and reads only the rest on the spot as before. Words of three or more characters use the index; shorter ones (Korean two-letter words such as 논문 or 모델) are matched in the stored text directly, a little slower. A name-only search that finds nothing also says how many files contain the words in their text, so the model can decide to look inside. The process ends with GLaDOS (its standard input closes), only one runs even with two GLaDOS windows, and `/index` shows the state (files, scanned PDFs, progress, folders, index size). `[index] enabled = false` in glados.toml turns it off. Not started for one-shot `glados "..."` runs. If this Python's SQLite has no FTS5, content search reads files as before |
| Search by meaning (M7.2) | The indexing process also turns each indexed document into a few vectors with a small embedding model in Ollama (`[index] embed_model`, default `qwen3-embedding:0.6b`, 639 MB, 100+ languages; one-time `ollama pull qwen3-embedding:0.6b`): the beginning plus up to eight 1,000-character pieces spread over the document, 256 numbers each. It pauses while a task runs, since it shares the GPU. `fs.search` with `content=true` then also lists up to five files whose text is **close in meaning** to the query even when the words differ (English query `breach` → a Korean contract about 위약금), with a closeness score and the page, under "Close in meaning (the words may differ; read before relying on them)"; files already found by their words are not repeated, and scores under 0.40 are not shown. The query is embedded by the same Ollama (`[model] host`), so with the default settings nothing leaves this computer. Until the vectors exist the result says so; if the model is not installed, `/index` says to pull it. `embed_model = ""` turns this part off |
| Korean documents and OCR (M7.3) | **hwp and hwpx** are read like docx: `fs.read`, content search, and the content index. hwpx is a zip of XML sections (paragraphs, line breaks, and table cells in order); hwp (Hangul 5.0) is an OLE file (`olefile`) whose body sections hold paragraph-text records (UTF-16, object placeholders such as tables skipped; their text comes as their own paragraphs). Password-locked files are reported as locked; distribution (copy-protected) files give only their preview text; Hangul 3.0 files are not read. **Scanned pdf pages and images** (png, jpg, webp, bmp, gif, tiff) are read by OCR with the model GLaDOS already uses (qwen3.5 reads images): the page is drawn with `pypdfium2` (long side 1,600 px, grayscale) and the model transcribes it as written, in its own language. Nothing is installed for it and nothing leaves this computer. It takes about 20 seconds a page, so `fs.read` does the first 2 pages of a scanned pdf by default and at most 4 new pages per call, then says which pages are left; pages with text are read as before and only the blank ones go to OCR. Transcriptions are cached (`cache/doctext.sqlite`) and, if the file is in the content index, added to it so the next content search finds the words. Results are marked OCR so the model checks names and numbers. A model that cannot read images gets a clear error. New dependencies: run `uv sync` (and the `uv tool install ... --reinstall` line for the `glados` command) |
| Every drive and photos (M7.5) | Asked after GLaDOS could not find a driver's license photo kept in Google Drive (the PC mounts it as `G:`): the default scope is now **every drive** (`search_default = ["*"]` in folders.toml; Windows: fixed and removable drives plus Google Drive; Linux and macOS: home plus mounted volumes). Folders starting with `.` or `$`, hidden system folders, GLaDOS's own folders, protected locations (Windows, Program Files, AppData, ...), and package folders (`node_modules`, `anaconda3`, ...) are skipped. The **index now lists every file** (name, size, time), so a search over a big scope (a drive, the home folder) reads that list instead of walking the disk, with Downloads and Desktop listed live so new files still show; the result says when the list was made. Content search over a big scope goes to the index's text first and counts the scope from the list (2–5 s for 300,000 files in a test). **Photos and scans**: images of 20 KB or more and pdfs without text are looked at by the model GLaDOS already uses while GLaDOS is idle (newest first; it pauses during tasks): an image becomes "Image: <what it is>" plus the text in it ("a photo of a Korean driver's license card", "자동차운전면허증 ..."), a scanned pdf its first 2 pages; this goes into the index, so content search and search by meaning find it. `fs.read` of an image gives the same. **`fs.inspect` looks at images directly** with the yes/no question ("Is this a photo of a driver's license?", about 1–3 s an image at 896 px, answers remembered). Files only in the cloud (Google Drive streaming, OneDrive) are downloaded to be read (`[index] cloud = true`, the Researcher's choice; `false` keeps names only). Content matches with the words as written come before matches through an English meaning ("면허증" before LICENSE files found through "license"). The same search or read is **not run twice in one task**: the model gets a note to change the words or answer (it repeated one search six times before). OCR text of photos, ID cards included, is kept only in `cache/index.sqlite` and `cache/doctext.sqlite` on this computer; delete them to remove it. `[index] ocr = false` turns photo reading off |
| Finding photos sooner (M7.6) | From the first real run (124,554 files listed, 4,188 photos waiting, none looked at, and the model searched only Documents, then only the home folder, so Google Drive `G:` was never searched): (1) the indexer's model work (reading photos and scans, making vectors) now runs in its **own thread next to the text reading**, so photos are looked at from the start instead of after hours of reading documents; it still pauses during tasks; (2) photos are taken **from your folders first** (Pictures, Desktop, Downloads, Documents, OneDrive, and drives other than the system drive such as Google Drive), photo formats (jpg, webp) before png and the like, then newest first; background reading uses 1,280 px images (faster); `fs.inspect` on images picks images in the same order; (3) content matches are ordered by **relevance** (SQLite FTS5 bm25) within "as written" and "through English meaning", not by date; (4) `fs.search`'s description now says to leave roots out (every drive) unless the Researcher named a folder, the prompt says never to narrow to a folder that was not named, and a narrowed search says which drives it left out ("Only C:\\Users\\User was searched. Leave roots out to search every drive (C:\\, G:\\)"); (5) `/index` says "paused" only while a task really runs |
| Planning before acting (M7.7) | For any task that can use tools, GLaDOS first makes a **separate planning call** before it acts: the model writes the goal, whether tools are needed, 2 to 6 steps (what to look for, every place it could be, which tool, the most likely way first and what to try if that finds nothing), and how it will check the result. For a greeting or a question it can answer, it answers right away from that same call. The planning call sees **facts about this computer now**: the drives a search covers when roots are left out (and which one is Google Drive), the Researcher's own folders, and how far the background index has got (files listed, documents with text, photos looked at and waiting, vectors ready). The plan is shown first, translated into the Researcher's language, on the task card and in the side panel, and the work **goes ahead without waiting for approval**. The plan stays in the current task only (memory keeps the steps). The planning call does not count toward the step limit. If the plan reply is malformed, the first step writes the plan as before (M2.7). `[loop] plan_first = false` turns it off |
| Asking when unclear (M7.8) | From a real run: a one-word request made the model invent a research question (a specific year and measures), run 12 paper and web searches (18 sends to outside services), stop at the step limit, and answer its own made-up question. Now the planning call chooses **next**: `ask`, `answer`, or `act`. When it cannot tell what the Researcher wants and the conversation does not settle it, it does not guess: it asks one short question with two or three likely meanings and ends the task, with no tools and nothing sent outside (the question skips the answer safety nets, is translated, and stays in memory so the reply continues from it). The **goal** restates the request at exactly the scope given, without added details (a year, a measure, a place, a narrower topic); the note after the plan repeats the goal, and the output rules say to answer what was asked, never a narrower question of the model's own, and to say which part was done if only part was |
| Mouse copy, paste, and links (M7.9) | In the TUI, drag to select text, then **right-click to copy** it; **right-click with nothing selected pastes** into the input (the Windows console convention; multi-line text goes in as a pasted-text marker, as with Ctrl+V). **Ctrl+click opens a web link** (http or https only; files and programs are never opened) in the default browser. Web addresses in answers, sources, and the process panel are drawn underlined as links (a Korean particle right after an address is not part of it). Copying uses the operating system's clipboard (Windows clipboard API, `pbcopy`/`pbpaste`, `wl-copy`/`xclip`/`xsel`), falling back to the terminal's OSC 52 copy; Ctrl+C on a selection uses the same path. F1 lists the mouse actions |
| Conversation first (M8) | From daily use: every message ran as a task (a one-word message searched the home folder for files, a passport photo search took 8 tool calls and 111 seconds in silence), plans were translated into orders addressed to the Researcher, and the English wit was often off. Now (1) **the planning call is GLaDOS's turn in the conversation**: besides goal, next, plan, and check (English, for the tools) it writes **say**, what it tells the Researcher now, in their language: the answer, a question when the request is unclear, or what it will do first and where; the plan steps go to the side panel; (2) **work in short stretches**: the output rules tell the model to stop and say what it checked and found, with one short question, before searching a much bigger place, before something slow (such as looking at many photos), or when the likely ways found nothing; and after **6 tool calls** in one task it must come back without tools and ask whether to go on (`[loop] checkin_steps`, 0 turns it off); the reply continues the same conversation; (3) **answers are written in the Researcher's language directly**, so there is no translation call (the translator is only a fallback for an answer in another language); thought, tool arguments, goal, plan, and check stay English, and the English word guards apply to English answers only; (4) **wit only when it truly fits**, still in English, never about the Researcher's intentions; (5) **the TUI looks like a chat**: no "task · done · 39s" line on top; greetings and answers are just the two voices, and when GLaDOS starts using tools a single progress line appears under its first words ("running · step 2 · 0:12" → "done · 39s · 4 tool calls") |
| File and folder links (M8.1) | Paths in answers, sources, command lines, and the side panel (`C:\\...`, `/...`, `~/...`) are underlined links when they exist on this computer; a path with spaces is followed word by word until it names a real path, and a Korean particle stuck to the end is dropped. **Ctrl+click** opens a folder in the file manager and a file in its default app. Files that would run a program (`.exe`, `.bat`, `.ps1`, `.py`, `.lnk`, ...) are never opened: their folder opens with the file selected instead (`opener.py`) |
| Asking means waiting (M8.2) | From daily use after M8: one-word messages got "this is not related to the experiment plan" with a Korean wit, and "let's do a simple experiment" got a good question ("what is the goal and hypothesis?") followed by acting anyway on a random sample file and an unsupported conclusion. Now (A) when GLaDOS's first words end with a question it waits for the answer even if it chose "act" (no tools, nothing sent), and the planning prompt says a question means "ask"; (B) wit is an optional field in both formats (a required field made the small model fill it every time) and a wit that is not English is neither shown nor remembered; (C) the language note gives short Korean examples and says to speak to the Researcher, never about them in the third person; (D) the role line is now the Researcher's lab assistant who does what is asked at the size asked, answers small talk briefly in its own voice, and settles an experiment's goal and hypothesis with the Researcher before running anything (the brand voice stays); (E) the output rules say to claim only what results, the Researcher, or general knowledge support |
| English inside, experiments as scripts (M8.3) | From daily use after M8.2: with the model writing Korean directly, its thoughts, goals, and plans drifted into Korean too, and when asked to write up an experiment it ran it again with new random numbers, so the report did not match the result it had shown. Now (1) **the model writes everything in English again** (thought, tool arguments, say, answer), and only the screen gets a translation; the Researcher accepted translation-style Korean for this; an answer not in English is sent back once, then taken as written (the M8 direct-Korean answers are undone; the conversation turn, check-ins, and chat look stay); (2) **experiments run as script files**: the code goes into a new file in the experiments folder (`<workspace>/experiments`) with a fixed random seed and the settings at the top, then that file is run, and its path goes in sources; summaries, tables, and write-ups of earlier work use the results already in the conversation instead of running again (if it must run again, the same script and seed); and anything the Researcher asks to see (a table, a list, numbers) goes in the answer itself, even when it is also saved to a file |
| Plans and inspection (M2.7) | **The model decides how to do the task** (no hard-coded rules such as what counts as a paper). When tools are available, the first reply starts with a `plan`: what needs to be known, where it is, which tools get it, and how to check the result; the first step follows in the same reply. Output order is forced as plan → thought → `action` (null if none) → `done`; a plan without an action is sent back once. The TUI shows the plan in the right-hand process panel, the line screen shows it dimmed. Three general tools look inside files: `fs.search content=true` (words inside pdf, docx, and text files; for pdf the first 12 and last 3 pages; one matching line per file), **`fs.inspect`** (reads the start of each file and answers the model's own question, e.g. "Is this an academic paper?", with yes/no/unsure and a reason; 6 files per call, up to 300 files and 150 s), and `fs.read` (one whole file). Results state in numbers what was and was not checked: names only or contents, how many were read, and how many were skipped (scanned, encrypted, cloud-only, protected location, unreadable types such as xlsx, time limit). Extracted text and inspection answers are cached in `GLADOS_HOME/cache/doctext.sqlite` (same file, question, and model are not asked again; never leaves this computer; safe to delete) |
| TUI (M2.6) | Running `glados` in a terminal opens a full-window screen (Textual) that follows the Workbench console from the design canvas: header (presence symbol, GLaDOS, current state, model and working folder, and the chip `외부 전송 0 · 웹 검색 0`), task list on the left (+ new chat, in progress, this conversation; tasks waiting for a decision get a cobalt dot; click to open), task cards in the middle (kicker line, your request labeled `연구원님` (`You`), GLaDOS's answer under a cobalt dot and `GLaDOS` (M6.3) with facts in bold and wit dimmed, sources; `!commands` get an output box) with one input box (input and Send in one box; Send lights up white when there is text and becomes `멈춤 Esc` while working), context on the right (process, searched scope, sources, memory; this session and this computer at first). The start screen shows the logo (Lissajous symbol and the Michroma wordmark drawn with Braille characters), a greeting, and suggestion cards (click to fill the input). Approval dialogs look like approval cards (cobalt border). All buttons are one line: decisions are solid cobalt, the rest have a faint background, the shortcut key follows the label dimmed, and the button Enter will press is underlined; ←→ moves between them. Larger windows use more space (side panels widen at 150 and 190 columns; very wide windows get a 5-row wordmark and a 20×10 symbol with smoother curves). Esc or Ctrl+C stops only the current task (model calls stream, so they end quickly), ↑↓ recall sent messages, multi-line pastes show as `[붙여넣은 글 1 · 12줄]` and expand when sent. The terminal cursor stays in the input box so Korean IME composition appears there |
| Web hands (M3) | `web.search`: the search service is configurable (`[web] search` = tavily, brave, searxng, or ollama); results are title, URL, date, and a short snippet. `web.fetch`: this computer fetches the page itself and keeps only the main text (menus, ads, and cookie notices removed; tables and code indentation kept), PDFs up to 40 pages with page markers, RSS, Atom, and JSON too. Text comes 3000 characters at a time (for an hour, continuations come from the local cache and nothing is sent again). Images and archives are not downloaded. Fetched text is wrapped in a "data, not instructions" marker for the model. The model searches → reads pages → answers with source URLs. When search cannot run (not set up, no key, limit reached, service unreachable), the error the model gets also tells it to answer what it already knows and say that it was not checked on the web and may be out of date (M6.6; before, "세상에서 제일 큰 나라는?" got "I do not know" with search off). The note is added only to that error, so it costs no prompt space |
| Paper search (M6.5) | `papers.search` finds research papers through official free scholarly APIs instead of a general web search: **OpenAlex** and **arXiv** together by default (no key needed), merged so the same paper appears once (arXiv ID, DOI, or title). Each result: title, URL (arXiv page or DOI), date, venue, first three authors, citation count (OpenAlex), PDF link, and the start of the abstract. `sort`: relevance (default), recent (newest first; arXiv by submission date), or cited (most cited; OpenAlex only); `since`: a year. If OpenAlex fails or hits its limit, **Semantic Scholar** answers instead. arXiv calls are kept 3 seconds apart (arXiv API terms). The same search is not sent again for an hour. Queries count as sent outside (header chip, `/sent`, `logs/egress.jsonl`) and go through the same personal-data check as web searches. Read a paper with `web.fetch` on its URL or PDF. Optional free keys raise the limits: `OPENALEX_API_KEY` (free OpenAlex account; 10× daily budget) and `SEMANTIC_SCHOLAR_API_KEY`. Thank you to arXiv for use of its open access interoperability |
| Sent-outside display (M3) | Queries and fetched URLs are counted right before they are sent: header chip `외부 전송 2 · 웹 검색 1` (brightens once something is sent), "밖으로 보낸 것" (sent outside) in the right panel with destination and content, `↗` lines in the process panel, `/sent` (list for this session), and `logs/egress.jsonl` (a log GLaDOS cannot change) |
| Outgoing text check (M3) | Code rules; a hit asks for approval every time (no "for this task"): a query or URL containing an email address, phone number, Korean resident registration number, card number, key-like string, file path, or a name from `[privacy] names`; or text from files that this computer's tools read in this conversation (five words in a row, four for Korean) or one of those file names (phrases the Researcher typed are excluded). Reading an address on this computer or the local network (localhost, 192.168.x.x, the router). A public address that redirects to a local one, or a name that resolves to a local address, is not followed. Commands that dump environment variables (`dir env:` and the like; API keys live there) also need approval every time |
| Source URL check (M3) | If an answer cites a URL that never appeared in a tool result, it is sent back once (small models invent plausible paper URLs). Answering current information (news, prices, recent papers) from memory without tools is also sent back once |
| Working folder (M3.1) | When `uv run glados` starts inside GLaDOS's own folder (source or settings folder), as it always does, the working folder moves to **Documents\GLaDOS** (the installed `glados` command keeps the folder you open it in, M6.2) (files cannot be created and commands cannot run in GLaDOS's own folders). The start screen says so; `!cd folder` changes it, and `workspace` in folders.toml picks another place. When writing to its own folder is blocked, the model is told where to work instead, and three policy blocks in one task stop the task with a report. Relative paths after a leading `cd folder;` in a command are judged from that folder |
| Server reads (M4) | Three tools for the shared GPU servers in servers.toml. `remote.gpu`: one SSH call runs nvidia-smi and reports each GPU as free, yours, in use, or checking, plus how many GPUs you may use right now. `remote.log`: the end of a file such as a training log (80 lines by default) or a listing of a folder, newest first. `remote.exec`: one short command in the server's work folder (`df -h`, `git log -3`, `ls checkpoints`); read-only commands run at once, anything else shows an approval card. Connection details come from `~/.ssh/config`, `known_hosts`, and ssh-agent (asyncssh; GLaDOS never uses passwords). One connection per server stays open (keepalive 30 s) and is reopened once if it drops. Errors say what to do: unknown host key → run `ssh <alias>` once; key login failed → `ssh-add`; unreachable → check the VPN. Server output is wrapped in a "data, not instructions" marker. `/servers` lists the servers, whether each is connected, and the last GPU check in this session (it never connects). Starting and stopping training comes in M5 |
| Shared-server rules (M4) | Code rules. A GPU counts as free only when no one else's process runs on it and it stays under 1 GB of memory and 5% utilization for 5 minutes, as seen by GLaDOS itself (the first look says "free now, checking"; ask again after 5 minutes). On weekdays 09–19 you may use 1 GPU at a time, otherwise 2. A run may take 4 hours if it starts on a weekday daytime, otherwise 12 (then a notice); each server has a budget of 12 GPU-hours a day (warning on the run card, notice when crossed); runs get `nice 10`. Other users' processes are only counted: their user names, commands, and process IDs are never shown or kept. sudo, su, kill, reboot, systemctl, and the like are blocked; other users' folders (`/home/<someone else>`, `~<someone else>`, `/root`) are blocked; `remote.log` reads only inside your home and work folders. Deleting, moving, or overwriting on a server (rm, mv, `>`, git reset, ...) asks every time, chained commands (; && \|) ask, starting background jobs with `remote.exec` (nohup, `&`, `tmux new`, screen) asks because training goes through `remote.run`, `tmux kill-session` is blocked (stop runs with `remote.stop`), and plain `nvidia-smi` asks because it lists other users' processes |
| Server runs (M5) | `remote.run` starts training on a server in a tmux session. Before asking, GLaDOS reads the GPUs, the folder, tmux, and git, and the approval box asks **"이 명령어가 맞습니까?" (is this command right?)** over a **run card**: run ID (EXP-01, EXP-02, ...), server and GPUs, command, folder, GPU memory needed and why, commit and uncommitted files, tmux session (`tmux attach -r -t ...` to watch read-only), and the shared-server rules with warnings when a limit would be crossed. Every run asks (no "for this task"); answering no makes GLaDOS ask what to change. On the server, `<work folder>/.glados/runs/EXP-NN/` keeps the command (`command.sh`), the run script (`run.sh`), the commit, `diff.patch` (uncommitted changes), the output (`output.log`), and the exit code. The session opens your login shell (so `.bashrc`, conda, and modules load), then runs the command with `CUDA_VISIBLE_DEVICES` set to the chosen GPUs (numbered as in nvidia-smi), `nice 10`, and unbuffered Python output. If no GPU fits yet (see GPU memory below), the run waits in a queue and starts by itself once one does, as long as GLaDOS stays open. `remote.stop` sends Ctrl+C (so training can save), closes the session if it is still running after 15 s, and also cancels a queued run; it asks every time. `remote.log run=EXP-NN` shows a run's state, its metrics table, and the end of its output. `/runs` lists runs. Runs are recorded in `runs.db` and `logs/runs.jsonl` in `GLADOS_HOME` |
| GPU memory (M5.1) | A run needs a GPU with enough free memory, not an empty GPU: free memory (total − used) must stay above the run's need plus a margin (10%, at least 2 GB) for 5 minutes, judged by the lowest value in those 5 minutes (other people's jobs grow and shrink, e.g. during evaluation). This can put a run on a GPU someone else is using; the run card then warns that both jobs slow down and that either may run out of memory if the other grows (a stronger warning when that GPU is over 80% busy). GPUs used by no one else and with the most free memory are chosen first. The need comes from, in order: what you said ("24GB 필요", `mem_gb`); the peak GLaDOS measured when the same command ran before in the same folder (it records your process's GPU memory every minute); an estimate from the code. For the estimate, GLaDOS reads the relevant lines on the server (the script's arguments and model, batch, precision, optimizer lines, the config files in the command), the model fills in the facts (model size, full/LoRA/frozen/inference, precision, optimizer, batch, sequence length, hidden size, layers), and a formula computes it (weights, gradients, optimizer state, activations, CUDA overhead); the run card shows the basis, e.g. `bert-base-uncased · 0.11B · fp32 · full training · adam · batch 32 · seq 128`. If the size cannot be worked out, only a fully free GPU is used, as before |
| Projects (M5.2) | Folders under `<work folder>/code` on the server (`projects` in servers.toml) are projects. "무슨 프로젝트 있어?" (what projects are there?) lists them with git branch, last commit date, the README's first lines, and the scripts. Asking about one makes GLaDOS read its files (layout, README, entry files and their `argparse`/hydra/click arguments with defaults, shell scripts, configs, requirements) and explain what it does and how to run it. Give it a script and arguments ("bert-cls에서 train.py --batch 32 돌려줘") and it builds the full command for that project, runs it in the project folder (`project=bert-cls`), and asks "이 명령어가 맞습니까?" first |
| Experiment sessions (M5.3) | Each run is an experiment session with a folder on this computer: **Documents\GLaDOS\experiments\EXP-NN_project** (under `workspace` if set in folders.toml). GLaDOS reads what the run prints (the server's `output.log`) and builds `metrics.csv` (a table of every epoch or step), `metrics.png` (a graph per metric over the whole run, best value marked), and `summary.md` (command, commit, GPUs, memory, state, the table with every epoch (evenly thinned to 200 rows only for very long step-based logs), best values, the last lines of output); copies go to the run's record folder on the server too. Output repeats the same shape, so only the beginning is used to find the pattern: once about 40 lines with numbers have appeared, the model looks at the names found in them, a few example lines, and the first lines, and decides what to extract (x axis such as epoch or step; metrics such as train_loss, dev_acc with the word that marks dev lines; whether higher or lower is better). After that, GLaDOS reads only new output, takes just those numbers into the table, and drops the text, so it keeps little: the table, where it stopped reading, the current epoch, the names it has seen, and the last 50 lines. If a new kind of line appears later (dev results often start only after the first epoch), it shows the model just those names and decides again (up to 3 times). Any print format works; the full output stays on the server. Updated every 5 minutes while the run goes, at the end, and whenever you ask "EXP-03 어때?" (how is EXP-03?) or continue it ("EXP-03 이어서"); the end notice shows the best values and the folder. If GLaDOS was closed, it catches up the next time it opens |
| Resuming conversations (M6) | Every finished task (and every `!command`) is saved to the current conversation in `GLADOS_HOME/sessions.db`, in full, as it sits in memory (tool calls and results, answers, work notes). A conversation starts with the first task after GLaDOS opens or after `/new`. `/resume` lists earlier conversations (date, first request, number of tasks), `/resume 2` loads one into memory and later tasks are added to it; the conversation you were in stays saved. You can also just ask ("어제 하던 거 이어서 하자", "resnet 실험 하던 거 이어서"): the model calls `session.resume` with words from that conversation, kept in your own words and language like `memory.note` text (M6.6: "얼룩말 프로젝트 하던거 이어서 하자" was searched as "Equestria" and found nothing) (or none for the latest; the conversation matching the most words wins, then the one with more tasks, then the newer one), gets a summary of its tasks for the current answer, and once the task ends that conversation is loaded into memory. When memory is empty, the system prompt lists the three most recent conversations (date, first request, number of tasks) so the model can pick the right one, and a rule says to call `session.resume` first when the Researcher mentions earlier work that is not in memory ("아까", "어제", "이어서"). The examples in the prompt are marked as examples, not requests (a small model once copied the file-search example as if it were the earlier task). The TUI start screen shows a **continue card** for the latest conversation (click to load it); the line screen prints a hint. Conversations that hold only `!commands` are saved but left out of these lists, the card, and `session.resume`, and a conversation that starts with a `!command` is titled by its first request (M6.6: `!git status` showed up as an earlier conversation). Saved conversations never leave this computer |
| Long-term memory (M6) | `GLADOS_HOME/MEMORY.md`: things you ask GLaDOS to remember ("실험 결과는 항상 표로 보여줘, 기억해"). The model calls `memory.note` with one line in your own words and language, and **every note asks first**: the approval box shows the exact line (기억하기 / 기억하지 않기; no "for this task", and setting it to allow in policy.yaml still asks). Notes go into every task's system prompt, newest first, within a limit (500 characters at 16k context, 1500 at 32k, 3000 above). `/memory` lists them with numbers, `/memory del 2` deletes one, and the file is plain Markdown you can edit: one line per note, with or without `- ` (headings, blank lines, and `<!-- comments -->` are not notes). **Secrets are never remembered (M6.2)**: a note that looks like a password, PIN, card number, key, token, or resident registration number is blocked by a code rule before the approval box (approving cannot override it), and the model is told not to save it anywhere else and to suggest a password manager. A secret already in the file (written by hand or saved before M6.2) is not given to the model; `/memory` marks it so you can delete it |
| Watching runs (M5) | While GLaDOS is open, it checks waiting and running runs every minute (only servers that have one): finished (exit code and last line), failed, gone without an exit code, loss NaN or inf, the maximum run time reached, today's GPU-hour budget exceeded, and the work folder's disk over 90% full. NaN and time limit are "decision needed" notices: GLaDOS does not stop anything by itself; say "EXP-03 멈춰" (stop EXP-03) to stop it. Notices appear as cards in the chat (cobalt for decisions), as a toast, and as an OS notification (Windows notification center; macOS and Linux notifications too; `[ui] notify = false` turns that off). Nothing runs on the server besides your runs: runs that ended while GLaDOS was closed are noticed the next time it opens, and runs that were still waiting for a GPU when it closed are cancelled with a notice |
| Live progress (M2.7) | While a TUI task runs: elapsed time (header state, card kicker, and the task list, e.g. `진행 중 · 2단계 · 47초`), tokens exchanged with the model (inside the card: `↑ sent  ↓ received`, with the last three lines of text being received: plans, thoughts, tool calls, inspection, translation). When the answer arrives the box is cleared and only the answer remains (also on stop). The process panel gets a total line (`= 47초 · ↑ 6.1k ↓ 102 토큰`). ↑ is Ollama's prompt_eval_count, ↓ is the chunk count while receiving and eval_count at the end. Refreshed every 0.25 s but paused while the input box has text (protects Korean IME composition); with `[ui] motion = false` it refreshes only when the step changes. The line screen (`--plain`) is unchanged |
| Logo (M2.6) | `logo.py` draws 06 LISSAJOUS with Braille characters. Presence states follow brand-identity: idle is a dim 1:2 curve with a still dot, working is a 3:2 curve with a moving dot (two-dot tail, about 7 s per loop), waiting for a decision is a dim curve with a breathing dot. Only the header symbol moves, only while working or waiting, and it pauses while the input box has text. `[ui] motion = false` keeps it still |
| Line screen | `glados --plain` or `[ui] screen = "plain"`: the classic REPL. One-shot runs (`glados "task"`) and pipes always use the line screen. Facts in bold, wit dimmed |
| Logs | `tasks.db` + `logs/tasks.jsonl`; policy decisions in `logs/policy.jsonl`; conversations in `sessions.db` |
| Tests | 667 (policy 78, outgoing text check 63, file hands 43, commands 42, loop 38, TUI, logo, mouse, and links 46, server reads 29, server runs 28, screen and tool lists 26, experiment sessions 20, screen language 22, conversations and memory 33, broken characters 5, v0 scenarios 6, safety nets 17, memory 17, settings 17, web hands 17, paper search 9, content index and search by meaning 18, Korean documents and OCR 12, every drive and photos 14, conversation turn, asking, check-ins, wit, and experiments 26, model calls 16, English matching 13, contents and inspection 12). The seven v0 scenarios run end to end through the app with a scripted model (`test_scenarios.py`; skipped on Windows, where a fake home folder cannot stand in for the real one). The TUI runs headless (Textual Pilot). Web hands are tested against fake search services and fake sites on this computer (no internet). Server reads, runs, and experiment sessions are tested against a fake SSH server on this computer with fake nvidia-smi and ps, a real tmux, a test clock for the 5-minute rule and time limits, and a scripted model for memory estimates and metric choices (skipped on Windows; the rule and policy tests still run). If PowerShell is installed, Windows command paths run for real |

## Install and run

Windows (PowerShell, not as admin)
```
setx PYTHONUTF8 1
setx GLADOS_HOME "C:\Users\User\code\.glados"
# then open a new PowerShell window
cd C:\Users\User\code\glados
uv sync
uv run pytest
uv run glados
```

Install the `glados` command (M6), so it runs from any folder:
```
uv tool install --editable C:\Users\User\code\glados
uv tool update-shell
# open a new PowerShell window
cd C:\Users\User\Documents
glados
```

Linux and macOS
```
export GLADOS_HOME=~/.glados      # optional; ~/.glados is the default
cd ~/code/glados
uv sync
uv run pytest
uv run glados

uv tool install --editable ~/code/glados   # the glados command (M6)
uv tool update-shell                       # once, then open a new terminal
glados
```

About the `glados` command:
- `--editable` points the command at this repository: after `git pull`, the command runs the new code right away. If `pyproject.toml` changed (new dependencies), run the install line again with `--reinstall`
- It starts in the folder you open it from, so that folder is the working folder (relative paths and commands start there), even inside the GLaDOS repository or `GLADOS_HOME`; there the start screen notes that GLaDOS's own commands are read-only (your `!commands` run as usual). `uv run glados` from the repository still moves to Documents\GLaDOS as before (M6.2: told apart by which Python runs it)
- Settings come from `GLADOS_HOME` (set it once with `setx` on Windows or in your shell profile), otherwise `~/.glados`
- Remove it with `uv tool uninstall glados`

- Line screen: `uv run glados --plain` (if Korean input misbehaves in the TUI; to keep it, set `[ui] screen = "plain"` in glados.toml)
- One-shot task: `uv run glados "다운로드 폴더에서 계약서 PDF 찾아줘"`
- Commands: `/help`, `/new`, `/resume`, `/memory`, `/index`, `/context`, `/lang`, `/log`, `/sent`, `/servers`, `/runs`, `/undo`, `/quit`, `!command` (e.g. `!git status`, `!dir`, `!cd folder`)
- English screen: add `[ui]` with `language = "en"` to glados.toml in `GLADOS_HOME` (older settings files lack this key, so they default to Korean). Answers always follow the language of the question
- Ctrl+C during a task (also Esc in the TUI) stops only that task. Quit the TUI with Ctrl+Q or `/quit`
- TUI keys: Enter send, ↑↓ sent messages, F1 help, F2 context panel (right), F3 task list (left), PgUp/PgDn scroll. Below 110 columns the task list hides, below 90 the context panel hides (the default Windows Terminal window is 120×30)
- For a denser screen, change it in the terminal: maximize the window, or press `Ctrl+-` in Windows Terminal for smaller text and more cells; GLaDOS re-lays itself out (`Ctrl+0` resets)
- If the moving header symbol is distracting or Korean input flickers, set `motion = false` under `[ui]`
- Starting as admin prints a warning and stops

## What the file hands guarantee

- Search: every drive by default since M7.5 (`search_default = ["*"]` in folders.toml; set folders such as `["~/Downloads", "~/Documents"]` to narrow it). "Everywhere" is every drive (`["*"]`); `["~"]` is the whole home folder. Names such as "다운로드", "~/다운로드", or "문서 폴더" map to the real location (on Windows, wherever OneDrive or another drive moved them; on Korean Linux, `~/다운로드`). Protected locations (keys, system folders) and GLaDOS safety locations are left out of results. Korean names stored in decomposed form (macOS) are found too
- Read: text files (utf-8, legacy Korean cp949, utf-16), pdf, docx, hwp and hwpx (M7.3), and scanned pdf pages and images by OCR (M7.3). 3000 characters at a time, continued with an offset. Excel and PowerPoint come later
- New files: if the name is taken, saved as `name (2).md`. pdf and docx are not created yet
- Edit: text files only, after approval. A copy goes to `GLADOS_HOME/checkpoints` first and `/undo` restores it. Original pdf and docx files are never edited
- Content search (content=true) and inspection (fs.inspect) cover text, pdf, and docx. Files in protected locations are listed by name but their contents are never read. Cloud-only files (OneDrive) are skipped so nothing is downloaded. Past the time limit (40 s for search, 150 s for inspection) the result says how many files were not checked (asking again continues). Inspection shows the start of each file to the model, so if the model runs on another computer (the lab), that text goes there

## Turning on web search (M3)

Which search service (decided 2026-11-12): **Tavily on Windows and macOS, a self-hosted SearXNG on Linux.** A new `GLADOS_HOME` gets that default in glados.toml; the API key (Tavily) or the SearXNG container (Linux) is still up to you, and until then the start screen or the search error says what is missing. Settings files made before this lack the `[web]` section, so web search is off there (reading pages still works); add it as below.

Add this to glados.toml in `GLADOS_HOME`.
```toml
[web]
search = "tavily"       # "tavily" | "brave" | "searxng" | "ollama"
results = 5

[privacy]
names = []              # names that trigger approval before leaving the computer, e.g. ["홍길동", "Gildong Hong"]
```
Put the API key in an environment variable, not in the settings file, then reopen GLaDOS in a new PowerShell window.
```
setx TAVILY_API_KEY "tvly-..."      # Tavily: free key at app.tavily.com (1,000 searches a month, no card)
setx BRAVE_API_KEY "..."            # Brave Search: card required ($5 monthly credit)
setx OLLAMA_API_KEY "..."           # Ollama web search: ollama.com/settings/keys
```
- SearXNG (self-hosted, the Linux default): see "SearXNG on Linux" below. No account or key needed
- The start screen shows something like `웹 검색: Tavily (api.tavily.com)` when it is on, and says so when the key is missing
- To approve every search, add `"*": ask` under `web.search:` in policy.yaml in `GLADOS_HOME`

### Paper search (M6.5)

Works without any setup or key. Ask in your own words, for example:
- `LLM 평가 지표 최신 논문 찾아줘` (newest first)
- `2024년 이후 diffusion 기반 EEG 디코딩 논문 인용 많은 순으로` (since 2024, most cited)
- `그 중 첫 번째 논문 읽고 요약해줘` (GLaDOS reads the paper's page or PDF with `web.fetch`)

Optional keys, set like the Tavily key (not in the settings file):
```
setx OPENALEX_API_KEY "..."           # free OpenAlex account: 10x daily budget
setx SEMANTIC_SCHOLAR_API_KEY "..."   # Semantic Scholar (used when OpenAlex fails): own 1 request/s instead of the shared pool
```

### SearXNG on Linux

SearXNG is a metasearch engine you run yourself: it asks several search engines (Google, Bing, DuckDuckGo, Brave, Wikipedia, arXiv, ...) for you and returns the merged results, with no account or key. The official install is a Docker container. Once:
```
sudo usermod -aG docker $USER            # then log out and back in
mkdir -p ~/searxng/config ~/searxng/data
cat > ~/searxng/config/settings.yml <<EOF
use_default_settings: true
server:
  secret_key: "$(openssl rand -hex 32)"
  limiter: false
search:
  formats:
    - html
    - json
EOF
docker run --name searxng -d --restart unless-stopped \
  -p 127.0.0.1:8888:8080 \
  -v "$HOME/searxng/config:/etc/searxng" \
  -v "$HOME/searxng/data:/var/cache/searxng" \
  docker.io/searxng/searxng:latest
curl -s "http://localhost:8888/search?q=test&format=json" | head -c 300
```
- `json` under `search: formats` lets GLaDOS read results (without it SearXNG answers 403, and GLaDOS says so)
- `127.0.0.1:8888` keeps it to this computer (the docs' example `-p 8888:8080` opens it to the network); `--restart unless-stopped` brings it back after a reboot
- glados.toml: `search = "searxng"` and `searxng_url = "http://localhost:8888"` (the defaults on Linux)
- If it is not running, a search says `SearXNG(http://localhost:8888)가 꺼져 있습니다. 터미널에서 docker start searxng`
- Update: `docker pull docker.io/searxng/searxng:latest`, then `docker rm -f searxng` and the `docker run` line again (settings stay in `~/searxng/config`)

## Setting up servers (M4)

GLaDOS logs in to servers with SSH keys only. It never types passwords or passphrases and never accepts an unknown host key by itself, so plain `ssh <alias>` must work without a password first. GLaDOS then reads the same `~/.ssh/config`, `~/.ssh/known_hosts`, keys, and ssh-agent. Steps 1 and 5 are once per computer; steps 2 to 4 are once per server.

**1. Make a key** (skip if `~/.ssh/id_ed25519` already exists)

Windows 10/11 has the OpenSSH client built in (if `ssh` is missing: Settings → System → Optional features → OpenSSH Client). In PowerShell, or a terminal on macOS and Linux:
```
ssh-keygen -t ed25519
```
Press Enter for the default location (`C:\Users\<you>\.ssh\id_ed25519` on Windows). An empty passphrase is the simplest; with a passphrase, also do step 5.

**2. Name the server in `~/.ssh/config`**

On Windows, `notepad $env:USERPROFILE\.ssh\config` opens it (answer "Yes" to create it; the file has no extension). One `Host` block per server:
```
Host server-a
    HostName gpu-a.example.ac.kr      # the server's address or IP
    User gildong                      # your account on the server
    Port 22
    IdentityFile ~/.ssh/id_ed25519
```
If the server is reached through a gateway, give the gateway its own `Host` block and add `ProxyJump gateway` here.

**3. Put your public key on the server** (the last time you type the server password)

Windows PowerShell:
```
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh server-a "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```
macOS and Linux: `ssh-copy-id server-a`.

The first connection asks whether to trust the server's host key. Check it and answer `yes`; it is saved in `~/.ssh/known_hosts`, where GLaDOS looks for it.

**4. Check**

`ssh server-a` must now log in **without asking for a password** (then `exit`). If it still asks, run `ssh -v server-a` and read the last lines. Common causes: the home folder or `~/.ssh` on the server is writable by others (`chmod go-w ~` on the server), the key was added to a different account than `User`, or the server accepts passwords only (ask its admin).

**5. Keys with a passphrase: ssh-agent**

Windows, once in an **admin** PowerShell:
```
Get-Service ssh-agent | Set-Service -StartupType Automatic
Start-Service ssh-agent
```
Then in a normal PowerShell: `ssh-add $env:USERPROFILE\.ssh\id_ed25519` (Windows keeps it across restarts). macOS and Linux: `ssh-add ~/.ssh/id_ed25519` in the session that starts GLaDOS.

**6. Tell GLaDOS**

Fill in servers.toml in `GLADOS_HOME` and reopen GLaDOS. One `[[server]]` block per server; `alias` is the `Host` name from step 2 and must be different for each server.
```toml
[[server]]
alias = "server-a"                       # the Host name in ~/.ssh/config
gpus = 4
workdir = "/home/<user>/workspace"       # your work folder; remote.exec and relative paths start here
tmux_prefix = "glados-<user>"            # tmux session names for runs
projects = "code"                        # project folders under the work folder (default "code")

[[server]]
alias = "server-b"
gpus = 8
workdir = "/home/<user>/project"
tmux_prefix = "glados-<user>"
```
`/servers` lists them as `연결 안 됨` (not connected) until the first question. The account name used to tell your folders from other people's is `[user] name` in glados.toml, the same on every server.

**When GLaDOS cannot connect**

| GLaDOS says | Fix |
|---|---|
| The host key of server-a is not in known_hosts | Run `ssh server-a` once in a terminal and answer `yes` (step 3) |
| Key login to server-a failed | The key is not on the server or not loaded: steps 3 to 5, then check with step 4 |
| Cannot reach server-a (... a VPN may be needed) | Outside the campus network, connect the VPN first. Otherwise check `HostName` and `Port` |
| server-a is not a configured server | `alias` in servers.toml does not match; reopen GLaDOS after editing servers.toml |

No need to restart GLaDOS after fixing the key: ask again and it connects.

**For runs (M5)**

- The server needs `tmux` (`tmux -V`; if it is missing, ask the server admin). GLaDOS never installs anything there.
- Runs open your login shell first, so whatever `ssh server-a` gives you interactively (conda, modules, PATH) is there. To use a conda environment, put it in the request: `conda activate myenv && python train.py`.
- Put your code in a git repository under the work folder: the commit and uncommitted changes are saved with each run. A folder without git still runs, with a note on the run card.
- Each run leaves `<work folder>/.glados/runs/EXP-NN/` on the server. Delete old ones yourself when you no longer need them.
- Projects are the folders under `<work folder>/code`. If yours live elsewhere, set `projects = "my_projects"` (under the work folder) or an absolute path in the server's block in servers.toml.
- For the metrics table, print the numbers you want to see (e.g. `print(f"epoch {e} train_loss {loss:.4f}")` and `print(f"[dev] epoch {e} acc {acc:.4f}")`). Any `name value`, `name: value`, or `name=value` format works, including tqdm postfixes and dict prints; numbers written only to other files are not read.
- M5.3 adds matplotlib (for the graphs): run `uv sync` after pulling.

## Things to try: M7.5 (every drive and photos)

1. Open GLaDOS and wait: `/index` shows the file list growing (`파일 목록을 적는 중`), then reading, then `사진·스캔: 본 것 N개, 남은 것 M개`. The first pass over every drive can take hours; photos are read only while GLaDOS is idle
2. Right away (before the photos are read): `내 자동차 면허증 사진 찾아줘` → the model can use `fs.inspect` on images (`Is this a photo of a driver's license?`) in Google Drive or Pictures
3. Later, after the photos are read: the same request finds it by content (`자동차운전면허증` in the photo's text) or by meaning
4. A search that finds nothing should no longer repeat the same call; if the model still loops, note it in the sheet
5. `/index` while a task runs pauses photo reading (`작업 중에는 쉽니다`)

## Things to try: M7.3 (Korean documents and OCR)

1. After `git pull`: `uv sync`, and for the `glados` command `uv tool install --editable C:\Users\User\code\glados --reinstall`
2. `문서 폴더에 있는 hwp 파일 하나 읽어서 요약해줘` → the text of the hwp, not "cannot read .hwp yet"
3. A scanned pdf (a photographed or scanned contract): `이 스캔한 계약서 읽어줘 <path>` → pages 1–2 marked `OCR`, about 20 s a page; then `3쪽부터 더 읽어줘`
4. A screenshot or whiteboard photo: `바탕 화면의 screenshot.png에 뭐라고 써 있어?`
5. After OCR of a scanned file in Documents, search for a word from it with content search: it is found from the index

## Things to try: M7.2 (search by meaning)

1. Once: `ollama pull qwen3-embedding:0.6b`, then open GLaDOS. `/index` shows `뜻으로 찾기: 파일 N/M개 준비` growing
2. A request whose words are not in the document: `계약 어기면 돈 물어내는 조항 있는 문서 찾아줘` → under "Close in meaning" the contracts with a penalty clause should appear, with a score
3. If results under "Close in meaning" look unrelated, note the scores in the test sheet (the 0.40 cut-off may need tuning)

## Things to try: M7.1 (content index)

1. Open GLaDOS and wait a few minutes, then `/index`: the number of files, scanned PDFs, and whether it is still reading (newest files first)
2. `다운로드에서 위약금 조항 있는 계약서 찾아줘`: the answer should come back quickly with the page (`p.N`) of the match; the result line says "from the content index"
3. A Korean two-letter word: `문서 폴더에서 내용에 "논문"이 들어간 파일 찾아줘`
4. Edit a text file in Documents and search for the new words right away: it is read on the spot (the index catches up within 10 minutes)
5. Close GLaDOS: the indexing process ends too (Task Manager shows no extra `python` left)

## Things to try: M6 (resume, memory, the glados command)

1. Install the command (above), open a new window in any folder, run `glados` → the header shows that folder as the working folder
2. Ask something (`lr 실험 계획 세워줘`), quit with `/quit`, open `glados` again → the start screen has a `제안 · 이어하기` (continue) card with that request and the number of tasks. Click it → `이어서 합니다: ...`, and `/context` shows the earlier task in memory
3. `/new`, then `/resume` → the list of earlier conversations; `/resume 1` loads the first one
4. In a new conversation, just say `어제 하던 lr 실험 이어서 하자` → the model calls `session.resume`, answers from the summary, and the screen says `지난 대화를 이어 갑니다: ...`
5. `앞으로 실험 결과는 항상 표로 보여줘. 기억해` → the approval box shows the line to save; choose 기억하기. `/memory` lists it with the file path; open `MEMORY.md` in `GLADOS_HOME` to see it. Try `/memory del 1`
6. Ask GLaDOS to remember something you would rather not keep, and choose 기억하지 않기 → nothing is saved and GLaDOS continues without it

## Things to try: M5.1–M5.3 (memory, projects, experiment sessions)

1. `server-a에 무슨 프로젝트 있어?` (what projects are on server-a?) → the list from `code/`
2. `bert-cls 프로젝트 어떻게 실행해?` (how do I run bert-cls?) → an overview: what it does, the entry file, and the main arguments with defaults
3. `bert-cls에서 train.py --batch 32 돌려줘` (run train.py --batch 32 in bert-cls) → the box asks "이 명령어가 맞습니까?" over the run card. Check the full command and the memory line (an estimate from the code with its basis). Choose 아니요 (no) and say what to change, e.g. `--lr 3e-5도 넣어` (add --lr 3e-5) → a new card
4. `메모리는 20GB면 돼` (20GB is enough) → the card says 20GB (as you said); a GPU another person is using but with enough room may be chosen, with a warning on the card
5. While it runs: `EXP-01 어때?` → state, the last rows of the metrics table, best values. Open Documents\GLaDOS\experiments\EXP-01_bert-cls: `summary.md`, `metrics.csv`, `metrics.png`, `output.log`
6. Close GLaDOS for a while, open it again → the folder catches up (and a notice if the run ended meanwhile); `EXP-01 이어서` (continue EXP-01) picks up the session
7. Run the same command again → the memory line now says the peak measured in EXP-01

## Things to try: M5 (runs)

1. `server-a에서 proj 폴더의 python train.py --lr 3e-4 돌려줘` (run train.py in proj on server-a) → the run card appears: EXP-01, the GPU, the command, the commit, the tmux session, and the rules. Usually the first time it says it will start once a GPU is confirmed free (GLaDOS has not watched the GPUs for 5 minutes yet). Approve with 1
2. About 5 minutes later the notice `EXP-01 시작: server-a GPU 1 (5분 기다림)` appears (card, toast, Windows notification). In another terminal, `ssh server-a` then `tmux attach -r -t <session from the card>` shows the live output (read-only; leave with Ctrl+B then D)
3. `/runs` → EXP-01 running, with its tmux session. `EXP-01 어때?` (how is EXP-01?) → `remote.log run=EXP-01` → state, elapsed time, and the last lines
4. NaN check without a real model: `server-a에서 echo "step 1 loss 0.5"; echo "step 2 loss nan"; sleep 600 돌려줘` → within a minute of starting, a "결정 필요" (decision needed) notice about NaN. Then `EXP-02 멈춰` (stop EXP-02) → an approval box → stopped with Ctrl+C (exit 130)
5. Close GLaDOS while a short run is going (`sleep 120; echo done`), wait 2 minutes, open it again → a notice that it finished while GLaDOS was closed
6. If GLaDOS ever tries to start a job with `remote.exec` (`nohup python train.py &`, `tmux new ...`), the approval card says training goes through `remote.run`; deny it. `tmux kill-session` through `remote.exec` is blocked

## Things to try: M4 (servers)

1. `/servers` → the servers from servers.toml, `연결 안 됨`, and a hint
2. `server-a GPU 비었어?` (is a GPU free on server-a?) → `remote.gpu` → per-GPU states and how many you may use now (1 on weekday daytime, 2 otherwise). An idle GPU first shows as checking; ask again after 5 minutes and it shows as free. GPUs used by other people show only as "in use by another user". `/servers` now shows `연결됨` and the last check time
3. `server-a 작업 폴더에 뭐 있어?` (what is in the work folder?) → `remote.log` lists it newest first; then `제일 최근 로그 끝부분 보여줘` (show the end of the latest log) → the last 80 lines
4. `server-a 디스크 얼마나 남았어?` (how much disk is left?) → `remote.exec df -h` runs without approval
5. `server-a에서 /home/다른사람 폴더 보여줘` (show another user's folder) → blocked by the code rule. `server-a에서 sudo nvidia-smi` → blocked. `server-a에서 rm old.log` → approval card every time
6. Turn off the VPN (or use a wrong alias in servers.toml) and ask again → the answer says what to check instead of an SSH traceback

## Things to try: M3.1 (working folder)

1. Run `uv run glados` from the repository folder → the start screen shows `작업 폴더: C:\Users\User\Documents\GLaDOS (…옮겼습니다…)` and the header shows the same working folder
2. `간단한 파이썬 예제 만들어봐` (make a simple Python example) → a .py file in Documents\GLaDOS; running it shows an approval card (`python …` is not on the allowlist) → the output
3. `!cd C:\Users\User\code\glados` → the old note (read-only commands only) appears

## Things to try: M3 (web)

1. `LLM 평가 지표 최신 논문 3개 찾아서 요약해줘` (find and summarize 3 recent papers on LLM evaluation metrics) → plan (search → read pages → summarize) → `web.search` (time_range=year) → `web.fetch` on the paper pages → summary with source URLs. The header chip grows (e.g. `외부 전송 4 · 웹 검색 1`) and the right panel lists the query and URLs under "밖으로 보낸 것"
2. Open the source URLs or compare with `/sent`: are the cited URLs really from the results? (An invented URL leaves a trace in the process panel: `! 답의 출처 주소가 도구 결과에 없어…`)
3. `https://arxiv.org/abs/1706.03762 요약해줘` → `web.fetch` directly, no search; the PDF URL (`…/pdf/1706.03762`) works too
4. `다운로드의 논문 하나 읽고 그 제목으로 후속 연구 찾아줘` (read a paper in Downloads and search for follow-ups by its title) → a title read from a file goes into the query, so an approval card appears ("이 대화에서 읽은 파일의 글"). Denying it sends nothing
5. `내 이메일 ○○@○○.com 로 검색해줘` → approval card (email address). `http://192.168.0.1 읽어줘` → local-address approval card
6. With web search off, `오늘 뉴스 찾아줘` (find today's news) → no invented news; it says web search is not set up

## Things to try: M2.7 (plans and inspection)

1. `다운로드에서 PDF 찾아줘` then `그 중에서 논문 찾아줘` (find PDFs in Downloads, then the papers among them) → the process panel shows a `계획` (plan) first (e.g. names are not enough → ask fs.inspect "Is this an academic paper?" for each PDF → report the yes ones), then `fs.inspect` runs and the papers are listed with reasons. About a minute the first time (67 files, 6 per model call); the same question is instant afterwards
2. While working, elapsed time runs in the card kicker and header, and `↑ … ↓ … 토큰` with the text being written scrolls inside the card; when the answer arrives it disappears and only the answer remains
3. If a plan looks wrong (e.g. searching names only) or a file is misjudged, report the plan and the file names; the fix goes into tool descriptions and instructions, not hard-coded rules
4. `다운로드에서 위약금 나오는 문서 찾아줘` (find documents in Downloads that mention penalties) → does the model pick `content=true`, and does the result say `Contents checked: N of M files`
5. When the answer says something was not found in the contents, were they really read? Check the numbers in the result (scanned PDFs, encrypted files, and hwp are counted separately)
6. For `안녕` (hello), the plan should be one line or absent

## Things to try: M2.6 (TUI)

In Windows Terminal, `uv sync` then `uv run glados`.

1. Start screen: logo (Lissajous symbol and GLaDOS written in dots), greeting, two suggestion cards; clicking a card fills the input. If Braille shows up as boxes (□), it is a font problem; please report it
2. Korean input: while typing `다운로드에서 논문 찾아줘`, does the composing character appear inside the input box at the cursor? Do final consonants and double finals (`읽었`, `값`) stay intact? Is typing the next message fine while working (header dot moving)? If not, describe what happens and try `motion = false` or `uv run glados --plain`
3. The answer goes into a task card (kicker `작업 · 완료 · 12초 · 도구 1번`, request, facts in bold, wit), the process into the right panel, with searched scope and sources there too. The header state changes from `작업 중 · fs.search · 1단계` to `완료 · 12초`, and the task moves from `진행 중` to `이번 대화` in the list
4. After `!cd ..` out of the GLaDOS folder, `uv 캐시 정리해줘` (clean the uv cache) → approval dialog (cobalt border, one-line buttons), header `결정 대기`, cobalt dot in the list. Keys 1, 2, 3, ←→ then Enter, or Esc (deny). Digits and Enter pressed right as the dialog appears are ignored (so typing never turns into an approval)
5. Esc during a long task → `연구원님이 멈췄습니다.` appears shortly (even while the model is writing)
6. `!git status`, `!cd ..` → output box and the working folder in the header
7. Paste a multi-line error message → the input shows `[붙여넣은 글 1 · N줄]` and the whole text is sent
8. `+ 새 대화` (or `/new`) → back to the start screen. `/lang en`, F2, F3, ↑↓, Ctrl+Q

## Things to try: M2.5 (English inside, translated answers)

1. `다운로드 폴더에서 논문 찾아줘` → the model searches in English (e.g. `query=paper`), `학위논문.pdf` matches as `en: thesis, ...`, and the answer comes in Korean. The first search takes a few extra seconds to translate words in file names
2. `찾은 PDF 하나 읽고 요약해줘` → reads a path found earlier and summarizes in Korean
3. `Find the paper in my Downloads` → finds the same file and answers in English (no translation; words come from the cache)
4. Watch for Chinese characters or odd English words (such as superior) in Korean answers

## Things to try: M2.4

1. `Find the paper in my Downloads` → finds `학위논문.pdf` and answers in English
2. `!git status` → runs at once and shows the output. Then `방금 결과 설명해줘` (explain that output) → answers from it
3. `파이썬 버전 알려줘` (what Python version) → GLaDOS runs `python --version` (no approval)
4. A command outside the allowlist, such as `uv 캐시 정리해줘` → approval card
5. Since M3.1, starting in the GLaDOS folder (`C:\Users\User\code\glados`) moves the working folder to Documents\GLaDOS; after `!cd` back into the GLaDOS folder only read-only commands run and the rest are blocked

## Things to try: M2.2 (two lines that failed in M2, plus memory)

1. `다운로드 폴더에서 논문 하나 찾아줘` → searches with `roots=['~/Downloads']` and reports only the found file without reading it
2. `찾은 PDF 하나 읽고 요약해줘` → calls `fs.read` on the path found earlier. If it tries to answer without tools, a dim line "도구로 확인하지 않은 답이라 다시 확인하게 했습니다" appears and it checks again
3. `그 논문 결론만 다시 말해줘` (repeat just the conclusion) → answers from what was read, without reading again, with that file in the sources
4. `/context` → the last 3 tasks are included as [전체] (full)
5. Watch for plain English words (such as superior) in Korean answers. Technical terms (baseline, BLEU-1) may stay in English
6. `Find the paper in my Downloads` → answers in English
7. `/lang en` → screen texts (working, approval card, /context) switch to English; `/lang ko` switches back

## Things to try: M2

1. `다운로드랑 문서 폴더에서 계약서 PDF 찾아줘` (find contract PDFs in Downloads and Documents)
2. `찾은 PDF 하나 읽고 위약금 조항만 정리해줘` (read one and summarize only the penalty clauses)
3. `방금 정리한 걸 문서 폴더에 위약금_정리.md로 저장해줘` (save that summary to Documents as 위약금_정리.md)
4. `위약금_정리.md에서 제목을 "위약금 요약"으로 바꿔줘` (change the title) → approval card → `/undo` restores it
5. `/log` shows the policy decisions

## Models per machine (glados.toml)

| Machine | `model.mode` | Model | Context |
|---|---|---|---|
| Lab Linux (48 GB GPU), MacBook | `normal` | qwen3.5:9b (9b-mlx on Mac) | 32k |
| Home Windows (8 GB GPU) | `low` | qwen3.5:4b | 32k (measured 2026-11-12 with `measure-ctx.ps1`: 100% GPU, 4.1 GB, a 24k-token prompt read in 5.7 s, 62 tokens/s written; it was 16k before) |

## Layout

```
src/glados/
  cli.py              line screen and engine wiring (App), events -> screen lines (event_lines); glados entry point
  tui.py              TUI (Textual, Workbench design); reuses cli.App for the engine, runs tasks in threads
  logo.py             logo: Lissajous symbol (Braille, per-state looks) and wordmark
  config.py           settings (utf-8)
  osinfo.py           OS detection, admin check, real locations of Downloads, Documents, Desktop
  i18n.py             screen texts in Korean and English; the engine passes keys and the screen renders them
  core/llm.py         the only place that calls the model
  core/loop.py        agent loop (one function; glados serve will call it as is)
  core/context.py     conversation memory: fit earlier tasks into the context length
  core/schema.py      output contract
  core/prompts.py     system prompt (prompts/persona.md)
  core/guards.py      answer safety nets (promise-only answers, file and current-information talk); English text
  core/translate.py   English inside: language detection, name words -> English (Glossary, cache), answer translation (Translator)
  safety/policy.py    policy engine check()
  safety/locations.py three kinds of location
  safety/checkpoint.py checkpoints, /undo
  safety/privacy.py   outgoing text check (personal details, file contents, local addresses); code rules
  safety/fair.py      shared-server rules: when a GPU counts as free, how many GPUs you may use now, run time, GPU-hour budget
  hands/fs.py         file hands (same code on every OS)
  hands/docs.py       document contents: pdf and docx text extraction, cache of text and inspection answers (no judging)
  hands/shell.py      command hands: shell.run and the chat's !command (PowerShell on Windows, bash/zsh on macOS and Linux)
  hands/web.py        web hands: web.search (pluggable: Tavily, Brave, SearXNG, Ollama), web.fetch (main-text extraction), sent-outside log
  hands/remote.py     server hands: SSH connection hub (asyncssh, one connection per server), remote.gpu, remote.log, remote.exec, remote.run, remote.stop
  hands/runner.py     server runs: run card, tmux start (run folder, git snapshot), queue for GPUs, stop, watching and notices, experiment sessions
  hands/memest.py     GPU memory estimate: relevant code lines from the server, facts from the model, the formula
  hands/metrics.py    metrics from printed output: name-number pairs, the model's choice, table, graph
  hands/<os>/         per-OS hands (app control in v1)
  memory/log.py       task log, policy decision log
  memory/runs.py      server run records (runs.db): EXP-NN, server, GPUs, session, commit, state
  memory/sessions.py  conversations (sessions.db): saved turns, recent list, finding one by words, the summary for the model
  memory/notes.py     MEMORY.md: notes to remember, add, delete, the part that fits in the system prompt
  hands/session.py    session.resume (load an earlier conversation after the task), memory.note (asks every time)
  notify.py           OS notifications (Windows toast, macOS, Linux notify-send)
  defaults/           default settings written on first run
tests/
  test_scenarios.py   the seven v0 scenarios end to end through the app (fake home, fake web, fake SSH server with real tmux)
  test_sessions.py    conversations and memory
```

In v0 the hands live inside the engine. When an external program (VSCode, etc.) needs them, the same interface gets wrapped as an MCP server.

## Next

v0 is complete. Next: a week of daily use on the real machines (home Windows, lab Linux), real-server checks from the test list, and writing down what got in the way ("설계에 돌려줄 것" in the project documents)
