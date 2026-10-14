# nankeep —— NanShare strm 库治理工具

针对 `\\<你的NAS>\emby\strm\nanshare` 的「分享已整理 / 转存已整理」双库做**只读优先**的治理：

```
扫描 → 规格解析 → 多版本择优 / 逐集混版本 / 缺集 / 命名异常 / 孤立字幕 / 跨库对照
     → 报告 → 计划 → 执行前指纹复核
```

> 定位：站在 NanShare **下游**。nankeep 不生成 strm、不改 config、不参与 302 播放，只读你已经整理好的目录。

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
#    → out/apply_movie.sh（拷到 NAS 上 sudo bash，默认 DRY=1 只打印）
```

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
- 真删走 `--emit-sh` 的输出脚本，默认 `DRY=1` 只打印；确认真删要手动改 `DRY=0`，
  在 NAS 上 `sudo bash` 运行（NAS 删除需 root，SMB 直删会 err=5）。

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
