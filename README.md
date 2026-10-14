# nankeep —— NanShare strm 库治理工具

针对 `\\<你的NAS>\emby\strm\nanshare` 的「分享已整理 / 转存已整理」双库做**只读优先**的治理：

```
扫描 → 规格解析 → 多版本择优 / 逐集混版本 / 缺集 / 命名异常 / 孤立字幕 / 跨库对照
     → 报告 → 计划 → 执行前指纹复核 → （精确确认后）删除
```

> 定位：站在 NanShare **下游**。nankeep 不生成 strm、不改 config、不参与 302 播放，只读你已经整理好的目录。

两种用法，**同一套核心逻辑**：

| 形态 | 入口 | 适用 |
| --- | --- | --- |
| **Web 层（推荐）** | `nankeep_web.py` / 容器 `:8322` | 随时随地浏览器可用；带 11 道删除闸门 + 审计 |
| CLI | `nankeep.py` | 本机批处理、脚本化、导出 shell 计划 |

设计信条（照抄 StrimKeep 的删除模型）：**干得快 ≠ 敢删；宁可漏删、绝不误删。**

## 快速开始

```bash
cd /path/to/nankeep

# 1) 扫描并落索引（约 60~90 秒，输出 out/library.json.gz，约 2.7 MB）
python nankeep.py scan --lib share

# 2) 出治理报告（复用索引，秒出）
python nankeep.py report --lib share --use-cache --top 50
#    → out/report.md  +  out/analysis.json

# 3) 生成治理计划（默认电影；含文件指纹）
python nankeep.py plan --kind movie --use-cache
#    → out/plan_movie.json

# 4) 执行前复核（默认 dry-run，只打印）
python nankeep.py apply --plan out/plan_movie.json
python nankeep.py apply --plan out/plan_movie.json --emit-sh
#    → out/apply_movie.sh（默认 DRY=1 只打印；确认真删要手动改 DRY=0 再跑）

# 5) 或者直接起 Web 版（推荐，见下节「Web 层」）
python nankeep_web.py
```

## Web 层（推荐：随时随地可用）

CLI 只能在工作机的终端里跑。Web 层把同一套治理逻辑（**完全复用 `nankeep.py`，不是重写**）
包成一个常驻服务，部署在 NAS 上后用 Lucky 反代出去，手机/公司/任何地方打开浏览器就能用。

```
容器 nankeep（python:3.12-slim, uid 1000）
   ├─ /library  ←  /path/to/nanshare      （读写：删除需要写权限）
   └─ /data     ←  ./data                              （SQLite 计划+审计 / 索引缓存 / 互斥锁）
        │
        └── :8322 ──→ iKuai 上的 Lucky 反代 ──→ https://<你的域名>
```

### 部署（NAS 上）

```bash
mkdir -p /opt/nankeep && cd /opt/nankeep
# 放入 docker-compose.yml，改三处：NANKEEP_PASSWORD、volumes 左侧库路径、NANKEEP_ALLOWED_ORIGINS
mkdir -p data && sudo chown -R 1000:1000 data      # 容器以 uid 1000 运行，data 必须可写
docker compose up -d
docker compose logs -f                              # 看到 "web 已启动" 即成功
curl -s localhost:8322/api/health                   # {"ok":true,...}
```

**镜像**：`ghcr.io/utterliar1/nankeep:latest`。
推 `v*` tag 会触发 CI 构建 amd64+arm64 双架构并推送，所以 NAS 架构通常直接可用
（`docker compose pull` 即可升级）。用 `:v0.1.0` 锁版本，`:latest` 跟随主线。

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NANKEEP_USER` | `admin` | 登录用户名 |
| `NANKEEP_PASSWORD` | **（空）** | **必填，为空则拒绝启动**。明文比对（同 StrimKeep），请用长随机串 |
| `NANKEEP_SESSION_SECRET` | 回落密码 | 会话 HMAC 密钥。单独设随机值 → 改密码不会踢掉已有会话 |
| `NANKEEP_ROOT` | `/data/strm/nanshare` | 库根（容器内路径） |
| `NANKEEP_DATA` | `/data` | 状态目录：`nankeep.db`、`agent.lock` |
| `NANKEEP_OUT` | `$NANKEEP_DATA/out` | 索引缓存与报告输出 |
| `NANKEEP_CONFIG` | 镜像内 `nankeep.config.json` | 覆盖分类配置路径 |
| `NANKEEP_PORT` | `8322` | 监听端口 |
| `NANKEEP_BIND` | `0.0.0.0` | 监听地址 |
| `NANKEEP_SESSION_DAYS` | `30` | 会话有效期（天） |
| `NANKEEP_PLAN_TTL_MIN` | `120` | 计划有效期（分钟），过期即作废 |
| `NANKEEP_CONFIRM_PHRASE` | `删除这些 strm 文件` | 真删前必须精确输入的短语 |
| `NANKEEP_ALLOWED_ORIGINS` | （空） | 反代域名白名单，逗号分隔。不填=仅同源 |
| `NANKEEP_ALLOW_NO_AUTH` | （空） | `1` = 免登录（**仅限本机调试，禁止对外**） |

### 反向代理（Lucky，跑在 iKuai 上）

容器**必须**发布端口（`8322:8322`）—— 因为 Lucky 在 iKuai 主机上，和 NAS 不是同一台，
走不了 docker 网络。Lucky 里建一条 Web 服务规则：

| 项 | 值 |
| --- | --- |
| 前端域名 | `nankeep.你的域名`（`*.<你的域名>` 泛解析指向你的公网 IP） |
| 后端地址 | `http://<NAS 的局域网 IP>:8322` |
| 协议 | HTTPS（Lucky 自动签证书） |

**反代后必做**：把浏览器里访问的完整域名填进 `NANKEEP_ALLOWED_ORIGINS`，否则写请求
（扫描/生成计划/删除）会被同源校验挡掉 —— 这是防 CSRF 的设计，不是 bug。

### 页面能做什么

| 区域 | 动作 | 会删文件吗 |
| --- | --- | --- |
| 运行栏 | 扫描库 / 生成报告 / 生成计划（选 kind + 最低置信度） | 不会 |
| 状态 | 库根可写性、作品/strm 计数、规则指纹、待处理计划数 | 不会 |
| 报告 | 直接渲染 `report.md`（表格/引用块/标题，HTML 已转义） | 不会 |
| 计划 | 列待删文件 + 保留版 + 置信度；**「复核」按钮先重算指纹** | 不会 |
| 计划 | **「真删」按钮**：复核通过 **且** 精确输入确认短语后才可点，再弹一次 `confirm()` | **会** |
| 审计 | 每次生成计划/执行清理的留痕（含结构化结果），可点开看详情 | 不会 |
| 日志 | 服务端运行日志尾部（3 秒轮询） | 不会 |

## 命令

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| `scan` | 扫描两库，落 `out/library.json.gz` | `--lib share\|transcode\|both` |
| `report` | 生成 `out/report.md` + `out/analysis.json` | `--top N`（0=全部）、`--use-cache` |
| `plan` | 生成 `out/plan_<kind>.json` | `--kind movie\|tv`、`--min-confidence`、`--use-cache` |
| `apply` | 执行前逐文件指纹复核 | `--plan`、`--emit-sh` |

全局参数：`--root`（覆盖库根）、`--order`（比较顺序）、`--unknown`（未识别策略）、`--config`。

## 报告看什么

| 段落 | 含义 | 能做什么 |
| --- | --- | --- |
| 1. 规模 | 分类 / 作品数 / strm 数 / 季数 | 掌控库体量 |
| **2. 同片多版本** | 电影：一个作品目录 ≥2 strm（实测 397 部）；剧集：同季同集 ≥2 strm（实测 1372 集） | **择优去重省空间** |
| **3. 逐集混版本** | 同季内不同集规格不一致（实测 187 个季，如 E04 是 1080p、其余 2160p） | 定位需要统一/换源的集 |
| **4. 缺集** | 季内缺前序 / 中间断层（实测 123 个季，其中 102 个「中间断层」为确证） | 补集 |
| 5. 命名异常 | 目录缺 `{tmdbid-N}`、用了长横 `{tmdb-N}`、规格段含 `[]` / `, `、HTML 实体、Windows 非法字符 | 修命名 |
| 6. 孤立字幕 | 无同名 strm 的字幕 | 归位或清理 |
| 7. 跨库对照 | 分享已整理 vs 转存已整理 的差集 | 查两库不一致 |
| 8. 扫描错误 | 单条读取失败 | 排查权限/命名怪例 |

## 画质比较（9 维）

默认顺序（分出高下即停）：

```
分辨率 → 动态范围 → 资源类型 → 视频编码 → 色深 → 帧率 → 音频 → 声道数 → 发布组(默认关)
```

| 顺序预设 | 内容 | 适合 |
| --- | --- | --- |
| `best`（默认） | 分辨率优先，片源只在同分辨率内决胜 | 一般情况：4K 胜 1080p |
| `remux` | 片源优先，Remux/BluRay 压过 WEB-DL | 原盘党：1080p Remux 胜 4K WEB-DL |
| `compact` | 分辨率 → 编码 → 音频 | 想留省空间版本 |

```bash
python nankeep.py plan --kind movie --order remux
python nankeep.py report --order resolution,source,dolby,vcodec   # 也可自定义
```

**实测识别覆盖率（全库 1721 条电影规格抽样）**

| 维度 | 识别率 | 维度 | 识别率 |
| --- | --- | --- | --- |
| 分辨率 | 99.4% | 声道数 | 67.3% |
| 视频编码 | 98.5% | 帧率 | 55.8% |
| 音频规格 | 97.9% | 色深 | 49.8% |
| 资源类型 | 71.1% | 发布组 | 47.3% |
| 动态范围 | 55.0% | | |

> 未识别率高的维度**不是解析 bug**，是文件名本身没写（例如 72% 的文件不写片源类型、大量片子不写 SDR/声道）。

### 置信度（决定「该不该动手」）

每条择优都带 `confidence`，报告与计划里都有：

| 档 | 含义 | 建议 |
| --- | --- | --- |
| `high` | 分辨率/片源/动态范围/编码分出胜负 | 可放心执行 |
| `medium` | 色深/帧率/音频/声道分出胜负 | 建议抽查 |
| `tie` | **各维全等 → 等价版本** | **最安全的去重对象**，任删其一 |
| `weak` | 仅晚期维度勉强分出 | 人工复核 |

```bash
python nankeep.py plan --kind movie --min-confidence high   # 只出最有把握的
```

### 与 StrimKeep 的两处关键差异

1. **`unknown=skip`（默认）**：某维「一方未识别」时**按打平跳过**，不当成最差。
   StrimKeep 把「未识别」排最后一档，于是**命名更详细的版本会系统性获胜**——
   质量相同的两个版本，只因一个多写了 `10bit` 就判它更优，从而可能删掉另一个。本工具默认规避。
   要 StrimKeep 口径：`--unknown worst`。
2. **多了两维**：`vcodec`（视频编码）与 `channels`（声道数）。StrimKeep 不比较这两维，但 NanShare 命名里都有，且区分度大。

## 安全边界

- **默认全程只读**，不写 NAS。`apply` 也只打印，不实际删除。
- `plan` 为每个待删 strm 记录指纹：`(dev, inode, size, mtime_ns[, ctime_ns]) + sha256`。
  `apply` 执行前**逐文件重新指纹比对**，任何变化即**中止并提示重扫**（防扫描后目录被人动过还照删）。
  以下情形一律拒绝（返回「不可校验」→ 跳过，不删）：非绝对路径、含 `..`、**符号链接**、
  **后缀不是 `.strm`**、大小超 64KB、读取前后身份不一致。
- 真删有两条路，**都不需要 root**：
  1. **Web 层**（推荐）：容器内以 uid 1000 直接 `os.unlink`。已在飞牛 fnOS 实测通过 ——
     fnOS 把 `Administrators` 组（gid 1000）授予了存储层权限，所以 `1000:1000` 能删 `root:root 700` 库目录里的文件，
     `--privileged` / root / `cap_add` 一概不需要。
  2. **CLI**：`--emit-sh` 导出脚本，默认 `DRY=1` 只打印，确认真删手动改 `DRY=0`。
     **这是人为的摩擦闸门，不是权限要求** —— 让「删」必须经过一次显式编辑。
- **进程闸门，不是权限闸门**：安全靠「扫描不删、复核才删、只有显式确认才删」的操作分级（见下），
  而不是靠把删除能力锁起来。

## 操作分级（照抄 StrimKeep 的成熟模型）

| 档位 | 动作 | 会不会删文件 |
| --- | --- | --- |
| **只读** | `scan` / `report` / `/api/status` / `/api/report` | **绝不删** |
| **建议** | `plan` / `/api/task/plan`——只是产出「待删清单 + 指纹」 | **绝不删** |
| **复核** | `apply`（无 `--emit-sh`）/ `/api/plan/{id}/preview`——重算指纹、报告差异 | **绝不删** |
| **人工确认** | Web 端「真删」按钮（需先复核通过 + 精确输入确认短语）/ CLI 改 `DRY=0` | 到这一档才可能删 |
| **执行** | 只在上一档完成后才允许；带 `mutation_lock` 互斥、逐文件错误累积 | 删 |

配合的三条硬约束：

- **低置信度不删**：只有 `exact`（各维全等）与 `normalized`（归一化后唯一胜出）允许执行；
  `ambiguous`（多版本并列）/ `unique_fallback`（仅凭「目录里只有一个视频」）/ `none` **一律拒绝**。
- **执行前重验**：`plan` 里每个文件都带指纹，执行前逐文件重算；
  计划与磁盘实际状态不一致 → **中止并提示重扫**。
- **fail-safe**：若「保留版」的指纹校验失败，**放弃删除对应的待删文件**（宁可漏删，绝不误删）。

### 删除链（11 道闸门）

```
1  登录（HMAC 签名 Cookie）
2  CSRF（Origin / Referer 同源校验）
3  计划存在 + 状态 == pending
4  计划未过期（TTL，默认 120 分钟）
5  规则指纹一致（配置没被改过）
6  确认短语精确匹配
7  payload 完整性（计划内容未被篡改）
8  执行前指纹复核（validate_plan）
9  fail-safe：保留版指纹必须可校验
10 全局互斥锁（fcntl.flock，同一时刻只有一个任务能写）
11 一次性消费（执行后状态置 done，不可重跑）
```

## 配置

`nankeep.config.json`：

```json
{
  "share_root": "//YOUR-NAS/emby/strm/nanshare",
  "out_dir": "out",
  "quality": {
    "unknown": "skip",
    "order": ["resolution","dolby","source","vcodec","bitdepth","fps","audio","channels","release_group"],
    "enabled": { "release_group": false, "source": true, "...": true },
    "tiers": { "resolution": ["8K","4K","2K","1080p","720p","SD","未识别"], "...": [] },
    "release_groups": []
  }
}
```

- `unknown`：`skip`（默认，保守）| `worst`（StrimKeep 口径）
- `enabled` / `order` / `tiers`：逐维开关、比较顺序、档位高低（数组靠前 = 更优）
- `release_groups`：维护后把 `release_group` 打开才生效

## 设计约束（沿用本项目约定）

- 给 NAS 拼路径**禁用 `os.path.join`** → 一律 f-string 手拼 `/`，避免 Windows 反斜杠混入。
- **stdlib only**，无第三方依赖；脚本 LF、UTF-8。
- **治理身份 = 目录的「片名 + 年份」**，tmdbid 仅作辅助 —— 防止错误的 tmdbid 把 A 与 B 合并。
- **Windows 平台的指纹坑**：`os.stat().st_ctime_ns` 是「创建时间」，而 `os.fstat().st_ctime_ns` 会被填成 mtime，
  两者天然不等 → 做身份比对时 Windows 必须剔除 `ctime`，否则指纹永远对不上（已按平台分支处理）。

## 已知边界

- 剧集「缺前序」在**文件名用全剧连续集号**时是假阳性；工具用「文件名季号 ≠ 目录季号」+「已有区间比缺失区间还短」
  两个启发式识别，只报「中间断层」为确证，并在备注列出。仍有少数命名怪例可能漏判。
- **5.4% 的 strm 完全没有规格段** → 这些参与择优时各维全打平，只能靠人工或解 302 取真实源名/体积。
- **体积择优未做**：strm 本身无体积，要比体积需解 302 取 115 真实文件大小 —— 会引入大量网络请求，属下一步。
- `转存已整理` 侧尚未实测（结构与分享侧镜像，`--lib both` 可跑）。
- 本工具只处理 strm 的**择优与重复**；分类归位、tmdbid 补写、字幕语言码归位仍是既有技能的事。
- **fail-safe 的次优点**：若最优版本指纹校验失败，计划保留它、执行时跳过 —— 安全但不够优
  （更优做法是回退到次优可校验版本）。已记录，待改。

## 开发与测试

```bash
# stdlib 单测（54 例：解析/指纹/九维/缺集/命名/会话/CSRF/互斥锁/任务总线/计划守卫/fail-safe/渲染）
python -m unittest discover -s tests -v

# 端到端（45 项 HTTP 断言：登录→CSRF→扫描→报告→计划→复核→dry-run→确认短语→真删→一次性→审计）
python tests/e2e.py
```

CI（`.github/workflows/docker.yml`）在推 `v*` tag 时先跑上面两套测试，全绿才构建并推送双架构镜像。

**零依赖**：Python stdlib only，没有 `requirements.txt`，也没有 `pip install` 步骤 —— 镜像构建因此只有 COPY 和一条 `apt-get install tzdata`。

## 许可

MIT（见 `LICENSE`）。

删除安全模型的设计参考了 [StrimKeep](https://github.com/xcgtb/StrimKeep)（MIT）——
上游工具不同（它是 TgtoDrive，本项目是 NanShare），nankeep 的代码为独立实现，
沿用其「操作分级 + 执行前复核 + 低置信度不删」的思路。
