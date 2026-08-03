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

- `akm-pagination`
  - 用途：通用分页壳
  - 常用方法：`renderPagination({ totalPages, currentPage, onSelectName, summary })`

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
