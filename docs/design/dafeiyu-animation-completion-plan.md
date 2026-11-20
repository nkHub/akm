# dafeiyu 桌宠：补齐上游 106 段动画 — 实施计划

> 状态：**待执行**（2026-10-29 评估 + PoC 半程，编码端已验证、解码端被本机环境卡住）
> 目标读者：下次接手执行的 agent 或本人。每个阶段都有可验证的验收标准，按阶段推进、逐段验收。

## 一、结论先行

- 上游 `PC2005-cloud/dsh-pet` 共 **106 段动画**（不是 25 段）。`dafeiyu` 当前只接了 **14 段源视频**（16 个 clip）。
- **缺口 92 段**。这不是 bug，而是当初有意挑的最小可用集；真正影响体感的是「待机随机小动作只有 4 个可选，上游这一维度有 45 个」。
- 补齐的**唯一障碍是素材体积**：106 段 webm 共 51.8MB，而当前 14 段转成 WebP 帧序列已 54.6MB。92 段若沿用 WebP 帧序列，预计 **405MB**，不可行。
- 因此方案定为：**换播放后端为 HEVC-with-alpha `.mov` + `AVPlayerLayer`**，预计全量 106 段可控制在 **60~90MB**，画质不降。
- 已确认口径（用户决定）：**素材随插件市场 zip 一起分发**（与当前 README「不进 zip」的写法冲突，阶段 5 修改）。

## 二、当前事实基线（执行前请复核，勿凭记忆）

| 项 | 数值 | 复核命令 |
|---|---|---|
| 上游动画总数 | 106 段 | 取 `dsh-pet/assets/config.jsonc` 全部池子并集，与 `assets/webm/` 文件数交叉 |
| 上游 106 段总体积 | 51.8 MB（均 500KB/段，最大「放烟花」1299KB） | GitHub tree API 的 `size` 求和 |
| 现有素材 | 16 clip / 3444 帧 / 54.6MB（16.24 KB/帧） | `assets/pet-manifest.json` + `du -sh assets/pet` |
| 已用上游源视频 | 14 段 | `clips.*.source.file` 去重 |
| manifest 钉的上游 commit | `549fff2ea33c2acfb16612c76912c6137da4a758` | `assets/pet-manifest.json` → `assetSource` |

### 分组缺口对照

| 上游分组 | 上游 | 已接 | 未接 |
|---|---|---|---|
| 待机 / 转向 | 2 | 1（待机呼吸休闲） | 1（东张西望） |
| 拖拽 | 1 | 1 | 0 |
| 点击回应 | 5 | 2 | 3（开心跃动 / 害羞惊讶 / 傲娇生气） |
| 移动（漫游） | 3 | 0 | 3（螃蟹走路 / 原地漂浮踏步 / 原地左转奔跑） |
| 小动作 | 18 | 2（超大伸懒腰、哈欠连天） | 16 |
| 玩耍 | 27 | 1（鲸鱼吐泡泡特效） | 26 |
| 吃什么 | 12 | 1（吃Token） | 11 |
| 时节 | 21 | 0 | 21 |
| 文字 | 2 | 0 | 2 |
| 余额事件 | 6 | 0 | 6 |
| 碎碎念 | 3 | 0 | 3 |
| 工作状态 | 6 | 6（全接） | 0 |
| **合计** | **106** | **14** | **92** |

## 三、代码落点（改造前必读）

| 关注点 | 位置 | 说明 |
|---|---|---|
| 素材清单 | `plugins/dafeiyu/assets/pet-manifest.json` | `clips.<name>`：`frames[]` / `frameMs` / `loop` / `source{file,sha256,startFrame,endFrame}`；顶层 `stateMap` / `workingActivityMap` / `idleMicroClips` |
| 帧缓存 | `pet/pet_app.py:183-260` `FrameCache` | 按 clip 懒加载，首次访问读全部帧；`has` / `frames` / `frame_ms` / `loops` |
| 帧推进 | `pet/pet_app.py:1178-1320` `_tickOnce` | `frame_index` 推进、`loop=False` 停在最后一帧、「播完一轮才切」的排队语义都在这里 |
| 上屏 | `pet/pet_app.py:1322-1325` `_showFrame` | 唯一一处 `image_view.setImage_` |
| 角色图层 | `pet/pet_app.py:2291+` `PetImageView(NSImageView)` | 换成 AVPlayerLayer 宿主 view 时保持鼠标事件透传 |
| 挤压 | `pet/pet_app.py:1548` `_applySquash` | **只改 `view.frame`、不碰 CALayer/anchorPoint**（历史踩坑：改锚点导致图层永久左移）。AVPlayer 宿主 view 同样适用 |
| 交互池 | `pet/pet_app.py:78-80` | `CLICK_CLIPS`（2 个）、`IDLE_DECOR_CLIPS`（4 个） |
| 随机小动作入口 | `pet/pet_app.py:869` | `random.choice(IDLE_DECOR_CLIPS)` — 体感缺口的直接来源 |
| 菜单中文名 | `pet/pet_app.py:84-101` `CLIP_LABELS` | 缺失键回落显示 clip 原名，新增素材不改也能用 |
| 播放优先级 | `pet/pet_app.py:_active_clip` | 手动锁定 > 一次性动作 > 状态动作 |

## 四、分阶段执行

### 阶段 0：环境与素材就位

1. 装解码器（**必须 Python 3.12**；系统 Python 3.9 + pip 21 拿不到 av wheel）：
   ```bash
   /Users/nk/Desktop/Ecology/ccs/.venv/bin/pip install --target $PI_SCRATCH_DIR/poc/libs av
   # 或 brew install ffmpeg（含 libvpx-vp9）
   ```
2. 拉上游素材。**不要用 raw.githubusercontent 逐个 curl**（实测 ~21KB/s，51.8MB 要 40 分钟以上）：
   ```bash
   git clone --depth 1 --filter=blob:none --sparse https://github.com/PC2005-cloud/dsh-pet
   cd dsh-pet && git sparse-checkout set dsh-pet/assets/webm dsh-pet/scripts
   ```
3. 生成素材清单（文件名 / 字节数 / sha256），落盘到 `$PI_SCRATCH_DIR/poc/webm-index.json`。

**验收**：本地 106 个 webm，文件名与 config.jsonc 池子名 100% 对应，sha256 清单生成。

### 阶段 1：单段端到端 PoC（决定整体方案生死）

任选一段（建议「放风筝.webm」1299KB，最大最坏情况）：

1. **解码验证**：确认 pix_fmt 是 `yuva420p`，且 alpha 通道**不是全 255**（真的带透明信息）。
2. **编码验证**：喂上游 `scripts/hevc_alpha_encoder.swift`（已在本机编译通过，见下方"已验证"）：
   ```bash
   ffmpeg -c:v libvpx-vp9 -i 放风筝.webm -an -f rawvideo -pix_fmt bgra pipe:1 \
     | ./hevc_alpha_encoder kite.mov 640 360 24
   ```
3. **播放验证**（PyObjC + `pyobjc-framework-AVFoundation`，最小验证窗口）——四件事必须全过：
   - [ ] 透明通道正确（不是黑底）
   - [ ] `view.frame` 挤压变形生效（模拟 `_applySquash`）
   - [ ] `AVPlayerItemDidPlayToEndTime` 回调能拿到（等价「播完一轮」）
   - [ ] `pause()` 能稳定停在最后一帧（等价 `loop=False` 语义）
4. 记录单段 mov 体积与实际耗时，外推 106 段的总体积与总时间。

**验收**：上述四项全部打勾 + 得到 106 段的体积/耗时外推值。
**若任一项不过** → 立即停，转「备选路线 B」（见第六节），不要硬推阶段 2。

### 阶段 2：渲染层改造（现有 16 段先跑通，不接新素材）

原则：**双后端共存**，新增 mov 播放后端但保留 WebP 帧序列路径，用 manifest 的字段切换，避免一次性断掉现有能力。

1. `FrameCache` 抽出「clip 数据源」概念，`frames()` 后端改为 mov 时不再解码帧。
2. `_tickOnce` 的帧推进改为「播放位置推进」，**保持三处既有语义等价**：
   - `loop=True` 循环播放（现为 `frame_index` 归零）
   - `loop=False` 播完停在最后一帧
   - 「当前动作播完一轮的轮末才切下一个」的排队（`pet_app.py:843` 附近注释描述的设计不变量，不可破坏）
3. `reduce_motion`（固定姿势）→ `pause` 在第 0 帧；拖拽中暂停、落地继续。
4. `_applySquash` 不动（仍改 view.frame）。

**验收**：现有 16 段的观感与改造前一致（真机截图/录屏对比）；`pytest` 全绿；无「卡住不动」类回归（历史上这个插件最容易回归的就是动画被强制中断/永久停摆）。

### 阶段 3：批量转码全量 106 段

1. 统一转码参数：裁切窗口与缩放规则必须与现有素材一致（现有为 `crop [100,0,412,344]`，上游原生 640×360 → 需确认「crop 后 412×344」与「640×360 整体」在显示尺寸上的差异，**优先保持现有观感**，不要为了省事改变角色在窗口里的比例）。
2. 批量转码 + 断点续跑（输出比源新则跳过）。
3. 双向校验：段数 = 106、逐段帧数一致、首末帧 alpha 非全不透明、抽样帧与 WebP 版本像素接近。

**验收**：106 段 mov、总体积 ≤ 100MB、校验全通过、抽 5 段真机播放无异常。

### 阶段 4：接线（让 92 段真正能被触发）

1. `pet-manifest.json` 扩展 `clips`，每段带 `source`（文件名+sha256）与 `loop` 标记。
2. `CLIP_LABELS` 补中文名（92 个）。
3. **随机动作池分类**（这是体感收益最大的一步）：按上游分组落成 `小动作 / 玩耍 / 吃什么 / 时节 / 文字`，带权重，替换现在 `random.choice(IDLE_DECOR_CLIPS)` 的 4 选 1；沿用上游的「同档不连续重复」思路。
4. 事件类接入：
   - 余额 6 档 → 按已用百分比分档（`index = p===100 ? 5 : floor(p/20)`）
   - 碎碎念 3 段 → 现在 AI 选动作的候选池扩充
   - 漫游 3 段 → `drift_enabled` 开启时的行走动画
   - 点击回应 3 段 → `CLICK_CLIPS` 扩到 5
   - 东张西望 → 转向
5. `screen lock` / 面向左右：注意上游「文字类动画不镜像」的规则（`noMirror`），本地目前**没有**镜像功能，若后续加镜像必须补这条。

**验收**：右键菜单可点播全部 106 项；随机小动作连续观察 5 分钟能出现多个不同动作且不连续重复；余额/碎碎念/漫游各自命中对应段；`pytest` 全绿。

### 阶段 5：分发与文档同步（按项目规则，不可省）

1. 改「素材不进 zip」口径 → 素材随插件市场 zip 分发；确认 zip 体积可接受（目标 ≤ 100MB）。
2. `plugins/dafeiyu/README.md`：素材表从「16 组动作 / 3444 帧 / 59MB WebP」更新为新数字与格式；补 mov 转码管线说明。
3. 根 `README.md`：同步素材描述。
4. `docs/logs.md`：新版本追加在最上方（只写增量，不复述历史）。
5. 补 `scripts/import_dshpet_webm.py`（见第七节问题 1），让素材链路可复现。
6. 知识库沉淀一条：范围=桌宠素材格式选型与上游对照；结论=为什么不能沿用 WebP 帧序列、106 段分组、转码参数。

**验收**：装 zip 后直接有全部动画；文档数字与实现一致；知识库能检索到该条目。

## 五、已验证可复用的事实（别重复踩）

- **HEVC-with-alpha 编码端在本机可用**：`xcrun swiftc -O -suppress-warnings -framework AVFoundation -framework CoreVideo -framework VideoToolbox hevc_alpha_encoder.swift -o hevc_alpha_encoder` **14 秒编译成功**，产物 96KB。走 `AVAssetWriter` + `AVVideoCodecType.hevcWithAlpha` + `kVTCompressionPropertyKey_TargetQualityForAlpha = 0.75`，stdin 吃 32BGRA 原始帧，参数 `<out.mov> <w> <h> <fps>`。macOS 14.1.2 实测支持。
- **挤压实现不碰 CALayer**（`_applySquash` 只改 `view.frame`），换播放后端不影响它。
- **本地无镜像功能**，无需处理 `scaleX`
- 环境事实：本机 macOS 14.1.2；无 ffmpeg / 无 cv2 / 无 PyAV；有 `xcrun swiftc` 与 `avconvert`；**`avconvert` 不支持 VP9，别指望它**。

## 六、备选路线 B（阶段 1 PoC 失败时启用）

保持 WebP 帧序列、不改架构，只降素材参数把 92 段塞进来：
- 实测维度：分辨率（412 / 320 / 280）× 帧率（24 / 15 / 12）× quality（68 / 58 / 52），量出「每帧 KB」曲线后再定组合
- 目标总量 ≤ 120MB；**12fps 以下会有明显顿感，Retina 下分辨率不能低于 360 宽**（显示宽 206pt，2x 需要 412px）
- 代价：观感下降 + 切 clip 时解码 241 帧的加载延迟；收益：零架构风险，只改 manifest + 素材

## 七、已知问题与风险（执行时一并处理）

1. **导入管线缺失**：`assets/ASSET_LICENSE.md:19` 提到的 `scripts/import_dshpet_webm.py` **仓库里不存在**，git 历史也无删除记录 → 转码脚本要从头写（阶段 5 第 5 条）。
2. **重复素材**：`waiting` 与 `working_search` 是同一源视频、同一帧窗口的完整拷贝，白占约 3.6MB，可合并指向同一份数据。
3. **AVPlayerLayer 与窗口特性**：无边框透明窗、始终置顶、多屏、`_clampToScreens` 越界夹取，都要在阶段 2 真机验证，历史上有过「坐标逻辑全对但窗口越界显示异常」的坑。
4. **HEVC alpha 在 Retina 缩放下的清晰度**：需要真机对比，必要时把编码分辨率提到 2x。
5. **转码耗时**：106 段 ×（解码 + HEVC 编码），macOS VideoToolbox 硬件编码较快，但要实测总时长，必要时分批。
6. **网络**：raw.githubusercontent 慢（~21KB/s），一律走 git sparse clone。
7. **PyPI 环境坑**：系统 Python 3.9 + pip 21.2.4 拿不到 `av` wheel（报 `from versions: none`，容易被误判成网络不通）。用 ccs 的 3.12 解释器。
8. **调试时别把二进制打到 stdout**：`python ... | hexdump` 之类会瞬间灌满上下文（本次踩过），一律重定向到文件再读。

## 八、执行顺序速查

```
阶段 0 环境+素材 ─→ 阶段 1 单段 PoC ─┬─ 通过 ─→ 阶段 2 渲染层(16段) ─→ 阶段 3 批量转码 ─→ 阶段 4 接线 ─→ 阶段 5 分发文档
                                    └─ 不通过 ─→ 备选路线 B（WebP 降参）─→ 阶段 4 ─→ 阶段 5
```

每个阶段结束都要跑一次 `pytest tests/test_dafeiyu_*.py`（当前基线 1315 项全绿），并在 `docs/logs.md` 留一条增量记录。
