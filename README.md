# nankeep —— NanShare strm 库精简工具

针对 NanShare 输出的 `\\<你的NAS>\emby\strm\nanshare` 的「分享已整理 / 转存已整理」双库做**只读优先**的精简：

```
扫描 → 规格解析 → 多版本择优 / 逐集混版本 / 缺集 / 命名异常 / 孤立字幕
     → 双库对照（完整度优先 → 画质 → 平局留分享）
     → 报告 → 计划 → 执行前指纹复核 → （精确确认后）删 strm + 网盘源文件 + 空目录
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

# 2) 出精简报告（复用索引，秒出）
python nankeep.py report --lib share --use-cache --top 50
#    → out/report.md  +  out/analysis.json

# 3) 生成精简计划（默认电影；含文件指纹）
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

CLI 只能在工作机的终端里跑。Web 层把同一套精简逻辑（**完全复用 `nankeep.py`，不是重写**）
包成一个常驻服务，部署在 NAS 上后用 Lucky 反代出去，手机/公司/任何地方打开浏览器就能用。

```
容器 nankeep（python:3.12-slim，默认 root —— 删 CD2 挂载点上的源文件需要）
   ├─ /library  ←  /path/to/nanshare                  （读写：删除需要写权限；改成你的真实路径）
   ├─ /cloud    ←  /path/to/emby/strm/CloudNAS:rslave （只启用 remote 时需要；删网盘真实文件）
   ├─ /data     ←  ./data                              （SQLite 计划+审计 / 索引缓存 / 互斥锁 / settings.json）
   └─ /app/nankeep.config.json ← ./config.json:ro       （画质档位 / 双库策略，改完重启即生效）
        │
        └── :8322 ──→ iKuai 上的 Lucky 反代 ──→ https://<你的域名>
```

### 部署（NAS 上）

不需要 `git clone`，也不需要本机构建 —— 镜像由 GitHub Actions 构建好推到 ghcr，直接拉。

```bash
mkdir -p /opt/nankeep && cd /opt/nankeep
# 放入仓库里的 docker-compose.yml，改四处：NANKEEP_PASSWORD、① 库路径、② 网盘挂载点、NANKEEP_ALLOWED_ORIGINS
cp nankeep.config.json config.json                  # 配置文件挂载点（不挂也行，用镜像内默认）
mkdir -p data && sudo chown -R 1000:1000 data      # 兜底：改回 uid 1000 运行时 /data 仍可写
docker compose pull                                 # 拉取 ghcr.io/utterliar1/nankeep:<版本>
docker compose up -d
docker compose logs -f                              # 看到 "web 已启动" 即成功
curl -s localhost:8322/api/health                   # {"ok":true,"version":"0.4.1",...}
```

改 `config.json`（比如把 `strategy.tie_keep` 从 `share` 改成 `skip`）后 `docker compose restart`
即生效。规则指纹（`rule_sig`）随之变化，**旧计划会自动作废**，不会拿新规则去删旧结论。

> 只想用只读/精简功能、不删网盘文件时：把 compose 的 `user: "0:0"` 改回 `"1000:1000"`，
> 并删掉 `/cloud` 那条 volume —— 前四项功能一个都不少（只有「删网盘源文件」不可用）。

**镜像**：`ghcr.io/utterliar1/nankeep:latest`。
推 `v*` tag 会触发 CI 先跑测试、再构建 amd64+arm64 双架构并推送，所以 NAS 架构通常直接可用
（`docker compose pull && docker compose up -d` 即升级）。用 `:0.4.1` 这样**不带 `v`** 的版本号锁版本，
`:latest` 跟随主线。

> **包可见性**：默认是 Private，匿名 `docker pull` 会报 `denied`。本仓库代码本就公开，
> 建议在 GitHub 的 Packages 页把 `nankeep` 改为 **Public**（Package settings → Change visibility），
> 之后 NAS 直接拉即可。若要保持 Private，就在 NAS 上先登录一次：
> `echo <PAT> | docker login ghcr.io -u utterliar1 --password-stdin`（PAT 需要 `read:packages` 权限）。

### 发布与升级

```
改代码 → git commit → git tag v0.4.1 && git push origin v0.4.1
        └─ CI：测试 → 构建 amd64+arm64 → 推 ghcr（tag 0.4.1 + latest）
NAS：  改 compose 里的 image 版本 → docker compose pull && docker compose up -d
```

**不需要在 NAS 上 `docker build`**。仓库里的 `Dockerfile` 只服务于 CI 与排障时的本地构建。

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
| `NANKEEP_SETTINGS` | `$NANKEEP_DATA/settings.json` | 界面设置覆盖层（见下「三层配置」） |
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
| 前端域名 | `nankeep.你的域名`（把 `*.你的域名` 泛解析指到公网入口） |
| 后端地址 | `http://<NAS 的局域网 IP>:8322` |
| 协议 | HTTPS（Lucky 自动签证书） |

**反代后必做**：把浏览器里访问的完整域名填进 `NANKEEP_ALLOWED_ORIGINS`，否则写请求
（扫描/生成计划/删除）会被同源校验挡掉 —— 这是防 CSRF 的设计，不是 bug。

### 页面能做什么

界面是单页多视图（侧栏切换 + 亮/暗双主题 + 移动端抽屉），零外部依赖
（不引 CDN、不引字体，`static/` 全本地）：

| 视图 | 内容 | 会删文件吗 |
| --- | --- | --- |
| **总览** | 两库规模 KPI、按库×区的分类分布条、精简待办计数（点进报告对应章节）、最近精简时间线、计划与运行状态 | 不会 |
| **报告** | `report.md` 渲染成带章节导航的页面（吸顶目录 + 滚动高亮），支持复制/下载 Markdown | 不会 |
| **清理计划** | 计划列表 + **保留版 / 待删版并排对照**（规格摘要、版本数、置信度、择依据）；网盘源文件与待清空目录单列一栏；网盘定位失败会显式标红并说明原因；「执行前复核」逐文件重算指纹并列出不一致项 | 不会 |
| **清理计划** | **「执行删除」**：必须①复核返回 `executable` ②勾选确认 ③精确输入确认短语，三个条件同时满足按钮才可点 | **会** |
| **设置** | 九维档位/顺序/开关、精简策略（决策、平局档、配对、达标率、静默期、豁免词）、网盘清理段 —— 全部可视化编辑，带「界面 / 配置」来源标注与前后端同口径校验 | 不会（只写 `settings.json`） |
| **审计** | 每次生成计划/执行清理的留痕（可按类型或失败筛选，点开看结构化 extra） | 不会 |
| **日志** | 服务端运行日志尾部（可过滤、自动滚动开关、失败行高亮） | 不会 |

顶栏的运行状态浮层聚合了服务连接、库根可写性、索引/报告新鲜度、待执行计划数、
当前任务与版本号，以及**网盘清理是否真的能生效** —— 除「网盘清理（宿主前缀）」外还单列
「网盘挂载点」，显示 `container_prefix` 及容器内是否可见；映射三件套
（`host_prefix` → `container_prefix` → 容器内可见）任一不成立都会在这里显形，
不必等生成计划才发现配错。`GET /api/overview` 一次返回这些数据，前端只轮询这一个接口。

## 命令

| 命令 | 作用 | 关键参数 |
| --- | --- | --- |
| `scan` | 扫描两库，落 `out/library.json.gz` | `--lib share\|transcode\|both` |
| `report` | 生成 `out/report.md` + `out/analysis.json` | `--top N`（0=全部）、`--use-cache` |
| `plan` | 生成 `out/plan_<kind>.json` | `--kind movie\|tv`、`--min-confidence`、`--use-cache` |
| `apply` | 执行前逐文件指纹复核 | `--plan`、`--emit-sh` |

全局参数：`--root`（覆盖库根）、`--order`（比较顺序）、`--unknown`（未识别策略）、`--config`。

**CLI 与网盘清理**：`--emit-sh` 导出的脚本与 Web 层同语义（先删网盘源文件 → 删不净则整部跳过 →
删 strm → 自内向外删空目录），脚本默认 `DRY=1` 只打印。但要注意 **CLI 跑在宿主上**：
计划里的网盘路径是按 `remote.container_prefix` 映射过的，填了容器挂载点（如 `/cloud`）时
这些路径在宿主上不存在 → `apply` 会**直接拒绝并说明原因**（不会静默跳过）。
用 CLI 就把 `container_prefix` 留空（此时计划里的网盘路径即宿主路径）；用容器就用 Web 层。

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
| **8. 双库精简建议** | 对两库都存在的作品，**先比完整度（谁不缺集）→ 再比画质 → 全平局则留分享库** | 生成可审阅的跨库待删动作 |
| 9. 扫描错误 | 单条读取失败 | 排查权限/命名怪例 |

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

## 双库精简：怎么裁决

「分享已整理」与「转存已整理」是同一批片的两种存在形式。转存侧是本地的 strm，
**它的内容指向网盘上的真实文件** —— 所以淘汰转存侧才真正释放网盘空间；分享侧是 302 直链，删了只省 NAS 上的一个文本行。

裁决按固定三级顺序（**不跳级、不加权**）：

```
① 完整性（前置闸门，与画质无关）
     ├─ 一侧缺集 / 不完整           → 只处理「残次品」（删掉被完整侧完全覆盖的残缺副本）
     └─ 两侧一样完整（电影恒成立）  → 进第 ② 级
② 画质：按九维顺序逐维比（见上），分出高下即停
     ├─ 一胜一负                    → 留高的（quality_first）
     └─ 九维全平                    → 进第 ③ 级
③ 平局：tie_keep 决定
     ├─ "share"（默认）             → 留分享、淘汰转存 ← 同样画质下优先释放网盘空间
     ├─ "local"                     → 留转存、淘汰分享
     └─ "skip"                      → 谁都不删
```

剧集另有两条**集数优先**的硬规则（优先于画质，见 `season_verdict`）：分享侧更全时**绝不删分享**
（独有集号不可替代）；转存侧集数更多则淘汰分享。只有**集数相同**才允许「转存更优 → 淘汰分享」。

**平局档默认 `share`** 是与 StrimKeep 的一处刻意不同：两版画质读音完全一致时，留分享库
以腾出网盘空间 —— 这正是双库精简的主要目的。想变成「拿不准就不动」就设成 `skip`。

**可比性闸门（`unknown=skip` 时）**：如果双方**没有任何一维能读出档位**，这不算「打平」，
而是**根本没法比** → 一律不动作，**绝不套用平局档**。规格读不出来 ≠ 谁更差；此类作品只能在报告里人工判断
（解 302 看真实源名/体积）。

**剧集是季为单位**，且分享侧要**在分胜负的集里胜出 ≥ `season_replace_ratio`（默认 0.9）、
且集数不少于转存侧**才会整季替换；整季一集都分不出胜负 → 按 `tie_keep` 处理。
多季剧集默认 `multi_season_protect: compare`，避免拆散整季集合。

**配对**默认按「片名 + 年份」（`match: "title_year"`）；`tmdb_first` 会在 tmdbid 命中时优先按 tmdbid 配对，
但**要求年份一致且候选唯一**，否则回落 —— 防重拍片串号导致跨片删错。`strategy.exempt_keywords`
里写的词命中的作品一律跳过。

## 网盘源文件与空目录清理

删掉转存侧的 strm 只是「不看了」，**网盘上的真实文件还在占空间**。要真正省空间，
必须连带删掉 strm 指向的网盘文件 —— 也就是 CD2 / OpenList 挂载点上的那个 mkv。

转存侧 strm 的内容是一条**宿主路径**（CD2 的挂载点路径），nankeep 把它按下述三步映射成容器内路径后直接 `unlink`：

```
strm 内容  /vol1/1000/emby/strm/CloudNAS/CloudDrive/电影/…/xxx.mkv
                 └─────── host_prefix ──────────┘└─ 相对路径 ─┘
                        ↕  前缀替换
容器内     /cloud/CloudDrive/电影/…/xxx.mkv
                    └─ container_prefix ─┘
```

配置在 `config.json` 的 `remote` 段：

```json
"remote": {
  "enabled": false,
  "host_prefix": "/vol1/1000/emby/strm/CloudNAS",
  "container_prefix": "/cloud",
  "delete_source": true,
  "delete_sidecars": true,
  "clean_empty_dirs": true
}
```

| 键 | 说明 |
| --- | --- |
| `enabled` | 总开关。`false` 时计划里不出现任何网盘动作，只删 strm |
| `host_prefix` | strm 里那条宿主路径的前缀。**照抄 compose 里 volume 的左侧、一字不差**，否则映射失败该片直接跳过 |
| `container_prefix` | 上面挂在容器里的路径，与 compose 的 volume 右侧对应 |
| `delete_source` | 删掉 strm 指向的网盘真实文件（mkv/mp4 等） |
| `delete_sidecars` | 连带删掉**同目录**下同名的附属文件（`.nfo` 海报、外挂字幕、`.ass` 等） |
| `clean_empty_dirs` | 删完文件后，把**因这次删除而变空**的目录逐层删掉 |

**附属文件与空目录的行为边界（重要）**：

- 只动**这次被删文件所在目录**里的附属文件，**不跨目录扫**，也**不碰原本就存在的无关文件**。
- 空目录只删**「本次删除造成的空」**：判定时把同批已判删的文件与目录视同不存在，从深到浅逐层检查
  （`Season 01` 先空 → 删掉后内层目录也可能变空 → 继续上删）—— 一直删到**第一个原本就非空的目录**为止，
  绝不动分类根目录（如 `电影/其他电影/`）本身。
- **网盘侧先删、NAS 侧后删**：网盘文件删失败（不可读、已被别人删掉、FUSE 报错）时，
  **该作品的 strm 一律保留** —— 宁可留下一个指向死链的 strm 让人看见，也绝不留下
  「strm 没了但网盘文件还在悄悄占空间」这种查不出来的浪费。
- **网盘源文件定位失败 → 整份计划作废**（不是静默跳过）。`remote.enabled` 为真时，
  若某个转存侧 strm 是挂载点路径却匹配不上 `host_prefix`（多半是前缀写错了），
  或压根没配 `host_prefix`，该动作会被标成 `remote_problem`，
  `validate_plan` 直接在复核里给出原因、`executable` 变 `false`，删除按钮点不动。
  理由：这种情况下删 strm 省不下任何空间，还会让片子在 Emby 里凭空消失 ——
  与其让用户对着「复核不通过」猜，不如明说。
  （分享侧的 302 直链 strm 不参与这条判定：它本来就没有网盘源文件可删。）
- 与 strm 同名的**字幕跟删**（这条是既有行为，与 remote 无关）：删 strm 时会一并删掉
  **去语言码后同基名**的字幕；与「保留版」共享的字幕不动。

> **为什么容器要 root**：CD2 / OpenList 通过 FUSE 挂载，文件是 `root:root 755` 且带 `default_permissions`
> —— uid 1000 实测 `Permission denied`，即便只是 `touch`。所以 compose 里是 `user: "0:0"`。
> 不启用 remote 时可以改回 `1000:1000`（删 NAS 上的 strm 仍然可以 —— 见下「安全边界」）。

> **为什么 `rslave` 不能省**：CD2 的 `CloudDrive` 是挂在 `CloudNAS/CloudDrive` 上的**子挂载**
> （`CloudFS ... type fuse`）。挂载传播不带 `rslave` 时，容器里 `/cloud` 只有父目录的条目，
> 看不到子挂载内容 —— 表现为「容器里是空目录」。带了 `rslave` 后 CD2 重挂载也能自动传播进来。

> **回收站**：网盘删除只让文件进回收站，**不立即释放空间**。要真正腾出空间需另行清空回收站
> —— nankeep 刻意不做这一步（不可逆、易误伤）。


## 安全边界

- **默认全程只读**，不写 NAS。`apply` 也只打印，不实际删除。
- `plan` 为每个待删 strm 记录指纹：`(dev, inode, size, mtime_ns[, ctime_ns]) + sha256`。
  **网盘侧另记一份轻量指纹**：`(size, mtime_ns)` —— 影片动辄几十 GB，**不读 sha256**，
  只在执行前确认「文件没被人动过」。
  `apply` 执行前**逐文件重新指纹比对**，任何变化即**中止并提示重扫**（防扫描后目录被人动过还照删）。
  以下情形一律拒绝（返回「不可校验」→ 跳过，不删）：非绝对路径、含 `..`、**符号链接**、
  **后缀不在允许集内**、读取前后身份不一致。
  **体积上限按扩展名分档**：`.strm` 64 KB（它就是几十字节的文本，超了必是异常内容）；
  字幕 64 MB（`.ass` 常规几百 KB、`.sup` 蓝光图形字幕能到几十 MB）。
  一开始这里一律用 64 KB，结果真实库上 **21% 的字幕被判「不可校验」**，
  复核直接报「计划缺少指纹快照」，计划永远删不动 —— 已在 v0.4.0 修正。
- 真删有两条路：
  1. **Web 层**（推荐）：容器内 `os.unlink`。删 NAS 上的 strm **不需要 root** —— 已在飞牛 fnOS 实测通过，
     fnOS 把 `Administrators` 组（gid 1000）授予了存储层权限，所以 `1000:1000` 能删 `root:root 700`
     库目录里的文件，`--privileged` / `cap_add` 一概不需要。
     **只有删 CD2 挂载点上的网盘源文件才需要 root**（FUSE + `default_permissions`，见上）。
  2. **CLI**：`--emit-sh` 导出脚本，默认 `DRY=1` 只打印，确认真删手动改 `DRY=0`。
     **这是人为的摩擦闸门，不是权限要求** —— 让「删」必须经过一次显式编辑。
     导出脚本与 Web 层同语义（含网盘源文件与空目录，含「网盘删不净则整部跳过」）——
     但仅当 `remote.container_prefix` 留空时可用，见上「CLI 与网盘清理」。
- **进程闸门，不是权限闸门**：安全靠「扫描不删、复核才删、只有显式确认才删」的操作分级（见下），
  而不是靠把删除能力锁起来。`sudo` 与 `DRY=1` 都是**刻意保留的人工闸门**，不是权限必需。
- **删不干净就留着**：网盘源文件删失败 → 保留对应 strm（见上「网盘源文件与空目录清理」）。
  留下死链可见，好过留下「看不见的网盘占用」。

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
  },
  "strategy": {
    "decision": "quality_first",
    "tie_keep": "share",
    "season_replace_ratio": 0.9,
    "multi_season_protect": "compare",
    "special_action": "compare",
    "exempt_keywords": [],
    "quiet_minutes": 15,
    "match": "title_year"
  },
  "remote": {
    "enabled": false,
    "host_prefix": "/vol1/1000/emby/strm/CloudNAS/CloudDrive",
    "container_prefix": "/cloud",
    "delete_source": true,
    "delete_sidecars": true,
    "clean_empty_dirs": true
  }
}
```

- `unknown`：`skip`（默认，保守）| `worst`（StrimKeep 口径）
- `enabled` / `order` / `tiers`：逐维开关、比较顺序、档位高低（数组靠前 = 更优）
- `release_groups`：维护后把 `release_group` 打开才生效
- `strategy.decision`：`quality_first`（默认，按画质比较，完整性前置闸门始终生效）| `keep_share` | `keep_local`（后两者=无脑固定留某侧，不比较）
- `strategy.tie_keep`：九维**全部打平**时怎么办 —— `share`（默认，**留分享、释放网盘空间**）| `local` | `skip`（哪边都不删）。
  注意「全部打平」只指**可比且打平**；若双方所有维度都读不出档位，属「不可比」→ 一律不动作，不套平局档。
  （老配置里的 `tie_keep_local` 布尔仍认：`true` → `local`，`false` → `share`。）
- `strategy.season_replace_ratio`：剧集分享侧在**分胜负的集**里胜出达到该比例，且集数不少于转存侧，才替换整季；范围 `0.5` 到 `1.0`。整季一集都分不出胜负 → 按 `tie_keep` 处理
- `strategy.multi_season_protect`：多季剧集 `compare`（默认，整剧零和：分享全季达标才整体删转存）| `off`（按季独立处理）
- `strategy.special_action`：特别篇 `compare`（默认）、`ignore`，或显式 `delete`；删除仍受执行前保留版检查
- `strategy.exempt_keywords`：作品名、目录或 strm 路径命中即跳过双库动作
- `strategy.quiet_minutes`：目录最近变更后的静默分钟数，默认 `15`，避免新入库内容立即被处理
  （**做测试/演示时务必设 `0`**，否则刚建的目录会被静默期挡掉，计划全空）
- `strategy.match`：默认按片名+年份配对；`tmdb_first` 在 tmdbid 命中时优先按 tmdbid 配对，
  但**要求年份一致且候选唯一**，否则回落到片名+年份（避免重拍片/串号导致跨片删错）
- `remote.*`：见上「网盘源文件与空目录清理」

### 三层配置与设置页

生效值 = **代码默认** ← **`nankeep.config.json`**（compose 只读挂载）← **`settings.json`**（`/data`，界面上改）

- 前两层只读/半只读；**只有设置页写的那层**在 `settings.json` 里，是**稀疏覆盖**（只存改过的字段）。
- 任何一层变化都会改变 `rule_sig` → **所有旧计划立即作废**（`stale`），不会拿新规则去删旧结论。
- 设置页每一项都标了来源：**「界面」**= 当前值来自覆盖层；**「配置」**= 来自文件/默认。
  点「重置」会删掉 `settings.json`，全部回落文件值。
- 保存前会跑与后端**同一套**校验（枚举合法性、达标率 0.5~1.0、网盘前缀成对出现…），
  非法直接 400 且**不落盘**；有 `pending` 计划时会提示「保存将作废 N 个待执行计划」，需二次确认。
- 设置写请求同样过 CSRF 同源校验；`/data` 不可写时明确报错而不是静默失败。

## 设计约束（沿用本项目约定）

- 给 NAS 拼路径**禁用 `os.path.join`** → 一律 f-string 手拼 `/`，避免 Windows 反斜杠混入。
- **stdlib only**，无第三方依赖；脚本 LF、UTF-8。
- **精简身份 = 目录的「片名 + 年份」**，tmdbid 仅作辅助 —— 防止错误的 tmdbid 把 A 与 B 合并。
- **Windows 平台的指纹坑**：`os.stat().st_ctime_ns` 是「创建时间」，而 `os.fstat().st_ctime_ns` 会被填成 mtime，
  两者天然不等 → 做身份比对时 Windows 必须剔除 `ctime`，否则指纹永远对不上（已按平台分支处理）。

## 已知边界

- 剧集「缺前序」在**文件名用全剧连续集号**时是假阳性；工具用「文件名季号 ≠ 目录季号」+「已有区间比缺失区间还短」
  两个启发式识别，只报「中间断层」为确证，并在备注列出。仍有少数命名怪例可能漏判。
- **5.4% 的 strm 完全没有规格段** → 这些参与择优时各维全打平。因「不可比」闸门，**不做任何动作**；
  要人工判断就解 302 取真实源名/体积。
- **体积择优未做**：strm 本身无体积，要比体积需解 302 取 115 真实文件大小 —— 会引入大量网络请求，属下一步。
- **网盘删除只进回收站**，不立即释放空间；清空回收站是另一件事，nankeep 刻意不做（不可逆）。
- **`host_prefix` 照抄 compose volume 左侧**，一字不差。写错/写漏时映射出的容器路径不存在 →
  该片**不进网盘删除清单**（只会删 strm），不会删错东西也不会静默「strm 删了、网盘留着」。
- 本工具只处理 strm 的**择优与重复**；分类归位、tmdbid 补写、字幕语言码归位仍是既有技能的事。
  但删 strm 时会把**同名（去语言码）字幕一起删**，避免留下孤立字幕；与保留版共享的字幕不动。
- **fail-safe 的次优点**：若最优版本指纹校验失败，计划保留它、执行时跳过 —— 安全但不够优
  （更优做法是回退到次优可校验版本）。已记录，待改。

## 开发与测试

```bash
# stdlib 单测（153 例：解析/指纹（含体积分档）/九维/缺集/命名/双库精简/完整性闸门/
#                  平局档/tmdb 配对/字幕跟删/网盘路径映射与归属判定/附属文件/空目录清理/
#                  设置层三合一校验/emit-sh 语义/会话/CSRF/互斥锁/任务总线/计划守卫/
#                  fail-safe/渲染/总览聚合）
python -m unittest discover -s tests -v

# 端到端（131 项 HTTP 断言：登录→CSRF→扫描→报告→计划→双库动作→网盘侧标注→导出脚本→
#                     复核→dry-run→确认短语→真删（含网盘源文件与空目录）→一次性→
#                     设置往返与作废→网盘归属闸门→总览聚合→静态资源与穿越→审计→登出）
python tests/e2e.py
```

CI（`.github/workflows/docker.yml`）在推 `v*` tag 时先跑上面两套测试，全绿才构建并推送双架构镜像。

**零依赖**：Python stdlib only，没有 `requirements.txt`，也没有 `pip install` 步骤 —— 镜像构建因此只有 COPY 和一条 `apt-get install tzdata`。

## 许可

MIT（见 `LICENSE`）。

删除安全模型的设计参考了 [StrimKeep](https://github.com/xcgtb/StrimKeep)（MIT）——
上游工具不同（它是 TgtoDrive，本项目是 NanShare），nankeep 的代码为独立实现，
沿用其「操作分级 + 执行前复核 + 低置信度不删」的思路；界面的信息架构
（侧栏多视图 + 总览 KPI + 分布条 + 审计时间线）同样受其启发，样式与实现自写。

