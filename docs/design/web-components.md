# 管理台 Web Components 约定

当前管理台里可复用的壳组件统一放在 `akm/static/akm-ui.js`。

## 当前组件

- `akm-switch`
  - 用途：布尔开关
  - 常用属性：`label`、`host-class`
  - 常用方法：`setChecked(boolean)`、`setDisabled(boolean)`
  - 对外事件：`change`
  - 使用方：设置页各类开关、连接池页「自动刷新 (3s)」（`change` 的 `detail.checked` 控制 `setInterval` 起停）

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
  - 使用方：Key 管理页、审计日志页、连接池页（`#pool-empty`，样式由外层 class 提供）
  - 约定：`_styles.html` 中固定 `akm-empty-state { display: block; }` —— 自定义元素默认 `inline`，不补这条会让 `py-*` 垂直内边距与 `text-center` 全部失效（连接池页空态曾因此显示异常）；需要 flex 布局的用法（审计日志页）由 Tailwind 类按更高优先级覆盖

- `akm-settings-card`
  - 用途：设置页左右布局卡片壳
  - 常用属性：`align`（`center` / `start`）
  - 约定：右侧操作区用 `slot="actions"`

- `akm-modal`
  - 用途：居中弹窗壳
  - 常用属性：`title`、`max-width`、`body-class`、`panel-class`
  - 常用方法：`open()`、`close()`、`setTitle(text)`、`setSubtitle(text)`
  - 约定：底部操作区用 `data-modal-footer`
  - 使用方：连接池页「重建连接池 / 关闭路由池」确认弹窗（页面保留 `_poolPendingAction`，确认按钮再发起 `POST /api/pool/action`）
  - 约定：内容可能超长时加 `panel-class="max-h-[85vh] flex flex-col"` 与 `body-class="... overflow-y-auto flex-1 min-h-0"`，让弹窗整体限高、内容区内部滚动，标题栏与底部操作区保持固定（模型列表、用量查询配置、插件配置等弹窗均用此写法）

- `akm-drawer`
  - 用途：右侧滑出详情面板
  - 常用属性：`title`、`max-width`
  - 常用方法：`open()`、`close()`、`setTitle(text)`

- `akm-tooltip`
  - 用途：包裹任意触发行内元素（如信息图标），hover 时在页面级显示多行说明浮层
  - 常用属性：`content`（提示文本，支持 `\n` 换行）
  - 约定：浮层为 fixed 定位挂载在 `body` 下，避免被表格等容器的 `overflow` 裁剪
  - 生命周期：`disconnectedCallback` 会收起浮层并把挂到 `body` 的节点摘掉。定时重渲染的列表（如连接池页每 3s 刷新表格）必须依赖这一点：宿主节点被 `innerHTML` 整体替换时 `mouseleave` 不会再触发，否则浮层会永久留在页面上、并随每次重建不断堆积节点
  - 触发元素不要写成行内元素参与文本行盒：`inline-block` 的图标会撑高所在行，文字一换行还会被挤到下一行独占一行。重渲染单元格里应把触发元素作为 flex 子项（`flex items-start` + 触发元素 `shrink-0`，图标再给 `block`），行高才稳定（连接池页连接列即此写法）
  - 使用方：统计页费用 ⓘ、连接池页连接明细（`content` 由 JS 用 `\n` 拼出每条连接的源站 / 协议 / 状态 / 已发请求数；拼装前先 `esc()` 转义，换行保留在属性值里）

- `akm-line-chart`（`akm/static/akm-chart.js`，底层：本地 ApexCharts 3）
  - 用途：通用折线图壳组件，如统计页「报错趋势」与「每日用量」。早期为内联 SVG 自绘实现，现已迁移到本地打包的 ApexCharts（`akm/static/apexcharts.min.js`，仅统计页引入），对外 `render(config)` API 不变
  - 常用方法：`render({ labels, values, height, maxHeight, fill, color, unit, format, emptyText, details })`
    - `labels` / `values`：等长数组，分别对应 X 轴刻度与数值序列
    - `height`：可选，CSS px，默认 `220`；`fill` 开启时作为**最小高度**
    - `maxHeight`：可选，`fill` 模式下的高度上限，默认等于 `height`（不传 `maxHeight` 即不拉伸）
    - `fill`：可选，为 `true` 时垂直拉伸到父容器内容区高度，并夹在 `[height, maxHeight]` 之间；父容器不够高时保持 `height`
    - `color`：可选，折线/面积主色，默认 `#818cf8`
    - `unit`：可选，数据点悬浮提示的数值单位
    - `format`：可选，`function(value) -> string`，自定义悬浮提示里的数值（给了它就不再拼 `unit`），如延迟的 `4.2s`、成功率的 `98.42%`
    - `emptyText`：可选，无任何有效数据（全 null）时的空态文案；有数据但全 0 时照常画基线并叠加该文案
    - `details`：可选，与 `values` 等长的附加提示行（字符串或字符串数组），如「8 / 10 成功 · 2 失败」「该时段无请求」
  - 多序列 / 多 Y 轴：`render({ labels, series, axes, height, maxHeight, fill, emptyText })`
    - `series`：`[{ label, values, color, axis, area, format, unit }]`，`axis` 指向 `axes` 里的轴 id；`area` 为 `true` 时该线带面积填充
    - `axes`：`[{ id, side, min, max, format }]`，`side` 为 `left` / `right`；**不写 `min`/`max` 时按数据范围自适应量程**（成功率 99.7%~100% 的差异能看出来），需要固定量程才显式写（如 `min: 0, max: 100`）
    - 同一 `axis` id 的多条序列共用一条视觉轴：组件给每条序列生成一个 yaxis 项，并把 `seriesName` 设为**组内第一条序列的名字**来归组（ApexCharts 的分组轴按「yaxis.seriesName 与 series.name 同名」匹配，自造组名匹配不上）；组内只有第一条显示刻度
    - **空值语义（重要）**：`values` 里的 `null` = 该点没有样本 —— 折线在这一点断开、不画点、悬浮显示「—」；`0` = 真实的 0（画在 0 值处）。两者不要混用
    - 使用方：统计页「每日用量」7d/30d 折线图（总 Token 左轴 + 成功率/缓存命中率右轴自适应；无请求日成功率与缓存命中率传 `null` 断线）
  - 交互：`tooltip.shared` + x 轴 crosshairs 十字准星，悬浮一次给出该时间点全部序列数值（内容自绘并 HTML 转义，序列名/报错文本来自上游数据）；图例可点击隐藏/显示序列（单序列不显示图例）
  - 主题跟随：颜色统一从 CSS 变量（`--c-*`，注意要拼成**逗号分隔**的 `rgb(r, g, b)` —— ApexCharts 的颜色处理不认空格分隔的现代语法，会抛错导致轴渲染中断）；监听 `<html>` 的 `data-theme` / `class` 变化后销毁重建实例
  - 约定：沿用页面浅色 DOM（不引入 Shadow DOM）；`ResizeObserver` + 「宽度 + fill 可用高度」签名比对驱动重绘；依赖缺失时显示「图表库加载失败」而不是空白；组件文件从 `akm-ui.js` 拆出（`akm-chart.js`，全局引入），echarts 底层尝试已废弃，不要再用

- `akm-donut-chart`
  - 用途：通用环形图壳组件，如统计页按 Key / 按模型 / 按来源的「图」视图。已与折线图一起迁移到本地 ApexCharts（`akm/static/akm-chart.js`），对外 `render(config)` API 不变
  - 常用方法：`render({ items, unit, format, maxSlices, size, centerLabel, emptyText, palette })`
    - `items`：`[{ label, value, details }]`，`details` 为悬浮浮层的附加行（字符串或字符串数组），如费用拆解
    - `unit` / `format`：数值单位与格式化函数，图例、圆心、浮层共用
    - `maxSlices`：可选，最多画几片（**含**合并出的「其他」），默认 `6`；长尾按大小合并为「其他」并在下方提示合并了几项
    - `size`：可选，直径 CSS px，默认 `168`；`centerLabel`：可选，圆心默认文案（默认「总计」）
    - `emptyText`：可选，所有项都非正数时的空态文案
  - 交互：悬浮扇区弹 ApexCharts 浮层（名称 / 数值 / 占比 / `details`），圆心**不显示任何文字**（用户要求去掉；数值与占比由右侧图例承担，`centerLabel` 参数保留但当前不渲染）；`dataLabels` 必须显式关掉，否则片上会出现百分比数字；悬浮图例行走 `akmFloatingTip()`，且会高亮对应扇区（其余片淡出至 0.4），悬浮扇区反向高亮图例行（图例保留自绘 HTML，带数值与占比，原生 legend 做不到）
  - 约定：主题跟随与折线图共用 `akmObserveTheme()`（`data-theme` / `class` 变化时销毁重建）；片间缝用 `--c-border` 勾边；浮层样式统一由 `_styles.html` 的 `.apexcharts-tooltip` 覆盖块提供（`--c-tooltip-*` 配色、360px 限宽、11px 字号），折线与环形共用

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
- 定时重渲染的表格/列表要按内容去重：生成 HTML 后与上一次相同就直接返回，不要无条件 `innerHTML = ...`。否则每次轮询都会重建整块 DOM，指针下的行被换掉、悬浮中的浮层被销毁，无意义的重排也会白跑（连接池页 `renderPoolRows` / `renderPoolEvents` 用 `dataset.*Html` 做这项比对；空态分支要顺手清掉缓存标记，避免池/数据恢复后被误判成「没变」而不渲染）。

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
