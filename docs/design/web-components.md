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
  - 说明：`options` 为 `[{ value, label }]`，点击后回调 `window[onSelectName](value)`；同一个页面可挂多组（统计页范围用 `dashboard-days`，趋势指标用 `error-metric-tabs`，按 Key/模型/来源的「表 / 图」与「请求 / Token / 费用」分别用 `view-*`、`metric-*`）

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
  - 约定：沿用页面浅色 DOM（不引入 Shadow DOM）；用 `ResizeObserver` 监听宿主宽度与（`fill` 时）父容器尺寸，宿主/父容器尺寸变化、容器从 `display:none` 恢复显示时自动重绘，并以「宽度 + fill 可用高度」签名比对避免自激循环；有 `details` 时按相邻点中点划分整列透明命中区，不必精准对准圆点即可悬浮，浮层为 fixed 定位挂到 `body`（避免被卡片 `overflow-hidden` 裁剪），优先显示在数据点上方、空间不足时翻到下方；数据点多于 40 个时只画折线、不画圆点

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

## 使用原则

1. 组件只负责 UI 壳和基础交互，不承载具体业务请求。
2. 页面仍保留业务函数，例如 `loadKeys()`、`refreshLogs()`、`savePluginConfig()`。
3. 新组件优先复用现有样式体系，不引入 Shadow DOM，避免重复维护样式。
4. 如果只是一个页面独有且高度业务化的块，优先先抽“壳”，不要一上来把整块业务逻辑做成大组件。
5. 新页面若出现重复弹窗、分页、开关、分段按钮，优先复用这里已有组件，而不是再复制 HTML 结构。
6. 如果页面里重复出现“左侧说明 + 右侧操作区”的设置项，优先使用 `akm-settings-card` 收口布局，再在内部放具体业务控件。
