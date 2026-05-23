# 工具包 4.0 — 桌面端 Agent Pipeline / Multi-modal Tool Use 系统

> 一套**生产环境运行中**的桌面端商品数据处理 Agent。Yahoo TW 拍卖端到端自动上架：12 阶段 Agent Loop + 多模态 Tool Use + 自我验证回滚 + Harness 基础设施。

> **现状**: v4.0.83+, **11 用户日活, 单日 ~30 万 hub API calls, 累积 12 天 11000+ K0 LaMa events / 316066 个 API calls**。

> 这是 [`multi-account-automation-platform`](https://github.com/stzsdzswyxgs1-byte/multi-account-automation-platform) 的姐妹项目 — 那个负责**售后 + 客服 Agent**，这个负责**上架前数据处理 Agent**。

---

## 这个项目跟「Agent Harness 研发工程师」岗位的对应关系

| 岗位要求 | 在这个项目里的对应实现 | 关键代码 |
|---------|---------------------|---------|
| **桌面端 Agent 产品** | Tkinter GUI (1804 行 `app.py`) + 12-stage pipeline，端到端跑批，无服务端依赖 | `工具包4.0/商品處理中樞/app.py` |
| **Agent Loop** | 12 stage 串行 pipeline（A→B→C→D→E→G→F→H→I→J→K0→K），每 stage 独立 checkpoint + cooldown + 错误隔离，Resume 从任一阶段接续 | `pipeline.py` (1681 行) |
| **Tool Use（多模态）** | • **GPT-5.5 Vision** (商品分类 + 违禁判定 + SEO 标题生成)<br>• **gpt-image-2** (3-in-1 首图优化：违禁过滤 + 局部特写跳过 + 主图替换)<br>• **LaMa inpainting** (big-lama, K0 去水印救援)<br>• **闲鱼采集** (Python + SQLite) + **煤炉采集** (Node.js coordinator + 多浏览器) | `processors/seo_v64_full.py`<br>`processors/image_optimizer.py`<br>`processors/image_dewatermark.py` |
| **Reasoning / Self-correction** | **K0 LaMa 救援的 verify-rollback 闭环**：locate → inpaint → verify 残留检查 → 残留则 rollback 原图。模型不是 "跑一遍完事"，是「自验证 + 自纠错」闭环。VERIFY_PROMPT 有 8 类例外清单（对立平台 URL / 防盗声明 / app UI / sticker / 拼贴 / 影片截图…），即使 LaMa 没处理过也算残留 → rollback | `processors/image_dewatermark.py` |
| **Planning / Skills** | Stage E batch reject scan：4 图一批 → 模型按 **7 大必抓类** + **5 大放行例外** 输出 `reject_indices + reject_scores`。每个 stage 用独立 prompt + JSON schema，是典型的「Skills」抽象 | `processors/seo_v64_full.py` |
| **Memory / 状态持久化** | • CheckpointManager: 每 stage 完成持久化结果，Resume 从断点接续<br>• APIMonitor: per-stage rolling fail rate (window=30, min_sample=5)<br>• DynamicSemaphore: 动态并发槽位 (cap 可动态调) | `checkpoint_manager.py` |
| **Context Engineering** | Stage F-V65 多模态 SEO：Stage A (清洗+简述+违禁) → Stage B (主副词) → Stage C (SEO标题+视觉违禁) → Stage D (标签) → Stage E (reject 扫描)，每阶段 context 都基于上阶段产物 + 当前商品图，分层渐进 | `seo_v64_full.py` |
| **Harness Engineering（核心匹配点）** | • **AdaptiveCapController**（动态并发控制器，详见下方）<br>• **Cloud auto-update**（Cloudflare Worker + R2 + Durable Object, atomic extract + SHA256 verify + zip-slip 防御）<br>• **Quota system**（per-TG-ID, 2000/day, Asia/Taipei 自动重置, precheck→consume→refund 失败退还）<br>• **Feedback collector + atexit 兜底**（防 daemon thread 被 process kill 导致 detail log 没上传） | `processors/utils.py`<br>`_updater/updater.py`<br>`quota_client.py`<br>`processors/feedback_collector.py` |
| **MCP（外部能力接入）** | Cloudflare Worker 作为统一外部能力总线 — quota / feedback / detail log push / updater manifest 都通过同一套 Worker namespace 接入，4 个 app 独立 whitelist | `_updater/`, `quota_client.py` |
| **真实任务反馈持续迭代** | **4.0.10 → 4.0.83+ 累积 70+ 版本**，每个版本都有 audit data 支持。例如：<br>• 4.0.57 `mask.getbbox()` PIL API typo（1 行修复，影响 50+% rescued case）<br>• 4.0.74 PauseException 不吞（5/11 cascade 根因）<br>• 4.0.83 AdaptiveCap cooldown 10→5（针对 short-burst 任务的响应速度优化） | `CHANGELOG` 在 README 末尾 |
| **对模型行为有品味** | • **不同 stage 不同 prompt 形态**：Stage A 用 batch + JSON, Stage E 用 multi-image + indexed output, K0 用 chain-of-thought<br>• **入口判定**：LOCATE_PROMPT 先判「整张该丢」（app 截图 / 拼贴 / 海报），直接放弃不浪费 GPU 跑 LaMa<br>• **bbox 中心保护**：locate 区域 0.15-0.85 跳过避免破坏商品本体，但 verify 对 8 类全图检查兜底<br>• **OOM-aware**：LaMa pool 加载 OOM 自动降级（24GB→28, 12GB→16, 8GB→10） | `image_dewatermark.py` |
| **生产规模** | **11 用户日活, 单日 30 万 hub API calls**, 12 天累积 11000+ K0 LaMa events / 316066 个 API calls | feedback 系统实测数据 |

---

## 1. 系统架构

```
┌──────────────────────────────────────────────────────────────────────┐
│              Tkinter GUI  (app.py 1804 行 + pipeline.py 1681 行)      │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
   ┌─────────────────────────────┼─────────────────────────────┐
   ▼                             ▼                             ▼
┌────────────┐         ┌─────────────────────┐         ┌────────────────┐
│ 数据源       │         │ 12-Stage Agent Loop │         │ Harness 层      │
│ (Tool Use) │         │                     │         │                │
├────────────┤         ├─────────────────────┤         ├────────────────┤
│ • 闲鱼      │────────►│ A: 分类 (GPT Vision)│◄────────│ AdaptiveCap    │
│   Python   │         │ B: 价格转换         │         │ (每 stage 独立  │
│   SQLite   │         │ C: 翻译 (gpt-5.5)   │         │  动态并发)     │
│ • 煤炉      │         │ D: 替换词           │         │                │
│   Node.js  │         │ E: 价格清洗         │         │ Quota          │
│   多浏览器  │         │ G: 关键词过滤       │         │ (per-TG-ID,    │
│ • Yahoo TW │         │ F-V65: 多模态 SEO   │         │  Cloudflare DO)│
│            │         │   ├ A 清洗+违禁    │         │                │
│            │         │   ├ B 主副词       │         │ Feedback       │
│            │         │   ├ C SEO+违禁     │         │ Collector      │
│            │         │   ├ D 标签         │         │ (events + log) │
│            │         │   └ E reject 扫描 │         │                │
│            │         │ H-J: 模板填充       │         │ Cloud Updater  │
│            │         │ K0: LaMa 救援 ★   │         │ (R2 + SHA256   │
│            │         │ K: 首图优化 ★      │         │  atomic extract│
└────────────┘         └─────────────────────┘         └────────────────┘
                                 │
                                 ▼
              ┌──────────────────────────────────┐
              │ Self-correction Loop (K0 LaMa)   │
              │                                  │
              │  locate → inpaint → verify       │
              │     ▲                  │         │
              │     └──── rollback ────┘         │
              │     (8 类例外触发回滚)            │
              └──────────────────────────────────┘
```

---

## 2. 4 个 App 拆分

```
工具包4.0/
├── 商品處理中樞/     ← 主 Agent（Tkinter + 12-stage pipeline）
├── SEO翻譯工具6.0/   ← 独立翻译 Subagent（日→繁, gpt-5.5）
├── 闲鱼采集0420/     ← 闲鱼数据 Tool（Python + SQLite）
├── 煤爐采集0324/     ← Mercari 数据 Tool（Node.js coordinator + 多浏览器）
└── _updater/        ← 共用 Harness 基建（云端自动更新系统）
```

4 个 app 共享同一套 updater 基建（独立 whitelist namespace，互不影响），是典型的 「Harness 复用」工程。

---

## 3. 核心 Harness 技术亮点

### 3.1 AdaptiveCapController — 动态并发的 Harness 抽象

每个 stage 独立 cap（Stage C 慢不影响 Stage E）：

```
起点 = middleware suggestion × my_rpm_share / active_accts
第一次大跳 → (cap + max) / 2
后续 → +25%
fail rate 0.15-0.30 → -20%
fail rate > 0.30 → -50%
cooldown=5 task, window=30 task, min_sample=5
network_blip + cascade_pause 过滤（不算 fail signal）
```

**这是 Harness Engineering 的典型工作** — 不是写业务，是给 Agent 提供运行环境的稳定性保证。

### 3.2 K0 LaMa 去水印 — Agent Self-correction 闭环

Stage E 标 reject 的图，尝试「定位 → inpaint → verify → rollback」：

| 阶段 | Prompt 类型 | 关键约束 |
|------|------------|---------|
| **LOCATE** | Chain-of-thought + bbox 输出 | 入口判定：app 截图 / 拼贴 / 海报 → 直接放弃；中心区域 (0.15-0.85) 跳过避免破坏商品 |
| **INPAINT** | LaMa big-lama 模型（CUDA sm_61→sm_120 跨代支援） | OOM 自动降级 pool 大小 |
| **VERIFY** | 多类例外 + 全图检查 | 8 类即使 LaMa 没处理过也算残留：对立平台 URL / 防盗声明 / 规格文字 / app UI / sticker / 拼贴 / 影片截图 / 中央促销标 |
| **ROLLBACK** | 文件级原子还原 | verify fail → 还原原图，本张算 reject |

**这套设计的意义**：模型不是 "跑一遍完事"，而是「自己看自己跑出来的结果，发现不对就回滚」— 是 Agent Reasoning 的真实形态。

### 3.3 Stage E batch reject scan — Skills 抽象

4 图一批，模型按 **7 大必抓类** 输出 `reject_indices + reject_scores`：

1. 整张无商品纯文字
2. 装饰边框
3. 售价 / 促销标
4. 卖场 logo（任何角落）
5. 卖家方框印章 + 时间戳
6. 角落红字尺寸 / 重量规格
7. 中央描述 / 赞美文字

**5 大放行例外**：商品本体字 / 评级盒鉴定 / 商品纹饰 / QR code 马赛克 / 手指拿着。

每条 prompt 都是从真实生产案例迭代出来 — 4.0.55 修了 BATCH_REJECT regex greedy parse fix，4.0.56 silent exception 可见性问题。

### 3.4 Cloud Update System — 多 App 共用 Harness

Cloudflare Worker + R2 bucket，4 个 app 独立 whitelist namespace：

- `client_key`（read only）+ `admin_key`（push）二分
- whitelist-only 模式，zip 内含 whitelist 外的 path **拒绝解压**（zip-slip 防御）
- 流程：`start.bat → updater.py → check manifest → fetch zip → SHA256 validate → atomic extract → run app`
- hot-reload 不重启：updater 拉新版 atomic extract，跑批中途用户不会被打断

### 3.5 Quota System — Multi-tenant Harness

per-TG-ID 配额 + Cloudflare Worker + Durable Objects：

- 预设 2000/day，Asia/Taipei 10:00 自动重置
- 1 商品 = 1 额度，**只 lock 贵功能**（Stage E / 去水印 / 首图优化）
- precheck → consume → refund（失败自动退）

### 3.6 Feedback Collector + atexit 兜底

每个 K0 event 实时 push，batch 结束 gzip + base64 push 完整 detail log。

**关键 fix**：atexit 兜底等 background thread 跑完才放行 — 避免 daemon thread 被 process kill 导致 detail log 没上传。这是从生产里实际丢数据后反推出来的工程加固。

---

## 4. 12-stage Pipeline 全图

```
Step 0  读档 / Resume
Step A  分类处理 (mercari → Yahoo 类目, GPT Vision)
Step B  价格转换 (mercari ¥ → TWD, 多段乘数)
Step C  翻译处理 (日→繁, gpt-5.5)
Step D  替换词处理
Step E  价格 / 数字清洗
Step G  关键词过滤 (LLM 前)
Step F-V65  多模态 SEO + 视觉违禁判定
        ├ Stage A 清洗 + 简述 + 违禁
        ├ Stage B 主副词
        ├ Stage C SEO 标题 + 视觉违禁
        ├ Stage D 标签
        └ Stage E reject 扫描 (★)
Step H  默认值填充
Step I  标题去重
Step J  说明模板追加
Step K0 ★ 去水印救援 (K0 LaMa, verify-rollback 闭环)
Step K  ★ 首图 AI 优化 (gpt-image-2 3-in-1)
```

---

## 5. 关键设计决策

**为什么 12 stage 而不是 monolithic?**
- 独立 cooldown：每个 stage 用自己的 AdaptiveCap，Stage C 慢不影响 Stage E
- 独立 checkpoint：Resume 从任一阶段接续
- 错误隔离：Stage A unified batch fail 直接归入不合格，不影响其他商品

**为什么 verify 限 bbox + 例外清单?**
- 限 bbox：避免把商品旁边的书封字 / 包装纸字当水印残留 → 误 rollback
- 例外清单：K0_locate 不框中心区域避免破坏商品，但对立平台 URL / 卖家防盗文字必须 catch，所以 verify 对这几类**全图检查**做兜底

**为什么 hot-reload 不重启?**
- 客户端 start.bat → updater 拉新版 atomic extract，跑批中途用户不会被打断
- 中介 cli-proxy 透过 fsnotify 侦测 disabled 旗标，3 秒内生效

---

## 6. Tech Stack

| 层 | 技术 |
|----|------|
| 客户端 GUI | Python + Tkinter |
| 采集 | Python（闲鱼）+ Node.js coordinator（Mercari） |
| AI Vision | GPT-5.5（multimodal chat） |
| AI Image | gpt-image-2（商品首图优化） |
| 图像处理 | LaMa inpainting（big-lama, simple-lama-inpainting） |
| GPU | CUDA（sm_61 → sm_120 跨代支援） |
| 云端代理 | Cloudflare Worker + Durable Objects + R2 |
| 观察性 | JSONL feedback events + gzip detail log push |
| 配额系统 | per-TG-ID, 2000/day, Asia/Taipei 自动重置 |

---

## 7. 版本迭代历程（真实 audit-driven fix 摘录）

从 4.0.10 → 4.0.83+ 累积 70+ 版本，**每个版本都有 audit data 支持决策，不是猜的**：

| 版本 | Fix | 来源 |
|------|-----|------|
| 4.0.42 | cooldown 20 → 10 加大跳 ratchet | 生产 RTT 数据观察 |
| 4.0.55 | BATCH_REJECT regex greedy parse fix | 真实 LLM 输出 corner case |
| 4.0.56 | silent exception swallow visibility | feedback log audit |
| 4.0.57 | `mask.getbbox()` PIL API typo | **1 行 fix, 影响 50+% rescued case** |
| 4.0.58 | K0 并发 GPU pool floor | OOM 频率分析 |
| 4.0.59 | LaMa pool 升级 + OOM auto-reduce | 跨用户 GPU 配置差异 |
| 4.0.60-69 | VERIFY 加 8 类 bbox 外例外 | 真实漏抓案例累积 |
| 4.0.74 | PauseException 不吞 | **5/11 cascade root cause** |
| 4.0.75 | LOCATE 入口「整张该丢」判定 | GPU 浪费率统计 |
| 4.0.81 | RTT cap 解除 | 网络稳态后的吞吐瓶颈 |
| 4.0.83 | AdaptiveCap cooldown 10 → 5 | short-burst 任务响应速度 |

招聘 JD 里 "**用真实任务反馈持续迭代产品**" — 这就是。

---

## 8. 未包含 / Sanitization

- `big-lama.pt` (196MB model weight, 下载自 [simple-lama-inpainting releases](https://github.com/enesmsahin/simple-lama-inpainting/releases))
- `.venv/` Python 虚拟环境
- admin 内部工具（`pack_update.py` / `admin_review.py` / `update_admin_config.json`）
- 生产数据（采集 xlsx / cookies / db / log / images）
- 中介 server（`rate-limiter.js`）— 部署在另一台机器

所有可识别资讯（API 端点 / Token / TG ID / 帐号 emails）已脱敏为 placeholder。
