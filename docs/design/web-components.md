# 管理台 Web Components 约定

当前管理台里可复用的壳组件统一放在 `akm/static/akm-ui.js`。

## 当前组件

- `akm-switch`
  - 用途：布尔开关
  - 常用属性：`label`、`host-class`
  - 常用方法：`setChecked(boolean)`、`setDisabled(boolean)`
  - 对外事件：`change`

- `akm-range-tabs`
  - 用途：时间范围或分段按钮切换
  - 常用方法：`setOptions(options, currentValue, onSelectName)`
  - 说明：`options` 为 `[{ value, label, icon?, title? }]`，点击后回调 `window[onSelectName](value)`；同一个页面可挂多组（统计页范围用 `dashboard-days`，趋势指标用 `error-metric-tabs`，按 Key/模型/来源的「表 / 图」与「请求 / Token / 费用」分别用 `view-*`、`metric-*`）
  - 约定：`icon` 为受信任的内联 SVG 字符串，提供时按钮只渲染图标，可读文本退到 `title` / `aria-label`（缺省用 `label`），按钮内边距不变因此与纯文字态等高（统计页 `view-*` 用柱状图/表格网格图标表达「图 / 表」）

- `akm-pagination`
  - 用途：通用分页壳
  - 常用方法：`renderPagination({ totalPages, currentPage, onSelectName, summary })`
  - 说明：`totalPages <= 1` 时组件自动隐藏；点击后回调 `window[onSelectName](page)`。使用方：审计日志（`log-pagination`）、统计页按 Key/模型/来源三张表（`pager-key`/`pager-model`/`pager-source`，每页 6 行）

- `akm-empty-state`
  - 用途：统一空态文案
  - 常用属性：`message`

- `akm-settings-card`
  - 用途：设置页左右布局卡片壳
  - 常用属性：`align`（`center` / `start`）
  - 约定：右侧操作区用 `slot="actions"`

- `akm-modal`
  - 用途：居中弹窗壳
  - 常用属性：`title`、`max-width`、`body-class`、`panel-class`
  - 常用方法：`open()`、`close()`、`setTitle(text)`、`setSubtitle(text)`
  - 约定：底部操作区用 `data-modal-footer`
  - 约定：内容可能超长时加 `panel-class="max-h-[85vh] flex flex-col"` 与 `body-class="... overflow-y-auto flex-1 min-h-0"`，让弹窗整体限高、内容区内部滚动，标题栏与底部操作区保持固定（模型列表、用量查询配置、插件配置等弹窗均用此写法）

- `akm-drawer`
  - 用途：右侧滑出详情面板
  - 常用属性：`title`、`max-width`
  - 常用方法：`open()`、`close()`、`setTitle(text)`

- `akm-tooltip`
  - 用途：包裹任意触发行内元素（如信息图标），hover 时在页面级显示多行说明浮层
  - 常用属性：`content`（提示文本，支持 `\n` 换行）
  - 约定：浮层为 fixed 定位挂载在 `body` 下，避免被表格等容器的 `overflow` 裁剪；组件以 `inline-block` 行内展示，不影响宿主行高

- `akm-line-chart`
  - 用途：通用折线图壳组件（内联 SVG，不引入第三方图表库），如统计页「报错趋势」
  - 常用方法：`render({ labels, values, height, maxHeight, fill, color, unit, format, emptyText, details })`
    - `labels` / `values`：等长数组，分别对应 X 轴刻度与数值序列
    - `height`：可选，CSS px，默认 `220`；`fill` 开启时作为**最小高度**
    - `maxHeight`：可选，`fill` 模式下的高度上限，默认等于 `height`（不传 `maxHeight` 即不拉伸）
    - `fill`：可选，为 `true` 时垂直拉伸到父容器内容区高度，并夹在 `[height, maxHeight]` 之间；父容器不够高时保持 `height`
    - `color`：可选，折线/面积主色，默认 `#818cf8`
    - `unit`：可选，数据点悬浮提示的数值单位
    - `format`：可选，`function(value) -> string`，自定义悬浮提示里的数值（给了它就不再拼 `unit`），如延迟的 `4.2s`、成功率的 `98.42%`
    - `emptyText`：可选，全 0 时绘图区中央的空态文案
    - `details`：可选，与 `values` 等长的附加提示行（字符串或字符串数组）；传了它时该数据点改用组件自绘浮层展示「刻度: 数值单位」+ 附加行，未传时保持 SVG `<title>` 原生提示
  - 多序列 / 多 Y 轴：`render({ labels, series, axes, height, maxHeight, fill, emptyText })`
    - `series`：`[{ label, values, color, axis, area, format, unit }]`，`axis` 指向 `axes` 里的轴 id；`area` 为 `true` 时该线带面积填充
    - `axes`：`[{ id, side, min, max, format, color }]`，`side` 为 `left` / `right`；不写 `max` 时按该轴所有序列自动取「好看整数」上界，量纲固定时（如百分比）显式写 `min: 0, max: 100`
    - 约定：同一 `side` 的多个轴按声明顺序由内向外排开（第一条最贴近绘图区），刻度文字用该轴第一条线的颜色；图例固定画在绘图区顶部一行；`values` 里的 `null` / 空值按 0 处理（画在基线上、折线连续），调用方需要「无样本」语义时自行决定传什么值
    - 使用方：统计页「每日用量」7d/30d 折线图（总 Token 左轴 + 成功率/缓存命中率右轴 0~100%）
  - 约定：沿用页面浅色 DOM（不引入 Shadow DOM）；用 `ResizeObserver` 监听宿主宽度与（`fill` 时）父容器尺寸，宿主/父容器尺寸变化、容器从 `display:none` 恢复显示时自动重绘，并以「宽度 + fill 可用高度」签名比对避免自激循环；有 `details` 或为多序列时按相邻点中点划分整列透明命中区，不必精准对准圆点即可悬浮，浮层为 fixed 定位挂到 `body`（避免被卡片 `overflow-hidden` 裁剪），优先显示在数据点上方、空间不足时翻到下方；数据点多于 40 个时只画折线、不画圆点

- `akm-donut-chart`
  - 用途：通用环形图壳组件（内联 SVG，占比视角），如统计页按 Key / 按模型 / 按来源的「图」视图
  - 常用方法：`render({ items, unit, format, maxSlices, size, centerLabel, emptyText, palette })`
    - `items`：`[{ label, value, details }]`，`details` 为悬浮浮层的附加行（字符串或字符串数组），如费用拆解
    - `unit` / `format`：数值单位与格式化函数，图例、圆心、浮层共用
    - `maxSlices`：可选，最多画几片（**含**合并出的「其他」），默认 `6`；长尾按大小合并为「其他」并在下方提示合并了几项
    - `size`：可选，直径 CSS px，默认 `168`；`centerLabel`：可选，圆心默认文案（默认「总计」）
    - `emptyText`：可选，所有项都非正数时的空态文案
  - 交互：悬浮扇区或图例行都会弹浮层（名称 / 数值 / 占比 / `details`），同时该片加粗、其余片淡出、圆心切换为该片数值；扇区按角度命中（整个圆盘除圆心附近都算命中区），长尾小项建议用图例行悬浮
  - 约定：沿用页面浅色 DOM；浮层复用共享的 `akmFloatingTip()`（fixed 挂 `body`）；不引入第三方图表库

- `akm-chat-viewer`（`akm/static/chat-viewer.js`）
  - 用途：日志详情抽屉的对话视图，消息气泡级虚拟列表（动态测量高度，按可视区渲染）
  - 数据契约：`setItems(items)`，`items[]` 为 `{ role, html }`；`role` 支持 `user` / `assistant` / `system` / `meta`
  - 常用方法：`setItems(items)`、`setLoading(text)`、`clear()`
  - 样式约定：使用 Shadow DOM；`.md` 内已收敛 markdown 标题（`h1`~`h6` ≤ 1.15em）、代码块/引用/表格底色与边框、长单词 `overflow-wrap:anywhere`。注意：**对话框气泡字号与 markdown 样式均在此组件内维护**，页面侧勿再引入全局 markdown 样式，避免双重控制。
  - 来源：独立于 `akm-ui.js`，随日志页单独 `<script>` 引入。

## 布局稳定性约定（避免刷新抖动）

骨架屏/异步内容最容易踩的坑是「先按 A 高度画一遍，数据回来换成 B 高度，整页被顶一下」。当前沉淀的规则：

- `akm-range-tabs` 在调用 `setOptions()` 之前是空元素，宿主 `akm-range-tabs` 用 CSS 固定 `min-height: 30px`（`_styles.html`）先占住一行：否则 JS 跑起来时容器从 0 长到 30px，会把同页下方内容整体顶下去（例如统计页「时间范围」那一行）。
- 骨架屏必须与真实内容**逐块等高**，包括：卡片表头高度（真实表头里含 30px 的分段按钮，骨架屏用等高的占位块）、按条件出现/隐藏的整块（如统计页「每日用量」只在 7d/30d 出现，骨架屏要有同样条件才出现的占位）、以及服务端就已渲染好的静态块（如插件首页卡片区，应放在数据容器之外立即显示）。
- 骨架屏里的文字占位建议直接用「透明文字的骨架条」（`class="skeleton text-transparent text-xs"` + 真实文案）：这样窄窗口下文字换行时，骨架高度与真实高度仍然一致；固定宽度（`w-12` 之类）在换行场景下会对不上。
- 需要在首次绘制前就确定的状态（例如「每日用量」占位显示与否取决于 localStorage 里的天数），要在该元素之后紧跟一段内联 `<script>` 就地处理，不要等到 `DOMContentLoaded`/数据回调里再改，否则会发生在首屏之后，用户能看见跳变。
- 自定义元素初始化（`data-ready`）前布局与渲染后不一致时，宿主侧先 `visibility: hidden`（如 `.plugin-cards-masonry > akm-plugin-card:not([data-ready])`），避免先以原始槽内容出现再跳一下。

## 主题与配色 token

管理台有两套主题：深色（默认，即原有配色）与浅色，由 `<html>` 上的 `data-theme` 决定，用户在右上角切换（存在 `localStorage` 的 `akm.theme`，可为 `dark` / `light` / `system`）。

- token 定义在 `akm/templates/_styles.html`：`html { --c-* }` 是深色，`html[data-theme="light"] { --c-* }` 覆盖浅色。值写成 `R G B` 三段空格分隔（例如 `--c-surface: 30 30 46;`），Tailwind 侧配合 `rgb(var(--c-surface) / <alpha-value>)` 使用。
- tailwind.config（`akm/templates/_layout.html`）把 `surface` / `surface-light` / `surface-hover` / `border` / `border-light` / `strong` / `switch-off` / `gray-200`~`gray-600` 以及各强调色的**文字档**（如 `indigo-400`、`red-400`、`amber-400`、`indigo-950` 这类淡底档）映射到 token；强调色的实心按钮档（`*-500` / `*-600`）两套主题共用，仍配 `text-white`。
- 写新页面/组件时的约定：
  1. 落在 surface 上的标题、正文用 `text-strong` / `text-gray-200`~`text-gray-600`，**不要**用 `text-white`（那只留给实心强调色按钮上的文字）。
  2. 背景、边框用 `bg-surface` / `bg-surface-light` / `bg-surface-hover` / `border-border` / `border-border-light`，不要写死 hex。
  3. 开关未打开用 `bg-switch-off`（不要用 `bg-gray-600`，浅色下那个档位是给暗淡文字用的）。
  4. 组件内部的 SVG（图表网格线、刻度文字、图例、数据点圆环）用 `_styles.html` 里的 `.akm-chart-*` 类，不要在内联属性里写死颜色；序列色属于强调色，可以继续内联（如 `#818cf8`）。
  5. 浮层（tooltip / 通知卡）用 `rgb(var(--c-tooltip-bg))` 一类 token；Shadow DOM 组件（`akm-json-viewer` / `akm-chat-viewer`）直接引用 `var(--c-*)`，CSS 变量会继承进 Shadow DOM，并带一份深色兜底值以便脱离管理台单独使用时也能看。
  6. 不需要 `dark:` 变体：主题切换只换 token 值，模板与组件里不应出现 `dark:` 前缀类。

## 使用原则

1. 组件只负责 UI 壳和基础交互，不承载具体业务请求。
2. 页面仍保留业务函数，例如 `loadKeys()`、`refreshLogs()`、`savePluginConfig()`。
3. 新组件优先复用现有样式体系，不引入 Shadow DOM，避免重复维护样式。
4. 如果只是一个页面独有且高度业务化的块，优先先抽“壳”，不要一上来把整块业务逻辑做成大组件。
5. 新页面若出现重复弹窗、分页、开关、分段按钮，优先复用这里已有组件，而不是再复制 HTML 结构。
6. 如果页面里重复出现“左侧说明 + 右侧操作区”的设置项，优先使用 `akm-settings-card` 收口布局，再在内部放具体业务控件。
