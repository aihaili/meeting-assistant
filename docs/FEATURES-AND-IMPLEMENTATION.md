# 实时会议助理 · 功能与实现

> **这是什么**：一份把「软件能做什么」和「它到底怎么实现的」放在一起的说明，面向使用者与接手维护的人。
>
> **与其它文档的分工**：
>
> | 文档 | 面向 | 内容 |
> |---|---|---|
> | `README.md` | 使用者 | 能力清单、快速开始、配置、性能、限制 |
> | **本文** | 开发者 / 交接 | 功能 → 实现（文件、类、参数、数据结构）、数据流、设计取舍 |
> | `docs/architecture-mindmap.md/.html` | 概览 | 一张思维导图式的模块总览 |
>
> 文中所有关键结论都标了 `文件:行`，可以直接跳过去核对。

---

## 0. 一句话与边界

**一句话**：一个**全本地、单进程**的实时会议助理——边说边转写、边按声音分人、边把「要求 / 承诺 / 风险」等分类成线索、边检索当前项目的知识库，会后能把纪要变成事件表并导出 Obsidian。

**能力边界（刻意不做的事）**：

- **不联网推理**：ASR / 声纹 / 嵌入 / 分类全部本机跑（`README.md` 核心优势 #1）；唯一可选的远程依赖是 LLM，且缺了它只降级不瘫痪（`scripts/meeting/settings.py:11`）。
- **不做云端转写、不做多说话人开集识别**：声纹是**闭集**方案（会前名单 + 点名绑定），因为"把甲方的话记到我方名下"的代价远高于"未署名"（`scripts/meeting/voiceprints.py:15`）。
- **不做音视频会议接入**：音频只来自本机麦克风（或界面上的 WAV 回放）。
- **不做文档格式解析**：PDF/DOCX/XLSX 解析库未内置，建议先用 LLM 转 Markdown（`README.md` 已知限制）。

---

## 1. 运行形态与入口

### 1.1 四种入口

| 入口 | 用途 | 进程 | 说明 |
|---|---|---|---|
| `launcher\dist\MeetingLauncher\MeetingLauncher.exe` | **日常使用（推荐）** | pywebview 窗口 + 子进程 | 双击即用；无控制台；窗口就绪后才显示 |
| `run_meeting.ps1` | 开发 / 控制台排查 | 控制台 + `python -m meeting.server` | 有端口预检、`-NoLlm` / `-NoAsr` / `-Session` 等参数 |
| `python -m meeting.server --port …`（cwd=`scripts/`） | 最底层、调试单点 | 单进程 | 所有参数的最终形态，见 `--help`；**注意它的 `--port` 默认是 8500**，而启动器与 `run_meeting.ps1` 用 8510——`.js` 探针也按 8510 打 |
| `run_mic_live.ps1` → `mic_live.py` | 只验麦克风 + ASR，不启界面 | 单进程 | 控制台边说话边打字 |

另有一个**早期入口**：`run_asst.ps1` + `scripts/asst/server.py`（极简 RAG 助手，默认 8500 端口）。`meeting.server` 复用了它的 `Assistant`（`scripts/asst/core.py`），两者不要同时起（8500 端口冲突，见 `scripts/meeting/server.py:12`）。

### 1.2 进程模型

```
MeetingLauncher.exe（pywebview / WebView2，无 torch，无控制台）
   │  窗口 hidden 创建 → 后端就绪 → load_url(会议界面) → show()
   └─ 子进程（CREATE_NO_WINDOW，stdout 收进 data/launcher/backend.log）
        venv\Scripts\python.exe -u -m meeting.server --port P --managed
          ├─ LocalMicReceiver   （sounddevice 采集 → AGC → 16kHz 单声道）
          ├─ ASR 后端           （funasr StreamASR 或 FireRedASR，后台线程预加载）
          ├─ 声纹 / 聚类        （CAM++ 嵌入 + SpeakerClusterer + VoiceprintStore）
          ├─ 检索               （RAG 索引 + ONNX 嵌入，启动时预热）
          └─ HTTP :P            （页面 + REST + SSE）
```

要点（都能在 `launcher/app.py` 里对上）：

- **一个进程装完所有东西**（UI + 音频 + 检索）：句尾后 ~0.5–1s 就能同时出文本、线索和检索结果，因为三者共用内存里的会话对象，没有跨进程搬运（`scripts/meeting/server.py:1`）。
- **窗口 hidden 创建，就绪后再显示**：正常 3–6s 内起来（实测），所以第一眼就是会议界面；超过 8s 或后端报错才亮出加载页（`launcher/app.py` `_navigator`）。
- **端口自己找**：默认 8510，被**真服务**占用就顺延（`data/launcher/`… 见 `_port_free` / `_port_answered`）；界面上没有填端口的地方了。
- **无控制台**：exe 以 `--windowed` 打包（`launcher/build_launcher.ps1`），子进程用 `CREATE_NO_WINDOW`。
- **关闭窗口 = 停服务**：识别模型与嵌入模型都在子进程里，`events.closing` 里 `backend.stop()` 直接终止它（`launcher/app.py` `_on_closing`）；实测关窗后进程树清零。
- **重启服务**：界面设置页写 `data/launcher/restart.flag`，启动器看到就同端口重启后端（`launcher/app.py` `_watch_flag` ↔ `scripts/meeting/server.py` `/api/restart`）。

---

## 2. 功能清单（用户视角）

### 2.1 实时转写与发言流

- 对着麦克风说话，**中栏逐句出字**；一句话在一次识别里会反复被改写（修订），行内就地更新而不是新增一行。
- 录音由界面按钮控制（不是"服务一起来就录"）：`/api/mic` 的 `start/stop`，`status()` 里带电平、增益、溢出计数、模型加载状态（`scripts/phone_mic/mic_source.py`）。
- **模型没加载完按钮是禁用的**，并写明"模型加载中/加载失败"，避免"按了没反应"（提交 `d64cf7d`）。
- 可选 `--mic-save` 把采集音频存 WAV；平时边录边落**裸 PCM**（`data/mic-test/mic-<时间戳>-<n>.pcm`），停止时收成同名 WAV——被强杀也不丢音频（`scripts/phone_mic/mic_source.py:141`）。

### 2.2 声纹分人与人工修正

- 每句话带一个**簇号 `spk`**（CAM++ 嵌入 + `SpeakerClusterer` 在线分配 + 定稿重聚）。
- 左栏是参会人名单。点某句 → 「这句是某人说的」= **署名 + 登记声纹**；跨会库记住了，下次自动给建议（"听起来像 林浩然 0.82"），用户只需确认或否掉。
- **手动修正**（提交 `0f0c9b1`）：
  - **改名**：`POST /api/participants {action:"rename_speaker"}` —— 同一 `spk` 的所有段一起改名；
  - **改归属**：`POST /api/segment {action:"reassign"}` —— 把单段挪到另一个说话人。
- 未署名段的 spk 以「匿名-N」呈现；左栏显示"同一声音还有 N 句"，让用户知道点一下会改几句（`spk_counts()`）。

### 2.3 线索板（10 类闭集）

右侧把发言里要紧的东西分类列出：**甲方要求 / 我方承诺 / 时间节点 / 风险提示 / 历史约定 / 条款依据 / 当场决定 / 技术术语 / 人名 / 机构**（`scripts/meeting/session.py:57`）。其中 `requirement / commitment / deadline / risk / decision` 归为 **ACTIONABLE**（"只看要点"过滤与议程"待跟进"用，`session.py:82`）。

- 每条线索**强制带来源**：`seg_id`（哪句）+ `anchor`（原句里的确切子串，用来高亮）+ 可选 `actor/due/refs`；`anchor` 在原句里找不到就丢弃而不是显示一条无法核对的断言（`server.py:436`）。
- 可**钉住 / 改类型 / 删除**；点击出证据面板：原句 + 检索到的依据 + 相关线索（`evidence_for`）。

### 2.4 实时检索与项目 / 公共分库

- 中栏关键词带下划线，悬停出摘要、点击出佐证（`/api/refine`、`/api/clue`）。
- **项目库**（会议纪要、进度，随项目文件夹走：`<项目>/.plaud/rag.db`）+ **公共库**（合同、资质、规范，公司级共享）。分开的理由是**生命周期**：项目库每次开会都在长，公共库很少变，混在一起会让每次重建都重新嵌入整个公共语料（`scripts/meeting/server.py:1294` 附近的注释）。
- 分库隔离是实测过的：`eval_kb_split.py` / `eval_project_isolation.py`（README 记 0% 跨项目污染）。
- 启动器默认挂 settings 里的公共库；索引不存在就新建、有语料就先增量建索引，**嵌入模型启动即预热**。

### 2.5 会议组织

| 功能 | 实现位置 | 要点 |
|---|---|---|
| **议程导入** | `meeting/outline.py` + `/api/agenda/import` | 支持 `.docx`（zip+XML，含表格内段落）/`.md`/`.txt`/`.csv`；编号与项目符号先剥再匹配时间（`一、14:00 开场致辞` 这种最常见格式才能识别）、时间槽支持 `14:00`/`9:30-10:00`/`上午`+钟点/`九点半`；`kind` 从主持人措辞推断（汇报/确认/风险/跟进） |
| **发言计划（真文件）** | `meeting/planfile.py` | `<项目>/发言计划.md`：YAML 前置块 + `## N. 主题` + 一行 HTML 注释存机器字段（`id/kind/done/src/seg/x/y/w/z`）+ 正文 + `- 参考：来源 — 说明［公共库］`；解析宽容（无注释、无编号、`依据：` 都能读）；写原子替换 |
| **便签与标签** | `session.py` `PlanItem` + `ui.html` | 便签可拖动/缩放，坐标写回计划文件；标签（要点/汇报/请确认/风险/跟进）决定色带；`nw=0` 表示"自动宽度" |
| **钉为参考** | `attach_ref` / `detach_ref` | 把检索到的依据钉到计划项上，只存证据不写话术；参考里记 `kb`（公共库的"合同 5.2"与项目库的同名条款是两回事） |
| **资料导入** | `/api/agenda/import` 的 `scope` | 明确选"进项目库 / 公共库 / 只进本次会议"；文件**复制**而不是移动；非 `.md` 只存放不索引并如实报告 |

### 2.6 会后

- **事件表**：纪要 → 事件（类型/内容/期限/责任人），支持 `aggregate`（聚合提问）与 `timeline`（按时间遍历）。
- **Obsidian 导出**：带 `[[wikilink]]` 的 Markdown，实体归一（`scripts/export_obsidian.py`、`export_minutes.py`）——**一次性脚本**，不是界面按钮，也不是 HTTP 接口。
- **会话文件**：一边开一边自动保存到 `data/sessions/session-<时间戳>.json`（或 `--session` 指定），中断后 `--session` 续开。

### 2.7 设置、健康与重启

- 界面「设置」五节：**识别与模型 / 嵌入 / 知识库 / 大模型 / 界面**（`ui.html` `ST_SECTIONS`）。
- 设置项与默认值集中在 `scripts/meeting/settings.py` 的 `SCHEMA`；优先级：命令行 > `config/settings.json` > 环境变量（含 `config/.env`）> 内置默认（`settings.py:20`）。
- **密钥永不回传浏览器**：`api_key` 只报告"是否已设置"，空值提交表示"不改"（`settings.py` `describe()` + `server.py:873`）。
- **需重启才生效的项**由服务端给出（`settings.py:RESTART_KEYS`），界面据此提示，并在托管启动时显示「重启服务」按钮。

### 2.8 无麦克风路径

界面右上角**「试听回放」**：给一个 WAV 路径，走**和麦克风完全同一条**发布链路（`LocalMicReceiver.replay_wav`），编号与时间戳按会话现状平移，所以整页可以在没有麦克风的机器上跑通（`scripts/phone_mic/mic_source.py` `replay_wav`）。

---

## 3. 子系统实现

### 3.1 音频采集与预处理

**文件**：`scripts/phone_mic/mic_source.py`（`LocalMicReceiver` / `AutoGain`）、`scripts/phone_mic/audio.py`（采样率与格式换算）

| 事项 | 实现 | 理由（注释里写的） |
|---|---|---|
| 采集 | `sounddevice.InputStream`，优先直接以 **16000 Hz 单声道 float32** 打开，设备不支持才用原生率再重采样 | 让系统做重采样比逐块自己重采样干净：逐块重采样会在块边界留滤波器瞬态（`mic_source.py:309`） |
| 回调 | 回调里**只拷贝**到队列，工作线程再处理；攒够 0.5s 才处理 | 回调里做识别会跟崩 → PortAudio `input overflow` → 音频真的丢（`mic_source.py:12`） |
| 自动增益 | `AutoGain`：语音包络 `env` 上升 α=0.35 / 下降 α=0.06；`want=clip(TARGET_DBFS/env, 1, MAX_GAIN)`（只放大不衰减）；增益一阶平滑 `0.85g+0.15want`；噪声门（`rms < -50 dBFS` 或 `rms < env*0.18` 时 1:1）；峰值包络 `max(pk, env*0.995)` 并压到 `peak_target=0.9`；溢出才 `tanh` | `TARGET_DBFS=-20`、`MAX_GAIN=15`（原 40x 实测会跑到 30–38x，等于把底噪放大 30 倍喂 VAD/ASR，识别在窗口间不稳 → 同句两次转写匹配不上 → 碎片化）；`ABS_FLOOR=-50 dBFS`（只有相对门限不够：没人说话时"语音包络"就是噪声本身，实测 277 秒纯噪声被泵成一堵 -28 dBFS 声墙）；峰值感知是因为实测采到过 1.000（已削顶，不可逆失真）（`mic_source.py:37-98`） |
| 落盘 | 边录边写**裸 PCM**（`save_dir/mic-<MMDD-HHMMSS>-<n>.pcm`，int16/16k 单声道，每块 flush），`stop()` 读回整份收成同名 `.wav`，并填 `last_take={wav,seconds,dbfs,peak,segments}` | WAV 头要收尾回填长度，被强杀就留一个长度 0 的文件；裸 PCM 随时被杀都完整（`mic_source.py:139`）。文件名带时间戳是因为重启后序号从头开始会覆盖上一次录音（`:185`） |
| 编号 | 对外 `idx` 从会话"最大 idx + 1"开始（`prepare(base, elapsed)`），内部 ASR 行号稳定映射 | 否则新录的话会逐条覆盖会话已有行（实测 14.5s 讲成 29 段全写到旧行上，`mic_source.py:151`） |
| 时间戳 | 对外时间 = 录音开始时的会议时刻 + ASR 相对秒数（`time_offset`） | 不平移新发言会被插到时间轴最前面，用户盯底部永远看不到（`mic_source.py:157`） |
| 状态 | `status()` → `session{source,opened_at,chunks,overflows}`、`asr{loaded,loading,load_ms,spk,spk_error,gain,peak_in,level_dbfs}`、`resampler`、`tick_error` | 界面据此禁用/启用录音按钮、显示电平与增益；`peak_in` 让用户自己看出削顶（`mic_source.py:388`） |
| 回放 | `replay_wav(path, speed, chunk_s=0.5)`：读 WAV → 分块 `push` → `tick` → `_publish`，按 speed 控速，返回 `{seconds, wall_s, segments}` | 与采集共用发布路径，检索/线索/声纹/界面全都不用改。**限制**：不置 `running`，所以两次回放可并发；自己不 reset ASR、不重编号——必须由调用方先 `prepare(base, tail)`（`mic_source.py:420`） |

音频格式规约（`scripts/phone_mic/audio.py`）：**一律 16 kHz 单声道 float32**；重采样用 `scipy.signal.resample_poly`（抗混叠），没用 `audioop` 是因为它丢样点、48k→16k 会把 8k 以上折回语音带（`audio.py:10`）。

### 3.2 识别后端

两个可切换后端，接口一致（`load/push/tick/flush/reset`），由 `settings.json` 的 `asr.engine` 决定（`server.py:1346` 附近）：

| | **StreamASR**（默认 `funasr`） | **FireRedASR**（`firered`） |
|---|---|---|
| 文件 | `scripts/phone_mic/stream_asr.py` | `scripts/phone_mic/firered_asr.py` |
| 组成 | `paraformer-zh-streaming`（流式）+ `ct-punc`（标点）+ `fsmn-vad`（端点）+ CAM++（声纹）+ `paraformer-zh`（离线二遍） | FireRedASR2-AED + FireRedVAD + FireRedPunc（`E:\WhisperX\FireRedASR2S`，约 5GB，建议 GPU） |
| 粒度 | `CHUNK_MS=600` / `CHUNK=9600` 采样（官方实时粒度） | 由其 VAD 决定 |
| 二遍 | `two_pass=True`：流式先出，定稿用离线模型重听一遍 | — |
| 实测 | RTF≈0.027（CPU 也可）、首字丢失/行修订问题修过多次 | CER 3.05% vs FunASR 4.16%（`requirements.txt:36`） |
| 加载 | 后台线程预热（`server.py` `_preload`），实测 FireRedASR2S 约 20.7s；加载耗时写进 `receiver.load_ms` 供界面显示 | 同 |

行的状态机（`FeedSentence`，`scripts/phone_mic/streaming.py:626`）：`idx`（稳定行号）、`text`、`start/end`、`first_seen_at`、`committed_at`、`spk`、`revisions`（被改写次数）、`open`（还在保护带内、可被追加）、`emb/emb_start/emb_end/emb_weak`（声纹向量及其可信度）。`open=False` 的行不再吸收后续内容——否则一个人的回答会被粘到上一个人的定稿句上（`streaming.py:629`）。

> 现状提醒：`StreamingASR`（同文件）是**手机当麦克风**那条通道的遗留实现，2026-09-23 已随手机通道一起下线；现在只有离线评估/开发工具（`mic_live.py`、`mic_analyze.py`、`compare_engines.py`、`transcribe_capture.py`）还在用它。

### 3.3 说话人聚类 `SpeakerClusterer`

**文件**：`scripts/phone_mic/streaming.py:215`（被 `stream_asr.py`、`firered_asr.py` 共用）

- **在线**：新行的嵌入与各簇质心比余弦，≥ `merge_cos`（0.55）并入、< `new_cos`（0.35）**确定**开新簇，中间地带按最近簇（`SPK_MERGE_COS=0.55` / `SPK_NEW_COS=0.35`，`streaming.py:69`）。
- **定稿后重聚（refit）**：攒够 `refit_min=3` 行就重新聚类（层次/凝聚），下界 `split_floor_cos=0.80`（再相似的也不切，防碎）、上界 `merge_cap_cos=0.55`（再不像的也不并，防吞人），并用**最大间隙切割**数据驱动地定阈值（`min_gap=0.10` 才算真实说话人边界；`refit_thr_cos=0.45` 是行数太少时的兜底）。标签保持稳定：大簇优先继承旧标签，拆出来的小簇才拿新号（`streaming.py:214`）。
- **嵌入从哪来**：取该行实际音频（`emb_start/emb_end`，两侧各留 `EMB_PAD_S=0.35`），窗口默认 `EMB_WIN_S=2.0`；可用样本 < `MIN_EMB_S=1.2` 秒就标 `emb_weak`，不拿它开新簇（`streaming.py:74-80`, `streaming.py:142`）。
- **单位教训**：老版本把 `online_thr` 说成余弦、`refit_cap` 说成距离，两个数混用，结果 `refit_cap=0.70` 实际是"余弦 0.30 就合并"——比不同人的实测上限 0.37 还低，于是不断吞人（`streaming.py:218`）。现在**全部统一为余弦**，并按语义命名（merge/new/split_floor/merge_cap）。
- 诊断：`_spk.log` 里逐条记 `assign`（行 → 簇、相似度、new/merge）与 `refit`（多少行 → 多少簇 → 标签），`data/e2e/test3.py` / `test3_phone.py` 就是靠它出报告的。

### 3.4 声纹库 `VoiceprintStore`

**文件**：`scripts/meeting/voiceprints.py`（存储：`data/voiceprints.json`，挂项目时 `<项目>/.plaud/voiceprints.json`）

- 为什么必须有：`spk` 簇号只在一次会议内有效，跨会认人只能存**嵌入向量**（`voiceprints.py:3`）。
- 匹配：余弦；`threshold=0.60`、`margin=0.10`（还要比第二名高 0.10 才算确定）。阈值是**用真实会议录音校准**的：同一人相邻两段 0.72/0.81，不同人 0.08/0.12/0.15/0.22（`voiceprints.py:34`）。
- 每人最多留 `MAX_EMB_PER_PERSON=8` 条向量，取最大值匹配（同一个人换麦克风/感冒/远近都不同，只存质心会越用越偏）；太像的（cos>0.92）不重复登记（`voiceprints.py:126`）。
- **不静默改名**：过了阈值只给建议，要人点一下。库损坏 → 从空库开始并报出来（`load_error`），不能让"库读不了"变成"软件打不开"。
- 写入是原子的（临时文件 + `os.replace`）。

### 3.5 会议服务 `MeetingService`

**文件**：`scripts/meeting/server.py:69`

**两级分析**（`on_segment` + `_settle_loop` / `_settle_pass`，`server.py:299`、`server.py:352`）：

1. **即时（毫秒级、确定性）**：先**无条件**记下这句话 → 去重（同一 `idx` 文本没变就不重复分析，但**说话人变了要补一次广播**）→ 广播给页面 → 检索（`assistant.process`）+ **规则通道**分类（`classify_rules`）。这一趟就产出了"时间节点/义务/机构"这类最要紧的线索。
2. **定稿后（LLM 一次）**：行在 `settle_after_s=5.0` 秒内没再被改写，就认为定稿；`_settle_pass` 每轮最多处理 `llm_max_per_pass=4` 行（不设上限会在静默期后一次打出一串 LLM 调用，本地模型直接卡几十秒）。LLM 通道结果与规则通道 `merge()`。

理由写在注释里：流式识别会反复重发同一行（实测开场"好，"被改了 21 次），每次修订都调 LLM 的话 22 句话要 50 次调用（`server.py:306`）。

其它机制：

- **声纹建议只建议**：`add_segment` 里只有 ①有 `spk` ②该 `spk` 还没名字 ③带嵌入 ④库非空 ⑤用户没否定过 时才问库，且 `score ≥ 0.55` 才挂建议（`session.py:405`）。**绝不静默改名**——把甲方的话记到我方名下比"未署名"糟得多。
- **登记声纹的取样规则**：同一声纹里挑**时长最长**的段，最多 8 条、累计到 24 秒即停，够 3 秒才认为"这段声音值得入库"；不足 3 秒会在 `voice_note` 里写"语音偏少，建议再多说几句"（`session.py:742`）。旧写法取 `vecs[:3]` 被明确禁止——生产链路的段长中位数只有 1.1 秒（`session.py:746`）。
- **推送 + 轮询并存**：`GET /api/stream` 是 SSE 长连接（每订阅者一个 `Queue(maxsize=200)`，满则丢这一条不阻塞采集；连接时先发 `event: hello`，15s 无事件发 `: ping` 心跳），解决"流式识别原地改行、而轮询的 `since` 只送新行"的问题（`server.py:617`、`server.py:91`）。轮询（`/api/state?since=`）继续负责全量结构（参会人/线索/议程/计划/服务状态）；页面 `applyStreamSeg` 收到推送就原地改那一行（`ui.html:1792`）。
- **`history_rev`**：任何历史性变更（改归属、改人名、删发言、钉线索、清空…）+1；前端发现它变了就整份重拉，而不是指望增量轮询能发现"已经画出来的行被改了"（`session.py:265`、`ui.html:1414`）。**不落盘**。
- **热词刷新**：`on_segment` 每句之后调一次 `_refresh_hotwords()`（目标集没变时内部按指纹跳过），把刚出现的人名/术语纳入下一句的纠错范围（`server.py:348`、`hotwords.py:149`）；新增/改参会人时也刷（`server.py:751`）。
- **凭据安全**：`Settings.describe()` 只报告密钥是否已设置；页面永不回传明文（`settings.py:261`）。
- **`/api/restart`**：托管启动时写重启标记，非托管时明确 400 让你手动重启（`server.py:903`）。

### 3.6 HTTP API 全表

共 **23 条路径**（`grep 'u.path == "/api/' scripts/meeting/server.py` 可列全）；JSON 请求体解析失败时宽容为 `{}`。

**GET**（`do_GET`，`server.py:522`）

| 路径 | 行 | 请求 | 响应要点 |
|---|---|---|---|
| `/`、`/index.html` | 527 | — | `ui.html` 原字节（`no-store`）；文件缺失 → 500 JSON |
| `/api/state?since=N` | 541 | `since` 缺省 −1（按 0） | `to_dict(since_seg)` 快照 + `service`（见下） |
| `/api/stats` | 548 | — | `index`(assistant.stats)、`load_ms`、`warm_ms`、`asr_root`、`asr_available`、`ui`、`clue_types`、`actionable` |
| `/api/transcript` | 562 | — | 纯文本 `[  start] 说话人：文本` |
| `/api/refine?term=` | 570 | `term` 必填 | `{term, results[], elapsed_ms}`；**文档里的 `seg=` 参数代码从不读** |
| `/api/clue?id=` | 582 | `id` | `evidence_for()`：`clue/segment/refs/related(≤6)/meta`；未找到 404 |
| `/api/search?q=` | 591 | `q` 必填 | `{q, corpus, segments(≤20), segment_hits}` |
| `/api/audio` | 603 | — | 最近一次落盘 WAV 字节；无 → 404 |
| `/api/stream` | 617 | — | SSE：先发 `event: hello`；每订阅者一个 `Queue(200)`；15s 无事件发 `: ping`；断开即退订 |
| `/api/mic` | 652 | — | `{available, running, take, seconds}` |
| `/api/voiceprints` | 661 | — | `{voiceprints: store.stats(), denied: […]}`（**前端未调用**，纯诊断） |
| `/api/plan?text=1` | 667 | `text=1` 时附文件原文 | `{path, exists, in_project, items, text?}` |
| `/api/settings` | 685 | — | `{settings: describe(), health: check(), restart_keys, managed, corpora}` |
| 其它 | 702 | — | 404 JSON |

**POST**（`do_POST`，`server.py:706`）

| 路径 | 行 | action / 关键字段 | 响应要点 |
|---|---|---|---|
| `/api/participants` | 711 | 缺省 `add`；`remove` / `deny_suggestion` / `bind_voice` / `unbind_voice` / `rename_speaker` / `update` | add 空名 400；`bind_voice` → `{ok, spk, participant, changed, enrolled, voiceprints, bound_segments}`；add/update 成功后刷新热词 |
| `/api/reset` | 764 | `keep_people` | 清空 segments/clues/dropped，`history_rev+1`（**前端未调用**） |
| `/api/mic` | 784 | `action=start\|stop` | 无麦克风 400；start 前先 `prepare(base=max(idx)+1, tail=max(end))` |
| `/api/segment` | 813 | `action=remove\|reassign`；否则按 `text/start/end/spk/emb` 手工加段 | 加段后广播 + `_analyse(llm=False)` |
| `/api/clue` | 846 | `action=remove` 或 `id/kind/text/pinned/actor/due/confidence` | `{clue}`；未找到 404 |
| `/api/agenda` | 861 | `items`（必填） | **整表替换**议程栏 `{agenda:[…]}` |
| `/api/settings` | 869 | `set` 必须非空对象 | 逐项写入；**secret 传空串 = 不改**；有变更则 `save()+apply_to_env()`；回 `{changed, errors, settings, health, restart_keys, managed}` |
| `/api/restart` | 903 | — | 非 `--managed` → 400；否则写 `data/launcher/restart.flag` → `{ok, flag}` |
| `/api/agenda/import` | 923 | `path`，`scope∈{project,global,session}`（缺省 project） | 解析失败 404/422；成功 `set_agenda(items,"agenda")` + `imported_from`；`scope≠session` 且挂了项目才 `index_imported` |
| `/api/prep` | 981 | `add` / `attach_ref` / `detach_ref` / `update` / `remove` / `reorder` | 七条成功路径都经同一个 `done()` 闭包 → **顺手重写 `发言计划.md`** 并回 `plan_file` |
| `/api/plan` | 1074 | 缺省 `reload`；或 `write` | 从文件重建 / 写回文件 |
| `/api/prep/layout` | 1088 | `action=reset`（可选 `ids`）或 `items[{id,nx,ny,nw,nh,nz}]` | 便签布局（拖拽结束才调，避免每次 mousemove 重写会话） |
| `/api/agenda/mark` | 1114 | `id`、`done` | 单独端点：勾一个框不该整表回传（快照可能已过期） |
| `/api/title` | 1125 | `title`（空串不改） | `{title}` |
| `/api/audio/test` | 1133 | `path`、`speed` | 文件不存在/无接收器/正在录音 → 400；否则后台线程 `prepare(base,tail)` + `replay_wav` |
| 其它 | 1166 | — | 404 JSON |

`_service_state()`（`server.py:1170`）字段：`audio`（接收器 `status()["session"]`）、`resampler`、`asr`、`last_error`、`classify_llm`、`managed`、`spk_counts`、`voice_names`、`corpora{project_name, project_dir, project_db, global_db, global_kb}`；取状态异常时加 `audio_error`。

**注意（与 README 的出入）**：没有 `/api/import` 与 `/api/export`。资料导入走 `/api/agenda/import` 的 `scope`（复制到 `<项目>/导入/` 或公共语料目录并增量索引，只有 `.md` 会被切块索引）；**Obsidian 导出是独立脚本**（`scripts/export_obsidian.py` / `export_minutes.py`），不是界面/接口功能。

### 3.7 会话模型与持久化

**文件**：`scripts/meeting/session.py`（`MeetingSession` / `Participant` / `Clue` / `PlanItem`）

字段（`session.py:92`、`session.py:115`、`session.py:142`、`session.py:245`）：

| 结构 | 关键字段 | 说明 |
|---|---|---|
| `segments`（dict 列表） | `id, idx, text, start, end, speaker, spk, suggest, keywords[], results[], revised, at`；运行时还有 `_analysed_text/_changed_at/_llm_done/_emb` | 发言流；`_` 前缀是内部状态，广播时剔除 |
| `clues` | `id, kind, text, seg_id, t, anchor, confidence, actor, due, refs[], pinned` | 线索 |
| `participants` | `id, name, org, role, voice_id, voice_note, speaking, color` | 参会人（`voice_id` 非空 = 已绑定声纹） |
| `agenda` / `prep` | `PlanItem`：`topic, kind, detail, done, seg_ids, source, speaker, slot, order, refs[], nx/ny/nw/nh/nz` | 议程与发言计划（同一结构，`plan_list` 区分）；`nx..nz` 是便签布局 |
| `voice_names` | `spk → 显示名` | 改名的落点 |
| `history_rev` | int | 任何历史性变更 +1，前端据此整份重拉 |
| `stats` | `retrieval_ms / clue_calls / clue_ms / llm_calls` | 性能自述 |
| `dropped` | 字符串列表 | 分析失败等异常，不中断主链路 |

持久化与细节：

- `_save()` 每次变更即整份重写（临时文件 + `os.replace`），所以被强杀最多丢最后一两秒；写失败只往 `dropped` 里记一行，绝不抛出（`session.py:1109`）。磁盘文件比 API 大：`_emb` 与 `_analysed_*` 原样落盘，`to_dict` 才剥掉（`session.py:1089`）。
- **同一句的判定**：`idx ≥ 0` 按 `idx`；手工路径（`idx < 0`）按"文本相同且 `start` 相差 < 0.05"（否则演示脚本跑两次会把整份转写翻倍，`session.py:376`）。
- `load()` 的老格式迁移：`agenda`+`prep` 合并、缺 `list` 的项归 `prep`、`nw == 232.0` 一律当"自动宽度"归零（`session.py:1150`）。
- **不落盘的两处**：`history_rev`（重启后从 0 计）与 `denied_spk`（用户点过"不是他"的否定在重启后失效，会重新弹同一个建议）。

### 3.8 检索栈（RAG）

**文件**：`scripts/rag/rag_core.py`（索引与检索，757 行）、`chunking.py`、`embedder.py`、`ingest.py`、`sync.py`、`multi_index.py`、`docread.py`、`events.py` + `event_store.py` + `extract.py`（事件表）、`evaluate.py`、`selftest.py`、`cli.py`

**索引结构**（单个 SQLite 文件，默认 `<kb_dir>/rag/rag.db`；`SCHEMA_VERSION=2`，`rag_core.py:48`）

| 对象 | 类型 | 关键列 | 用途 |
|---|---|---|---|
| `meta` | 表 | `key, value` | `schema_version / dim / tokenizer / embedder`（embedder 形如 `bge-small-zh:Xenova/bge-small-zh-v1.5@512`，**首写者生效**） |
| `chunks` | 表 | `id, source, path, rel, heading, text, mtime, content_hash` | 切片正文与来源三件套 |
| `files` | 表 | `path(pk), source, mtime, size, content_hash, n_chunks, indexed_at` | 按文件记账 → 增量判据 |
| `chunks_fts` | FTS5 虚拟表 | 单列 `text`（`tokenize='unicode61'`），`rowid = chunks.id` | 词法臂；**手工同步**（写的是 jieba 预分词后的副本，原文在 `chunks.text`） |
| `chunk_vec` | sqlite-vec `vec0` | `embedding float[dim]` | 向量臂，`rowid = chunks.id` |

**中文分词**：索引与查询两侧都先过 jieba 再喂 FTS5；查询用 **OR** 而不是 AND——jieba 对查询与文档的切分可能不一致（查询切成 `没有/业务/结论`、文档是 `无业务结论`），AND 会得 0 分，多出来的召回交给 RRF + 向量臂收拾（`rag_core.py:104`）。**不做 trigram**：trigram 对 2 字词结构性失明（孙总/张伟/李强全 0 命中），实测 jieba **15/16** vs trigram **12/16**（`rag_core.py:16`）。tokenizer 是表定义的一部分，`if not exists` 会静默留下旧 tokenizer，所以从 `sqlite_master.sql` 读回实际 tokenizer，不一致就重建 FTS 并强制重灌（`rag_core.py:245`）。

**向量与嵌入**

- 距离：`vec0` 用默认度量（平方 L2），单位向量下换算 `cos = 1 - d/2` 再展示（直接报 `1-d` 会把真实命中显示成负数，`rag_core.py:536`）。
- 后端：`bge-small-zh-v1.5`（512 维 / INT8 23.9 MB / 中文，默认）或 `bge-m3`（1024 维 / 568.5 MB / 多语言）；指令前缀只加在**查询**侧、文档裸嵌（省掉可测地掉召回）；pooling 硬编码取 CLS（`hidden[:,0]`）；随后 L2 归一化，维数不符时按 Matryoshka 截断（同模型内精确，实测 cos=1.000000）。
- 产物解析顺序：`local_dir` → HF 缓存快照 → `hf_hub_download`；精度回退 int8 → fp16 → fp32。**自己扫缓存**是因为 `snapshot_download(local_files_only=True)` 会因缺元数据报 `IncompleteSnapshotError`（权重其实齐全）且本机 huggingface.co 被 DNS 劫持；且必须定向 glob `onnx/*.onnx`（`rglob` 会走 `blobs/` 里其它模型的多 GB 权重，让 24 MB 模型的"解析"花 8.5 秒）。
- 懒加载 + 进程级缓存（键 `backend:dim`，每后端只加载一次）；ONNX 会话 `ORT_ENABLE_ALL`、`intra_op_num_threads=cpu//2`、CPU provider。
- **不用 torch/transformers**：`import transformers` 单项约 4 秒（3.9 秒花在 `importlib.metadata.packages_distributions()` 枚举已安装发行包）；`tokenizers` 读 `tokenizer.json` 约 10 ms。实测冷启动 **14.5s → 0.96s**、热查询 **65ms → 4.4ms**、GPU **5.4GB → 0**（`requirements.txt:23`）。
- **不用 GGUF**：嵌入模型不是生成式 decoder，llama.cpp 只能跑 decoder 图。**放弃 WeMM-Embedding-2B**：查询分离度 +0.0401 vs bge-small-zh 的 +0.0852、5.09 GB 且无量化发布、图片直嵌能力无用（文档上游已转 Markdown）。
- 量化经过实测：INT8 vs FP32 逐文档余弦 ≈ 0.96，**8/8 探针 top-1 排名完全一致**（向量有漂移、排序不变——排序才是检索依赖的东西）。
- `meta.embedder` 与当前后端不一致（`backend_mismatch`）时**只停用向量臂**并明确提示重建，词法臂照常工作；`sync_folder` 遇到不一致直接全量重置（`sync.py:108`）。

**切片（`chunking.py`）**：`TARGET=450 / MIN=120 / OVERLAP=50`，打包预算 `budget = target - overlap = 400`（先装 400 自己的内容，再把上一块尾巴 50 字接上，最终仍 ≤ 450）。先删生成器横幅类 HTML 注释；块解析把**围栏代码块**与**表格**当原子块（"拆开的表格比稍微超长的块更糟"）；噪声行（`---`/`***`、≤24 字的纯斜体脚注）丢弃；按 `。！？!?；;` 断句后贪心打包；标题层级用栈维护，`heading` 是 `" > "` 连接；小片段在 heading 路径相同时并入前一块。`Chunk(text, heading, start, end)` 的 `start/end` 是近似游标（下游未用）；来源 `source/path/rel` 由 `index_file` 赋值，`rel` 相对 **source 根**而非 KB 根。

**入库与增量**：

| | `ingest.build` | `sync.sync_folder` |
|---|---|---|
| 定位 | 把任意 Markdown 文件夹建成**独立**索引（在真实语料上量检索质量，不动生产库） | 把已有索引**增量**更新到与文件夹一致（启动路径用它） |
| 默认 | `rebuild=True` 全量重建 | 增量 + `prune=True` |
| 判据 | 无（只按 `min_chars=40` 跳过） | `files` 里 mtime 相同且 size 相等 → `unchanged`；hash 相同 → `touched`（只刷记账）；真变才重切重嵌 |
| 删除 | 不管 | 只删属于本 root 的路径 + 孤儿行清扫 |
| 跳过规则 | **不使用** | `SKIP_DIRS`（`.git/.obsidian/node_modules/venv/rag/.plaud` 等）、`SKIP_NAMES`（`session.json`/`transcript.txt`/`segments.json`）、`SKIP_PATTERNS`（`-transcript.md`、`-segments.json`、音频） |

跳过规则背后的判断标准是**产物种类而不是作者**：`*-transcript.md`/session JSON/音频是"对已有语音的逐字复述"，重导入只让索引增长；而 `会议纪要/` 是决策与承诺的蒸馏记录，**故意不排除**（`sync.py:16`，并有 `test_rag_scope.py` 端到端守着这条——因为旧注释与实现曾经不一致）。实测启动同步：3 个文件 1.07s、无变化 0.10s。

**检索（`rag_core.search`）**：签名 `search(query, top_k=8, source=None, mode="hybrid", candidates=50)`；`RRF_K=60`。词法臂 `bm25()`（负值取反）、向量臂 `knn(k=candidates)`，融合时对每个文件取**两条臂里的最佳排名**，`file_score[path] += 1/(RRF_K + rank + 1)`；最后按本次最高分归一到 0..1——**于是每个索引自己的 top-1 恒为 1.0**。返回 `title/label/path/rel/score/heading/snippets(≤3)/chunks(≤3)`；`source` 过滤在成组阶段（RRF 与 `top_k` 之后）做，**不补位**，且过滤值是 **label**（`wiki`/`会议纪要`/`概念`）而不是目录名。

**多库（`multi_index.py`）**：`specs=[{db,kb,name}]`，坏索引记 `errors` 并跳过；单库直接透传该库分数，多库一律按**倒数排名融合**（rank 1-based，`1/(60+rank)`），去重键 `name::title::label`，命中带 `kb` 并强制标 `score_kind="rrf"`（"这是融合权重不是相似度"）。`mixed_backends`/`dim_conflict` 只进 `stats()`，不改变行为；`label_names` 参数是死参数。

为什么必须按排名而不是按分数：`RagIndex.search` 按本库最高分归一化，于是每库 top-1 都恰好 1.0，按分数合并会让所有库的 top-1 并列并退化为插入顺序——实测 **8 题只有 2 题正确文档排第一、7 题与无关文档并列**。项目库加权先验试过又删掉（`PRIOR=0.0006` ≈ 一次 rank-1 票的 3.7%）：能修并列，但**4 道只能由公共库回答的题里 0 道还能给出公共文档**。结论：两类语料回答的是不同类问题，任何单一标量跨语料排序都会埋掉一边；所以融合保持中性、结果带 `kb`、由消费方分组呈现（`search_by_index`）。

**分库 vs 混库**：`data/kb-split-eval.json` 实测 分库 **3/6** = 混库 **3/6**（检索质量无差别），所以分库买的不是准确率而是**生命周期**（项目库每次开会都长，混库会让每次重建都重嵌稳定的公共语料）与项目文件夹自包含。另外实测 `source` 过滤**不能替代分库**：混库只有单一 source 时 `filter_own` **0/18**（`data/source-filter-eval.json`），而独立库 18/18；混库不带项目名的通用问题有 **4/18（22%）** 命中别的项目（`data/project-isolation-eval.json`）。

### 3.9 大模型层与关键词 / 线索分类

**文件**：`scripts/llm_client.py`（共享 LLM 层）、`scripts/asst/keywords.py`（抽词）、`scripts/meeting/classify.py`（线索分类）、`scripts/asst/core.py`（助手核心）

**LLM 层（`llm_client.llm_chat`）**

- provider：`LLM_PROVIDER=openai` → OpenAI 兼容 `/chat/completions`；否则回退 **Claude CLI**（`claude --print --input-format text --max-turns 5 --model …`）。环境变量：`OPENAI_BASE_URL / OPENAI_API_KEY（默认 sk-no-key，llama.cpp 不校验）/ LLM_MODEL / LLM_MAX_TOKENS（默认 8192）/ LLM_TIMEOUT（默认 1800）/ NO_THINK（默认 1）/ CLAUDE_BIN`。配置来源与优先级见 §2.7（settings 会把这些推进环境变量，所以界面改了立即生效）。
- **重试**：只对 HTTP ≥500 与 `RequestException` 重试 3 次，退避 `1.5*(i+1)` 秒；4xx 直接抛。理由是 llama.cpp 在"这次 prompt 远大于上一次"时间歇性 500，而进程一直健康，单发会让整个流水线步骤无输出。
- **`no_think` 双保险**：正文追加 `\n/no_think` **并**加 `chat_template_kwargs={"enable_thinking": False}`（文本式标记是 Qwen3 时代的产物，新模板只是"有时"遵守——实测仍会丢掉 JSON 数组外层 `[]` 并把 token 预算烧在 `reasoning_content` 上）；若返回空且 `NO_THINK=1`，临时关掉它再带文本标记重试一次。
- **reasoning 清洗** `strip_reasoning()`：移除 `<think>…</think>`、**独占一行的截断标签**、残余标签，并把 3 个以上连续换行压成 2 个。理由是本地模型即使 `enable_thinking=False` 也会间歇性漏 `</think>`，曾作为整页正文泄漏进生成的文件。
- **失败降级**：任何异常都 `return ""`（不往上抛）——关键词回退 jieba、事件抽取当空、线索分类退回规则通道。
- 环境变量读取：`load_dotenv(Path.home()/".hermes"/".env")`（调用方各自加载）。

**关键词抽取（`asst/keywords.py`）**：模块刻意不依赖索引（便于单独评估）。提示词要求从发言里抽 **2–4 个**最能定位出处/代表主题的热点（人名、机构、文件、流程、专业术语、关键动作），每个 **2–8 字**且**必须是原文里出现过的连续片段**，不要日期/数量/泛化词，**宁少勿滥**，只输出 JSON 数组（提示词刻意简短：本地模型在长篇散文式提示上会逐字回抄示例占位符）。落地前四道过滤：①**原文锚定**（`k in segment`，模型编造的词直接丢——否则只会产生空热区）②包含去重（长词优先）③停用词（我们/他们/什么/这个… 16 项）④上限 `max_keywords=6`。LLM 不可用/抛错/解析为空 → jieba 回退（2–8 字、过滤停用词、按词频取前 6）。JSON 解析容错：先抓 `[...]` 再 `json.loads`，失败退化为正则抓引号内串。评估脚本 `asst/eval_keywords.py` + `keyword_eval.json`（11 段真实发言、每段 1–3 个 gold 热点，匹配用宽松包含），指标分两族：**quality**（precision/recall/f1）与 **economy**（`count` 越少越好、`noise` = 没命中任何 gold 的抽取词数，正是"到处是关键词"这个失败模式）。

**Assistant（`asst/core.py`）**：构造时用 `index.search("预热", top_k=1)` **touch** 一次嵌入器并记 `warm_ms`（这就是"嵌入模型启动即预热"的落点）；`process(segment)` 先抽词、再用**整段**做一次检索（不是用关键词，实测整段能命中正确章节而关键词会命中别的通用手册），**每个关键词的检索推迟到界面真的要看时**（`refine=True`）——一段 6 个热区只花 1 次检索而不是 7 次；`_search()` 的二次裁剪按**词重叠**挑出真正含答案的那一块并取 300 字窗口（默认 `top_k=3`、查询词 ≤12、每文件最多 3 块），因为"藏着答案的热点卡片没有用"。

**线索分类（`meeting/classify.py`）**

| | 规则通道（永远先跑） | LLM 通道（定稿后一次） |
|---|---|---|
| 覆盖 | 义务词（`必须/应当/不得/需要/要求/务必/尽快/抓紧/不能`，0.65；`要尽快/要确保…`，0.7）、承诺（第一人称 + 承诺词，0.7，且义务句里含承诺词则改判为承诺）、日期（`_DATE_CORE` 0.85 / `_REL_DATE` 0.7，向前吞并单位与期限后缀）、机构（0.8，过滤裸后缀、长度 <4 丢弃）、人名（职务后缀 0.7 / 已知参会人 0.9 / `请|让|由|叫|找+名` 0.5） | 意图判断：把"要求"识别成"风险"、把隐含决定标成"当场决定" |
| 手段 | 正则 + 分句锚点（取含匹配的**最短分句**、上限 30 字）+ span 冲突抢占 | `CLUE_PROMPT`（枚举 10 类 + 要求 anchor 是原文连续片段 2–12 字 + 只输出 JSON 数组），默认 `timeout=45` |
| 结果 | `[{type,text,anchor,confidence,…}]` | 经硬校验：kind 必须合法、anchor 必须在原文、占位符丢弃、confidence 夹到 [0,1]、`actor/due` 必须出现在原文 |
| 合并 | — | 以 **anchor 为键**：同 anchor 保留规则文本，**LLM 只允许升级 type，且规则类型 ∈ {person, org, deadline} 时不许覆盖**；confidence 取 max；按 confidence 降序 |

写进会话前还有一道硬门槛：`anchor` 非空但不在原句里 → 整条丢弃（`server.py:436`），"无来源可定位的线索宁可不显示"。为什么规则先跑：日期/义务/具名的人正是正则最擅长且最重要的类，而 LLM 补的是意图判断；LLM 不可达时看板仍可用（现场网络常比笔记本更差）。

### 3.10 事件抽取与导出

**文件**：`scripts/rag/extract.py`（规则 + LLM 抽取）、`event_store.py`（表与查询）、`events.py`（CLI）、`docread.py`（docx 读取）、`scripts/export_obsidian.py` / `export_minutes.py`（导出，独立脚本）

**事件表**（独立 `data/events.db`，`SCHEMA_VERSION=1`）

- `meetings(meeting_id pk, date, title, source_path, source_kind, attendees, extractor, indexed_at)`
- `events(id pk, meeting_id, date, type, content, deadline, owner, section, source_ref, seq, unique(meeting_id, seq, content))`
- `events_fts`（FTS5，写入 jieba 预分词后的文本，`rowid=events.id`）、`meta`
- **类型闭集（9 类）**：决策 / 行动项 / 甲方要求 / 甲方确认 / 己方承诺 / 风险 / 进度 / 议题 / 其他（`甲方确认` 与 `甲方要求` 分开：一个是要去做的指令，一个记录已达成一致）。

**抽取路径：规则优先，LLM 兜底**。`.md/.txt` 先 `extract_rules`（识别两种笔记形状：软件固定的五段 schema `会议摘要/关键决策/行动项/后续跟进/议题要点`，以及自标注类型的 bullet，如 `- **[甲方要求]** 9月5日前软件部署调通完成。（期限：9月5日前）`），命中且有事件就**不调模型**；否则（且允许 LLM）走 `extract_llm`。`.docx` 先由 `docread` 读元数据与正文，正文不符合 schema 才交给 LLM。

LLM 侧要点：输入是编号分句列表，**每批 6 句**（整段 418 字一次喂给本地 4B 模型会吐出 1894 字符的坏 JSON——引号重复、键重复；6 句/批则干净）；输出 `{i,type,content,deadline,owner}`；**结果不信任提示词**：长度 <8、命中元数据黑名单（提交版本/会议地点/与会人员…）、`1 林浩然 甲方单位` 这类表格行一律丢弃（实测表格进输入时 30 条里 17 条是元数据）；**期限必须接地**——模型给的 deadline 去掉空格后不是原文字串就清空并改用 `find_deadline(content)`（"被回引的捏造日期比没有日期更糟"）。

**幂等**：`meeting_id_for(path) = f"{stem[:40]}-{sha1(resolved_path)[:8]}"`（裸文件名会碰撞：同一份纪要的 `.docx` 与同名 Markdown 副本会被算成同一个 id，第二次导入静默覆盖第一次）；`upsert_meeting` 是**整会替换** + 会议内归一化去重（`re.sub(r"[\s\W_]+","",s)[:60]`）+ `insert or ignore`。

**四种查询形态**（`aggregate` / `list_by` / `timeline` / `find`）：

| 形态 | 做什么 | 为什么不是相似度检索 |
|---|---|---|
| `aggregate(type, owner, has_deadline, 日期区间)` | 精确计数 + 全表类型分布 | 7 场会议语料实测：问"甲方一共提出多少项要求"，相似度 **top-8 只召回目标类型 3/13**，按 `type` 过滤是 **13/13**——聚合**结构上**不是相似度问题 |
| `timeline(keyword)` | 跨会议按日期列出**全部**匹配事件（`content LIKE %kw%`） | "感应器的到货时间怎么变的"需要所有匹配事件按时间排序，top-1 永远只给一条 |
| `find(query)` | 走 FTS + `bm25`（jieba 分词，中文可召回性更好） | — |
| `list_by(...)` | 同过滤条件按 `date, seq` 列表 | — |

**为什么是扁平事件表而不是知识图谱**：7 场会议（1 真实 + 6 合成，事实互不相同）41 条事件上的定论——"找段落"混合检索 14/15 已经够用；"聚合"相似度做不到（3/13）；"遍历"需要全量；而该语料**根本不产生实体-关系三元组**（11 个疑似实体对里 5 个是子串假象），关系抽取只会为没人问的查询加一层。

**docx 读取为什么用 `xml.etree` 而不是正则**：同一份真实文件上正则坏了两次——正文在一个表格单元格里，而单元格可以嵌套 `<w:p>`，贪婪/非贪婪行为不一致（一次只读到 18 字符的标题，模型"抽取"了这些标题；另一次读到 0 字符）。`docread` 递归收集所有段落（含表格内），`docx_minutes_body` 取**最长段落**（真实纪要整个正文在一个单元格里、是一整段而不是分句列表），日期统一成 `YYYY-MM-DD`（否则混合格式会静默破坏时间线排序）。

**导出**：`export_minutes.py`（纪要 → 可点进依据的 Markdown）、`export_obsidian.py`（把别名归一到 frontmatter `aliases`，防 Obsidian 图谱碎片化）、`validate_obsidian_vault.py`（查悬空 `[[链接]]`）。三者都是**独立脚本/一次性动作**，界面与 HTTP 里没有导出端点。

### 3.11 前端单页

**文件**：`scripts/meeting/ui.html`（约 4150 行，内联 CSS/JS，无构建步骤；服务端原字节发给浏览器/WebView）

**三栏**（`ui.html:1138`）

| 栏 | 内容 |
|---|---|
| 左 · 参会人员 `#col-left` | 头像（姓名末 2 字 + 固定色）+ 姓名 + `org·role` + **声纹胶囊**（已绑定 / `N 句同一声音` / 未绑定）+ 删除；底部加人输入框 |
| 中 · 发言流 `#col-mid` | ① 可折叠「会议流程」`#plan-agenda`（导入按钮 / 计数 / 来源文件 / 逐条勾选、双击改名、删除、底部追加）② 发言计数 + 便签开关 ③ `#feed` 逐句（时间 / 说话人 / 正文，被改写过显示 `rev` 标记）④ 底部要点/便签输入条 |
| 右 · 线索 `#col-right` | 计数 + 类型筛选（纯色点 + 计数）+ 线索卡（可拖、可钉、可改类型、可删）；底部 `#detail` 证据面板 |

**覆盖层**：`#notes-layer`（便签层，视口坐标、`pointer-events:none`）、`#pop`（悬停摘要）、`#search`（搜索）、`#settings`（设置：左轨分节 + 右栏字段 + 页脚）、`#imp`（导入归属）、`#guide`（首次打开的使用引导）、`#toast`、`#boot-error`（顶部错误条）。前四个覆盖层共用同一条 `position:fixed; display:none` 规则（`.on` 才显示）——**加第五个面板时照抄那一行**，漏掉选择器会让面板变成文档流里的普通 div，把整个三栏挤塌（真发生过一次）。

**首次打开的使用引导**（`#guide`）：服务端的标记文件 `data/guide-seen` 不存在时（`/api/state` 的 `service.guide_seen=false`），首帧渲染完成（`boot()` 里第一次 `tick()` 之后）自动出现一次；六步分别指向**布局上一定存在的控件**（顶栏按钮 / 中栏容器），每一步给目标加一圈强调环（`.g-hl`，`outline` 不参与布局）。
- **标记为什么在服务端而不是 localStorage**：启动器用 pywebview，默认 `private_mode=True` —— WebView2 的用户数据目录建在内存里、退出即删，localStorage 一律不保留。标记若写在浏览器里，用户每次开应用都会被引导盖一遍，比没有引导更烦。关闭时 `POST /api/guide-seen` 落盘（`MeetingService.mark_guide_seen()`）。
- 关闭（走完 / × / 点外面 / `Esc`）即写下标记——"有没有看完"不追究，纠结它只会让下一个人再被弹一次。
- 这一层**刻意不压暗页面**（`background:none`，与另外三个覆盖层不同）：它要指的就是页面本身，压暗 + 模糊之后那圈环也跟着灰掉。只留一层透明全屏层接住"点外面 = 关闭"并挡住误点。
- 强调环加在**稳定的控件**上而不是列表项：发言流与线索列表每秒都在重建，指到列表项上的环会跟着节点一起消失。`Esc` 的判定把它排在**最前面**（它盖在最上层，否则第一次打开按 Esc 会去关别的层）。
- 再看一遍的两个入口：`设置 → 界面 →「再看一遍使用引导」`、以及 `?` 的快捷键提示末尾（`?` 本身仍然只弹 toast，不打开六步引导）。
- 自动化开关 `?guide=off`：审计 / 探针 / 截图启动的都是全新 profile，靠"已看过"的标记跳不过引导，而那一层透明全屏层会吃掉鼠标事件与 `Esc` 断言——它们一律带这个参数，只有 `probe_guide_ui.js` 不带（"第一次打开会弹"这件事只在那种状态下存在）。

**顶栏状态机**（`#btn-mic` / `paintMic()`）：麦克风不可用 → 禁用"录音"；ASR 未就绪 → 禁用并显示"ASR 加载中…"或"ASR 未就绪"；就绪 → 可点，录音中显示 `停止 m:ss`（0.5s 刷新）。点击是**乐观翻转**（先切本地计时器再 POST，失败回滚）。顶栏还显示会话标题（可改）、时长、句数/线索数、**音频电平**（`peak_dbfs > -45` 高亮，tooltip 给设备名/采样率/声道/已收秒数）、`试听回放`、`转录`（新标签开 `/api/transcript`）、左右栏折叠、设置、主题。

**交互细节**

- **悬停关键词出摘要**：130ms 延迟 → `GET /api/refine?term=` → 只给"出处 + 逐字原文"（按文件分组、章节面包屑取末两级），**刻意不给耗时/得分/机器概括**；`hidePop` 统一收口 mouseout/scroll/mouseleave/Esc。
- **点击关键词出证据**：若同名线索已存在就打开线索详情，否则造一个 `term:xxx` 临时对象并改走 `/api/refine`（避开 `/api/clue` 404）。
- **线索**：置顶 / 按 `types` 轮转改类别 / 删除；当前发言的线索排前面；`Ctrl+S` 置顶当前发言第一条线索。
- **声纹**：先点人（或选中某句自动选人）→ 点"这句是某人说的" = 署名 + 登记；建议条「听起来像 X 87%」+ 是他/不是他；**改名** `rename_speaker`、**改归属** `reassign_segment`（prompt 列出所有 spk，输入 `0` 清除）；点左栏胶囊解绑。
- **便签**：五个色点选类别、勾选已谈、删除、正文可直接改字、单条移除参考；拖拽期间**跳过重画**（否则每秒重画一次会把便签弹回起点）；把检索结果拖到便签上 = 钉为参考。
- **快捷键**：`Esc` 逐层关闭（**使用引导**→导入→设置→搜索→blur→popup→详情→清选中）；`1`/`2` 折叠左右栏；`?` 快捷键提示（末尾提示"完整引导在设置→界面"）；`Alt+J/K` 上下句；`Alt+G` 到底并恢复跟随；`Alt+P` 便签显隐；`Alt+L` 聚焦要点输入；`Ctrl+K` 搜索；`Ctrl+,` 设置；`Ctrl+E` 导出转写；`Ctrl+S` 置顶线索。输入框内除 Esc 外一律不响应（`isTyping` 守卫）。
- **主题**：**首帧之前**就在 `<html data-theme>` 上定下来（防白闪），颜色全走 CSS 变量，按钮文案显示**目标**主题，记忆用 `localStorage["plaud-theme"]`，只跟随系统偏好当用户没手动选过。
- **断线自愈**：400ms 轮询，连续 **3** 次失败 → 中性提示"服务暂时不可用（可能正在重启/加载模型），会自动重连"；连续 **25** 次 → 升级为错误条并给出日志路径；恢复后自动隐藏。

**数据流**：`GET /api/state?since=N` 增量轮询 + `EventSource("/api/stream")` 推送；`since` 的语义是 **`idx >= since`**，客户端按 idx 作键就地 `Object.assign`（修正 ≠ 追加）；`history_rev` 变化 → 整份重拉并**剪掉界面上已无对应发言的行**（增量协议看不见"删除"）；`service.spk_counts` 变化 → 重画发言（否则"同一声音 N 句"永不出现）；`service` 里 `audio / asr / corpora / managed / voice_names` 分别驱动电平、录音按钮、导入选项与设置页知识库节、重启按钮、署名。

**一个必须知道的形态问题**：`ui.html` 是从浏览器**"另存为"下来的快照**——文件第 2 行是 `<!-- saved from url=(0022)http://127.0.0.1:8510/ -->`、第 3 行 `<html data-theme="dark" data-darkreader-proxy-injected="true">`，另有 10 处 `--darkreader-*` 内联声明散在 11 行里（Dark Reader 扩展注入），末尾还有一个扩展注入的 Vue 宿主 div。这些内联变量会覆盖主题变量，是"主题看着不对劲"时要先想到的地方。

### 3.12 启动器与打包

**文件**：`launcher/app.py`（493 行）、`launcher/launcher.html`（197 行，加载页）、`launcher/build_launcher.ps1`（59 行）

- `Backend`：`start()` 选端口 → `Popen`（`CREATE_NO_WINDOW`，stdout/stderr 收进 `data/launcher/backend.log`）→ `_reader`（在内存里留最近 400 行 + 逐行落盘）/`_poll`（1s 一次 `/api/stats` 健康探测）/`_watch_flag`（重启标记）。
- `_note()`：启动器自己的事件（重启、起不来）也写进同一个日志文件——冻结版无控制台，不写文件就等于没记录。
- 生命周期：`window.events.closing → backend.stop()`（终止子进程）+ `finally` 兜底；`restart()` 先停、等端口不再应答、再用**同一端口**起来（页面不会失联）。
- 打包：PyInstaller `--windowed`（无控制台）+ `--collect-all webview/pythonnet/clr_loader`，`launcher.html` 作为 data 打进 `_internal/`；构建末尾自动生成 `launcher.json`（`repo_root`）——`--clean` 会清空 `dist`，手放的那份会丢。
- 启动期致命错误（找不到仓库根 / venv）会弹系统消息框，而不是"双击没反应"。

---

## 4. 端到端数据流

### 4.1 一句话的生命周期

```
声卡 (sounddevice, 16k 单声道)
  → AutoGain（语音包络增益 + 噪声门）
  → asr.push(chunk)（缓冲到 600ms 一块）
  → asr.tick()：VAD/端点 → 流式解码 → 标点 → 声纹嵌入 → SpeakerClusterer 在线分配
  → FeedSentence(idx, text, start, end, spk, emb, open/revisions)
  → LocalMicReceiver._publish：idx 重编号 + 时间戳平移 → receiver.on_segment
  → MeetingService.on_segment：
       ├ 会话 add_segment（立即）→ _broadcast（SSE 推送这一行的当前内容）
       ├ _analyse(llm=False)：Assistant.process（关键词 + 检索）→ classify_rules → add_clue(带 anchor/refs)
       └ _refresh_hotwords（这句新增的人名/术语进入下一句的纠错范围）
  → 5 秒没再改写 → _settle_pass → _analyse(llm=True)：classify_llm + merge（一次）
  → 页面：SSE 收到 → 就地重画该行；线索/检索结果同时出现
```

### 4.2 检索一条线索依据的路径

```
线索.anchor（原句里的确切子串，≥3 字）
  → Assistant._search(anchor) → RagIndex.search：FTS5(jieba) ∥ 向量(bge-small-zh INT8) → RRF 融合 → 归一化
  → 取前 2 条挂在线索 refs 上（anchor 太短才退回整句的 results）
  → 点线索 → evidence_for(clue)：原句 + 依据 + 相关线索
```

---

## 5. 关键设计决策与取舍

| 决策 | 理由 | 代价 |
|---|---|---|
| 全本地推理 | 会议内容不出机器；会议室网络常不可靠 | 模型体积与首次加载耗时（FireRedASR2S 约 5GB / 20s） |
| 单进程（UI + 音频 + 检索） | 句尾 ~0.5–1s 三样同时出；没有跨进程状态同步 | 一个进程崩了全崩；模型与界面共享 GIL/内存 |
| 音频固定 16k 单声道 float32 | FunASR 与 CAM++ 的输入规约；统一后下游不用再管格式 | 设备原生率高时要重采样（scipy，逐块 0.5s） |
| 轮询 + SSE 推送并存 | 推送解决"原地改行"，轮询兜底"历史被改"（`history_rev`） | 两套机制都要维护 |
| 声纹闭集 + 只建议不自动改名 | 认错人在会议里代价极高 | 陌生人需要临时点名绑定 |
| 人的声纹存多条向量（≤8） | 换麦/感冒/远近都不同，质心会越用越偏 | 误配概率略升 → 用 threshold+margin 双判据压住 |
| 检索分项目库 / 公共库 | 生命周期不同（项目库天天长） | 两本索引要管理；`MultiIndex` 融合逻辑更复杂 |
| 嵌入用 ONNX INT8（非 GGUF/非 torch） | 冷启动 0.96s、热查询 4.4ms、0 GPU；GGUF 跑不了解码器以外的图 | 需要 tokenizers + onnxruntime；换模型必须重建索引（向量空间不通用） |
| 发言计划做成真 Markdown 文件 | 是用户的文档（能 Obsidian/版本管理/打印），且随项目进知识库 | 需要往返稳定的序列化（整数归一、`nw=0` 表示自动、`［公共库］` 标记读回时摘掉） |
| 两级分析（即时规则 / 定稿 LLM） | 用户不用等模型；模型只看定稿句（22 句从 50 次调用降到 ~22 次） | 需要"行是否定稿"的判定（5s 静默）+ 每轮上限 4 行 |
| 手机 USB 麦克风通道整体删除 | 实际只用本机麦克风；两条输入通道让 server 里出现死代码与重复分支 | 历史能力下线（安卓 App / ADB reverse / 相关探针），见提交 `e1e44ce` |
| 启动器保留窗口但隐藏到就绪 | "关窗即停服务"与"无控制台"需要有个 owner；加载页只在慢启动/失败时才有价值 | 仍依赖 pywebview/WebView2（约 5MB 依赖 + 运行时） |

---

## 6. 数据与配置存放

| 路径 | 内容 | 是否入库 |
|---|---|---|
| `config/settings.json` | 全部后端设置（LLM/嵌入/ASR/知识库/界面），带权限收紧 | ✅ |
| `data/sessions/session-*.json` | 会话（发言/线索/参会人/议程/计划），自动保存 | ❌ gitignore |
| `data/voiceprints.json` | 没挂项目时的跨会声纹库 | ❌ |
| `data/ar.db` | 默认「公共库」索引（启动器首次运行会新建） | ❌ |
| `<项目>/` | `发言计划.md`、`导入/`、`导出/`、`.plaud/rag.db`、`.plaud/voiceprints.json` | 随项目 |
| `data/mic-test/mic-*.pcm` / `.wav` | 采集音频（裸 PCM + 收尾 WAV） | ❌ |
| `data/launcher/backend.log` | 后端子进程 stdout + 启动器自己的记录（排查用） | ❌ |
| `data/launcher/restart.flag` | 界面「重启服务」写给启动器的标记 | ❌（用完即删） |
| `data/tmp/*.py` | 自检 harness（见 §7） | ❌ |

---

## 7. 质量保障与验证手段

**仓库自带**（`scripts/`）：15 个 `test_*.py`、5 个 `eval_*.py`、9 个 `probe_*.py`、6 个 `check_*.py`、约 10 个数据生成/导出脚本，外加 `rag/selftest.py`（26 项）。完整清单见附录 B。

固定回归（文档与提交信息里反复引用）：

```powershell
cd meeting-assistant\scripts
..\venv\Scripts\python.exe -m rag.selftest          # 26/26
..\venv\Scripts\python.exe test_prep_api.py         # 发言计划 API
..\venv\Scripts\python.exe test_voiceprints.py      # 声纹库
..\venv\Scripts\python.exe check_hygiene.py         # 静态卫生（未使用常量/私有函数/可变默认参数）
..\venv\Scripts\python.exe check_ui_syntax.py       # ui.html 内联 JS 语法
# 需要真服务的两个（先起 meeting.server）：
..\venv\Scripts\python.exe test_settings_api.py     # 设置 API（跑完 settings.json 按字节还原）
..\venv\Scripts\python.exe test_import_scope.py     # 导入归属分流
```

**浏览器侧探针全部自建**：9 个 `.js` 探针都是"无头 Edge（`msedge.exe --headless=new --disable-gpu`）+ 手写极简 CDP over WebSocket"，各用不同 CDP 端口，仓库里**没有** Playwright/puppeteer/selenium。因此它们不随 `pnpm/npm` 环境走，跑之前要自己起服务与浏览器。

**端到端验证 harness**（`data/tmp/`，gitignore）：

| 脚本 | 验什么 |
|---|---|
| `exe_check.py`（带 `--dev`） | 端到端：双击 exe → 无控制台窗口 → 就绪后第一眼就是会议界面（比对"就绪时间 vs 窗口显示时间"）→ 设置页重启（换进程、同端口）→ 关窗后整棵进程树清零；跑完按字节还原 `settings.json` |
| `backend_check.py` | 启动器编排：健康探测、同端口重启、关窗停服、端口顺延、无控制台窗口 |
| `server_check.py` | 服务启动路径：默认挂公共库（索引缺了就建）、嵌入预热、`/api/settings` 回传 `restart_keys`/`managed`、`/api/restart` 写标记（非托管返回 400） |
| `replay_check.py` | 「试听回放」：`replay_wav` 的编号/时间平移、正在录音时拒绝、`/api/audio/test` 全链路 |
| `boot_check.py` | `meeting.server` 真启动 + `/`、`/api/state`、`/api/mic` 端点接线 |
| `winproc.py` | 上面几个共用的 ctypes 小工具：查子进程、查"有没有可见控制台窗口"、发 `WM_CLOSE` |

---

## 8. 性能实测（本机）

| 环节 | 实测 | 出处 |
|---|---|---|
| ASR（FunASR GGUF/流式，CPU） | 3 分钟音频 ~5s（RTF≈0.027） | README |
| ASR（FireRedASR2S） | CER 3.05%（FunASR 4.16%） | `requirements.txt:36` |
| 模型加载（FireRedASR2S） | ~20.7s（后台线程，界面先出来） | 实测 |
| 嵌入冷启动 / 热查询 | 0.96s / 4.4ms（torch+transformers 方案是 14.5s / 65ms） | `requirements.txt:23` |
| 服务就绪（含公共库 + 嵌入预热） | 2.6–6.6s | 实测 |
| 整段检索 / 关键词按需检索 | 16–18ms / 9–11ms | README |
| 音频 → 检索端到端 | 句尾后 ~0.5–1s | README |
| 项目索引（增量） | 首次 0.96s / 再启动 0.095s | README |
| 检索命中（hybrid） | hit@3 15/15、top-1 15/15 | README |

---
## 9. 已知限制

1. **文档格式**：知识库索引只吃 Markdown（PDF/DOCX/XLSX/PPTX 解析库未内置，建议先转成 Markdown 再入库）；议程导入能读 `.docx`，但那条路径**只解析不索引**——两件事不要混。
2. **无文件监听**：项目索引只在启动时同步一次，会议中新增文件要重启服务或手动 `python -m rag.sync`。
3. **声纹闭集**：完全陌生的人要临时点名绑定；很短的行（真麦段长中位约 1.1s）常常算不出可信向量，于是没有说话人；窄带电话音频下的在线判断不稳。
4. **LLM 依赖**：`--no-llm` 只有规则通道（快、可离线，但偏保守）；本地 LLM 首次调用慢；抽词与线索分类都靠提示词约束，模型越弱越容易漂（所以有 anchor 硬校验与降级）。
5. **Obsidian 导出是一次性的**：还没有"边开会边增量更新 vault"。
6. **两个 ASR 后端各有硬限制**：
   - StreamASR：加载 5 个模型（实测 20–25 秒）；`flush` 会丢弃不足 600ms 的尾巴。
   - FireRedASR：**没有增量解码**（VAD 收句才整句过 AED，开放句要 ≥3s 且每 3s 才重识别一次 → 开头约 3 秒界面无字）；**不做 ITN**（"十八"不会变"18"）；**不接热词**（没有 `set_hotwords`）；必须喂 int16 尺度（喂 float32(±1) 会 OOD、静默返回空文本）；模型约 5GB 且需 GPU。
7. **输入通道只有本机麦克风**：`scripts/phone_mic/streaming.py` 里的 `StreamingASR` 与 `save_segments`（`"source": "phone-mic"`）只被离线工具使用，不在服务链路里。
8. **界面音频 tooltip**：本机接收器的 `status()` 没有 `sample_rate / channels / seconds_received / peak_dbfs` 字段，鼠标悬停会显示 `undefined`（补齐这 4 个字段即可）。
9. **启动器依赖 WebView2 运行时**：机器上没有 WebView2 时窗口起不来（错误会弹消息框）；启动器本身是 pywebview/pythonnet（约 5MB 依赖 + 运行时）。
10. **`ui.html` 是浏览器"另存为"快照**：内含 Dark Reader 注入的 10 处内联变量，这些内联值会覆盖主题变量，改主题时要留意。
11. **Windows 限定**：启动器（pywebview）与部分默认路径按 Windows 写死；核心服务理论上可跨平台，但未验证。

---

## 附录 A · 模块清单

| 路径 | 行数（含空行） | 职责 |
|---|---|---|
| `scripts/meeting/server.py` | 1564 | HTTP 服务 + `MeetingService`（采集接线、两级分析、API） |
| `scripts/meeting/session.py` | 1203 | 会话数据模型与全部变更操作 |
| `scripts/meeting/ui.html` | 4146 | 单页前端（三栏工作台 + 设置 / 导入 / 计划面板 + 使用引导） |
| `scripts/meeting/{classify,outline,planfile,settings,voiceprints}.py` | 各 100–420 | 线索分类 / 议程导入 / 发言计划文件 / 设置 / 声纹库 |
| `scripts/phone_mic/mic_source.py` | 472 | 本机麦克风采集、AGC、落盘、回放 |
| `scripts/phone_mic/{stream_asr,firered_asr,streaming}.py` | 848 / 463 / 1481 | 两个 ASR 后端 + 共享的 `FeedSentence` / `SpeakerClusterer` / `StreamingASR` |
| `scripts/phone_mic/{audio,hotwords}.py` | 142 / 281 | 音频换算 / 热词纠错 |
| `scripts/rag/*.py` | 14 文件 2887（`rag_core.py` 780） | 索引、嵌入、切片、增量同步、多库融合、事件抽取、自检 |
| `scripts/asst/{core,keywords}.py` | ~800 | Assistant（关键词→检索→证据）与抽词（线索分类在 `meeting/classify.py`） |
| `launcher/app.py` + `launcher.html` | 492 + 196 | 原生启动器与加载页 |
| `scripts/asst/server.py` | 234 | 独立助手服务（默认 8500） |

## 附录 B · 工具索引（`scripts/`，按用途）

**自检 / 回归（16）**：`test_classify`（规则分类必过；`--llm` 只验契约）· `test_firered`（200ms 分块喂真录音，验文本/标点/说话人/时间戳/RTF）· `test_fix_batch`（11 组回归：sync prune / ASR reset / voice_names 与落盘 / ingest 跳过规则 / source 过滤位置 / 回放并发 / 定稿重试 / 短行说话人 / flush 尾巴 / 界面静态 / **使用引导标记在服务端**）· `test_import_scope`（导入归属分流，需服务）· `test_multi_index`（多库融合按排名）· `test_outline`（多种中文大纲格式）· `test_planfile`（往返 `read(write(x))==x`）· `test_plan_file_api`（自建服务：API 落盘/重载以文件为准/不丢 refs 与坐标）· `test_prep_api` · `test_prep_topics`（过短 topic 拒绝而非误合并）· `test_rag_scope`（纪要进索引、逐字转写不进）· `test_settings_api`（需服务）· `test_stream_pipeline`（真麦录音回归：音频空洞/行重复/切词）· `test_voiceprints` · `test_voiceprint_real`（CAM++ 归簇与跨会匹配）· `test_voice_bind`

**评估（7）**：`eval_crossmeeting`（7 场会横评，top-1 必须同时命中会议与条款——决定要不要上知识图谱）· `eval_kb_split`（分库 vs 混库）· `eval_project_isolation`（跨项目污染三级指标）· `eval_project_prior`（RRF 平票时的项目先验）· `eval_source_filter`（`source=` 过滤 vs 独立索引）· `asst/eval_keywords`（抽词 quality + economy）· `span_union_bound`（丢字定位：环形缓冲还是 fold_span）

**探针（9 个 .py + 6 个 .js）**：`probe_agg_decisive`（逐事件索引 vs chunk 检索在聚合题上）· `probe_delete_cleanup`（删后 chunks/FTS/向量/孤儿行）· `probe_guard`（保护带与 open 判定）· `probe_rag_instances`（扫缺 `check_same_thread=False` 的连接）· `probe_score_spread`（分数能否当置信度）· `probe_stream_vad` / `probe_vad_cuts`（FunASR VAD 基准与真实切点）· `probe_page.py` / `probe_note_drag.py` / `probe_hover_popup.js` / `probe_note_drag.js` / `probe_plan_ui.js` / `probe_settings_ui.js` / `probe_voice_ui.js` / `probe_guide_ui.js`（无头 Edge + 手写 CDP，验页面真实表现）

> 所有 UI 探针与 `audit_ui.js` / `shoot_ui.js` 都用**全新 profile** 启动浏览器，所以每次都是"第一次打开"——它们一律在 URL 上带 `?guide=off` 来跳过使用引导（见 §3.11）。`probe_guide_ui.js` 是唯一的例外：它**不带**，因为"第一次打开自动弹出"这件事只在新机器/新 profile 上存在。它会先把服务端的标记文件 `data/guide-seen` 删掉（"还没看过"是它的前提）、跑完再还原，并验四件事：`?guide=off` 真的压得住、第一次打开会弹且六步能走完（含对比度与"关掉后录音按钮能点到"）、第二次打开不再弹、以及「设置 → 界面」能把它叫回来。

**UI / 代码检查（6 + 3）**：`check_ui_syntax`（内联 JS 语法 + 声明遮蔽）· `check_css_braces`（多余花括号会吞掉后续规则）· `check_theme_contrast`（亮/暗色 WCAG AA）· `check_injected_js`（注入 JS 被内层反引号截断）· `check_hygiene`（phone_mic AST 卫生）· `check_meet_queries`（真值问题命中哪份纪要）· `audit_ui.js`（1000+ 行手写 CDP：控制台报错/溢出/截断/三栏几何/对比度/快捷键/点按尺寸）· `shoot_ui.js`（截图）· `pick_contrast.py` / `crop_shot.py`（配色与截图辅助）

**数据生成 / 演示**：`gen_meeting_audio`（合成 ogg + 逐句真值）· `gen_long_meeting`（长独白最坏用例）· `gen_synth_corpus` · `gen_multi_project_corpus`（三项目实体不重叠 + 真值问题）· `make_demo_session` · `make_sample_outline` · `seed_notes_demo` · `mic_list` / `mic_record` / `mic_live` / `mic_analyze` / `transcribe_capture` · `_dl.py` / `_sv.py`（下载声纹模型 / 打余弦矩阵）

**导出与展示**：`export_minutes` · `export_obsidian` · `validate_obsidian_vault` · `show_result`（看 `stress_long` 结果并标出丢掉的字）
