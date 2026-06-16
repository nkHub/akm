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

## 使用原则

1. 组件只负责 UI 壳和基础交互，不承载具体业务请求。
2. 页面仍保留业务函数，例如 `loadKeys()`、`refreshLogs()`、`savePluginConfig()`。
3. 新组件优先复用现有样式体系，不引入 Shadow DOM，避免重复维护样式。
4. 如果只是一个页面独有且高度业务化的块，优先先抽“壳”，不要一上来把整块业务逻辑做成大组件。
5. 新页面若出现重复弹窗、分页、开关、分段按钮，优先复用这里已有组件，而不是再复制 HTML 结构。
6. 如果页面里重复出现“左侧说明 + 右侧操作区”的设置项，优先使用 `akm-settings-card` 收口布局，再在内部放具体业务控件。
