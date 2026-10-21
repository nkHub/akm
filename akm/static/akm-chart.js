// ── akm-line-chart：通用折线图壳组件（底层：本地 ApexCharts 3）──
// 对外 API 与旧版内联 SVG 实现保持兼容，渲染引擎已从 ECharts 换成 ApexCharts，能力清单：
//   1) 空值断线：values 里的 null 表示「该点没有样本」，折线在这一点断开且不画点，不再把 null 当 0 画；
//   2) 轴量程自适应：axes 未显式声明 min/max 时按数据范围收窄（成功率 99.7%~100% 的差异能看出来）；
//   3) 十字准星：tooltip.shared + x 轴 crosshairs，一次看到该时间点所有序列的数值；
//   4) 图例开关：点击图例可隐藏 / 显示某条序列（ApexCharts 原生能力）；
//   5) 主题跟随：颜色统一从 CSS 变量（--c-*）读取，data-theme / class 变化时销毁重建（ApexCharts
//      的颜色写在实例里，CSS 变量变化不会自动生效）。
// 依赖：window.ApexCharts。统计页通过 /static/apexcharts.min.js 引入；若页面没引入，本组件会在
//       首次 render 时动态加载一次（同一页面共享同一个 Promise，不会重复请求）。
// 用法（与旧版保持一致）：
//   单序列：chart.render({ labels, values, details, format, unit, color, height, maxHeight, fill, emptyText })
//   多序列：chart.render({ labels, series: [{ label, values, color, axis, area, format, unit }],
//                          axes: [{ id, side, min, max, format }], height, maxHeight, fill, emptyText })
// 语义约定：values 里 null = 无样本（断线），0 = 真实的 0（画在 0 值处），调用方不要混用。

// 动态加载 ApexCharts（页面未引入时的兜底）
var __akmApexPromise = null;
function akmLoadApex() {
  if (window.ApexCharts) return Promise.resolve(window.ApexCharts);
  if (!__akmApexPromise) {
    __akmApexPromise = new Promise(function(resolve, reject) {
      var script = document.createElement('script');
      script.src = '/static/apexcharts.min.js';
      script.async = true;
      script.onload = function() {
        if (window.ApexCharts) resolve(window.ApexCharts);
        else reject(new Error('ApexCharts 未挂载'));
      };
      script.onerror = function() {
        __akmApexPromise = null;
        reject(new Error('ApexCharts 加载失败'));
      };
      document.head.appendChild(script);
    });
  }
  return __akmApexPromise;
}

// tooltip 内容是 HTML，序列名与报错文本都可能来自上游数据，必须转义
// （报错详情等长文本不限宽会把 tooltip 撑出屏幕外）
function tooltipWrapHtml(inner) {
  return '<div style="font-size:11px;line-height:1.7;max-width:320px;overflow-wrap:anywhere;white-space:normal;text-align:left">' + inner + '</div>';
}

// CSS 变量取色：--c-* 存的是「r g b」三元组。拼成逗号分隔的 rgb() ——
// ApexCharts 内部的颜色处理只认旧式 rgb(r, g, b)，空格分隔的现代语法会让它抛错、轴渲染中断
function akmCssColor(name, fallback) {
  var raw = '';
  try {
    raw = (window.getComputedStyle(document.documentElement).getPropertyValue(name) || '').trim();
  } catch (e) {
    raw = '';
  }
  return raw ? 'rgb(' + raw.split(/\s+/).join(', ') + ')' : fallback;
}
function akmEscapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
// 主题跟随：ApexCharts 的颜色在实例化时写死，需要监听 <html> 的 data-theme / class 变化后整体重绘。
// line 与 donut 两个组件共用；30ms 防抖合并连续变化（切主题会��发多个 attribute 变更）。
function akmObserveTheme(el) {
  var obs = new MutationObserver(function() {
    if (!el._config) return;
    clearTimeout(el._themeTimer);
    el._themeTimer = setTimeout(function() {
      if (el.isConnected && el._config) el.render(el._config);
    }, 30);
  });
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
  return obs;
}

if (!customElements.get('akm-line-chart')) {
  customElements.define('akm-line-chart', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      this.style.display = 'block';
      this._themeObs = akmObserveTheme(this);
      if (typeof ResizeObserver === 'function') {
        // 尺寸变化驱动重绘：容器从隐藏变为可见、窗口缩放、布局调整都会触发。
        // 用签名比对（宽度 + fill 模式下的父容器可用高度）避免自激循环。
        this._ro = new ResizeObserver(function() {
          if (!self._config) return;
          if (self._sizeSignature() === self._lastSig) return;
          self.render(self._config);
        });
        this._ro.observe(this);
      } else {
        this._onResize = function() {
          clearTimeout(self._resizeTimer);
          self._resizeTimer = setTimeout(function() {
            if (self._config && self.isConnected) self.render(self._config);
          }, 150);
        };
        window.addEventListener('resize', this._onResize);
      }
    }

    disconnectedCallback() {
      if (this._ro) { this._ro.disconnect(); this._ro = null; }
      if (this._themeObs) { this._themeObs.disconnect(); this._themeObs = null; }
      clearTimeout(this._themeTimer);
      clearTimeout(this._convergeTimer);
      clearTimeout(this._pendingTimer);
      if (this._onResize) window.removeEventListener('resize', this._onResize);
      this._destroy();
    }

    // 父容器内容区高度（剔除 padding），供 fill 模式把图表拉伸到卡片剩余高度
    _availableHeight() {
      var parent = this.parentNode;
      if (!parent || typeof window.getComputedStyle !== 'function') return 0;
      var style = window.getComputedStyle(parent);
      var pad = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
      return Math.max(0, Math.round(parent.clientHeight - pad));
    }

    // 重绘判定签名：宽度变化总是重绘；fill 模式再叠加父容器可用高度
    _sizeSignature() {
      var width = Math.round(this.clientWidth || 0);
      var fill = !!(this._config && this._config.fill);
      return width + 'x' + (fill ? this._availableHeight() : 0);
    }

    // CSS 变量取色：--c-* 存的是「r g b」三元组，拼成逗号分隔的 rgb()（实现见文件级 akmCssColor，
    // line 与 donut 共用；空格分隔的现代语法会让 ApexCharts 抛错、轴渲染中断）
    _cssColor(name, fallback) {
      return akmCssColor(name, fallback);
    }

    _destroy() {
      if (this._chart) {
        try { this._chart.destroy(); } catch (e) {}
        this._chart = null;
      }
    }

    render(config) {
      config = config || {};
      this._config = config;
      var self = this;
      // 首次渲染等两帧、让布局稳定：页面加载时骨架屏刚切换成真实内容，这一两帧里
      // 容器宽度还在变化，立刻渲染会先画一张窄图、随后收敛检查再重建，肉眼就是「宽度抖一下」
      if (!this._chart && !this._firstPaintDone) {
        if (!this._rafPending) {
          this._rafPending = true;
          var kick = function() {
            requestAnimationFrame(function() {
              requestAnimationFrame(function() {
                self._rafPending = false;
                self._firstPaintDone = true;
                if (self.isConnected && self._config) self.render(self._config);
              });
            });
          };
          kick();
        }
        return;
      }
      var labels = Array.isArray(config.labels) ? config.labels : [];
      // 容器当前不可见（如切换 1d/7d/30d 时内容区先隐藏、数据回来再显示）：不渲染 ——
      // 此时 clientWidth 为 0，会走 640 fallback 画一张窄图，随后可见了又重画，
      // 肉眼就是「切换时间范围时图表宽度抖一下」。等宽高就绪挂个短轮询兜底。
      if (!this.parentNode || !this.clientWidth) {
        clearTimeout(this._pendingTimer);
        var selfP = this;
        this._pendingTimer = setTimeout(function() {
          if (selfP.isConnected && selfP._config && selfP.clientWidth) selfP.render(selfP._config);
        }, 60);
        return;
      }
      var emptyText = config.emptyText || '暂无数据';

      // 高度：默认固定；fill 时拉伸到父容器可用高度，并夹在 [height, maxHeight]
      var baseHeight = Math.max(120, parseInt(config.height || 220, 10) || 220);
      var maxHeight = Math.max(baseHeight, parseInt(config.maxHeight || 0, 10) || baseHeight);
      var height = baseHeight;
      if (config.fill === true) {
        var available = this._availableHeight();
        if (available > 0) height = Math.min(maxHeight, Math.max(baseHeight, available));
        // 父容器尺寸变化时也要重绘；父容器可能晚于 connectedCallback 才挂上，这里补观察
        if (this._ro && this.parentNode && !this._parentObserved) {
          try { this._ro.observe(this.parentNode); this._parentObserved = true; } catch (e) {}
        }
      }
      var width = Math.round(this.clientWidth || (this.parentNode && this.parentNode.clientWidth) || 640);
      if (width < 240) width = 240;
      this._lastWidth = width;
      // 渲染入口处的签名：异步创建实例期间布局可能继续变化（骨架屏刚展开、侧栏切换），
      // _lastSig 必须定格在入口值 —— 若在异步回调里重取，会把「渲染期间发生的变宽」当成已处理，
      // ResizeObserver 比对相同就不再重绘，图就停在了旧的（如骨架屏期的 640px）宽度上
      var entrySig = this._sizeSignature();

      var multi = Array.isArray(config.series) && config.series.length > 0;
      // 归一化：null / 空串 / 非法值 = 无样本（断线）；0 是真实值
      var norm = function(v) {
        if (v == null || v === '') return null;
        var n = Number(v);
        return Number.isFinite(n) ? n : null;
      };
      var PALETTE = ['#818cf8', '#fb7185', '#34d399', '#22d3ee', '#fbbf24', '#a78bfa'];
      var seriesList = multi
        ? config.series.filter(Boolean).map(function(s, i) {
            return {
              label: String(s.label == null ? '' : s.label),
              color: s.color || PALETTE[i % PALETTE.length],
              axisId: s.axis == null ? '' : String(s.axis),
              area: s.area === true,
              format: typeof s.format === 'function' ? s.format : null,
              unit: s.unit || '',
              values: (Array.isArray(s.values) ? s.values : []).slice(0, labels.length).map(norm)
            };
          })
        : [{
            label: '',
            color: config.color || '#818cf8',
            axisId: '',
            area: false,
            format: typeof config.format === 'function' ? config.format : null,
            unit: config.unit || '',
            values: (Array.isArray(config.values) ? config.values : []).slice(0, labels.length).map(norm),
            details: Array.isArray(config.details) ? config.details : []
          }];

      // 一个有效数据点都没有：只渲染空态文案（不画空坐标系，避免看起来像「数据全 0」）
      var hasAnyValue = seriesList.some(function(s) {
        return s.values.some(function(v) { return v != null; });
      });
      if (!labels.length || !seriesList.length || !hasAnyValue) {
        this._destroy();
        this.innerHTML = '<div class="flex items-center justify-center text-xs text-gray-600" style="height:' + height + 'px">' + akmEscapeHtml(emptyText) + '</div>';
        this._lastSig = entrySig;
        return;
      }

      // ── 轴 ──
      // 每条序列对应一个 yaxis 配置项（ApexCharts 按下标对应）；同一 axisId 的序列共用一个视觉轴：
      // 用相同 seriesName 分组，组内只有第一条 show，其余 hide（刻度不重复、量程保持一致）。
      var axisDecls = (multi && Array.isArray(config.axes)) ? config.axes.filter(Boolean) : [];
      // 单序列未声明轴时，轴刻度沿用序列的 format（如 P95 的 fmtMs），否则刻度会是原始数字
      var axisMeta = axisDecls.length
        ? axisDecls
        : [{ id: '', side: 'left', format: !multi && typeof config.format === 'function' ? config.format : null }];
      var axisInfoById = {};
      axisMeta.forEach(function(d) {
        var id = d && d.id != null ? String(d.id) : '';
        if (axisInfoById[id]) return;
        axisInfoById[id] = {
          id: id,
          side: d && d.side === 'right' ? 'right' : 'left',
          min: d && d.min != null && Number.isFinite(Number(d.min)) ? Number(d.min) : null,
          max: d && d.max != null && Number.isFinite(Number(d.max)) ? Number(d.max) : null,
          format: d && typeof d.format === 'function' ? d.format : null
        };
      });

      var tickColor = this._cssColor('--c-gray-500', '#94a3b8');
      var gridColor = this._cssColor('--c-border', '#334155');
      var legendColor = this._cssColor('--c-gray-400', '#9ca3af');
      var fontFamily = '';
      try { fontFamily = window.getComputedStyle(document.body).fontFamily || ''; } catch (e) { fontFamily = ''; }

      // 同 axisId 的序列共用一个视觉轴。ApexCharts 的分组轴按「yaxis.seriesName 与 series.name
      // 同名」匹配，自造组名匹配不上、不会合并 —— 所以组名直接用组内第一条序列的名字。
      var groupNameById = {};
      seriesList.forEach(function(s) {
        var info = axisInfoById[s.axisId] || axisInfoById[''];
        if (!info) return;
        if (!groupNameById[info.id]) {
          groupNameById[info.id] = s.label || ('series-' + seriesList.indexOf(s));
        }
        s.axisGroup = groupNameById[info.id];
        s.axisInfo = info;
      });

      var yaxis = seriesList.map(function(s) {
        var info = s.axisInfo;
        // 贴近 ApexCharts 官方多轴用法：只声明 seriesName / opposite / labels，
        // 相同 seriesName 的轴自动归组（组内只有一条刻度）。min/max 只在有值时传，
        // 显式传 null 会把内部量程计算带崩（实测退化成 0~6 的刻度）。
        var conf = {
          seriesName: s.axisGroup,
          opposite: info.side === 'right',
          labels: {
            style: { colors: tickColor, fontSize: '10px', fontFamily: fontFamily },
            formatter: function(value) {
              // 十字准星悬浮到无样本（null）的点时，ApexCharts 会把 null 传进刻度 formatter ——
              // 不兜底会直接崩（null.toFixed），这里统一回落成「—」
              if (value == null || value === '' || !Number.isFinite(Number(value))) return '—';
              return info.format
                ? String(info.format(value))
                : Number(value).toFixed(1);
            }
          }
        };
        if (info.min != null) conf.min = info.min;
        if (info.max != null) conf.max = info.max;
        return conf;
      });

      var legendOn = multi && seriesList.some(function(s) { return !!s.label; });
      var chartSeries = seriesList.map(function(s) {
        return {
          name: s.label || ('series-' + seriesList.indexOf(s)),
          type: 'line',
          data: s.values
        };
      });
      var colors = seriesList.map(function(s) { return s.color; });

      // 全 0（或全 null）但有有效点时：0 是真实值照常画；绘图区中央再叠一句空态说明
      var allZero = !seriesList.some(function(s) {
        return s.values.some(function(v) { return v != null && v !== 0; });
      });

      // tooltip：完全自绘内容（转义后拼接），单序列带 details 附加行，多序列逐序列列数值
      var seriesFormat = function(s, raw) {
        if (raw == null || raw === '-') return '—';
        if (s.format) return s.format(raw);
        return raw + s.unit;
      };
      // 自定义浮层内容：整体走 tooltipWrapHtml（固定字号 + 限宽换行），
      // 多序列逐序列列数值，单序列带 details 附加行（如「8 / 10 成功 · 2 失败」）
      var tooltipCustom = multi
        ? function(o) {
            var idx = o.dataPointIndex != null ? o.dataPointIndex : o.dataPointIndex;
            var lines = ['<span style="font-weight:600">' + akmEscapeHtml(labels[idx]) + '</span>'];
            var series = o.w && o.w.globals ? o.w.globals.series : [];
            series.forEach(function(data, i) {
              var s = seriesList[i] || {};
              var head = s.label ? akmEscapeHtml(s.label) + ' ' : '';
              lines.push(head + akmEscapeHtml(seriesFormat(s, data[idx])));
            });
            return tooltipWrapHtml(lines.join('<br/>'));
          }
        : function(o) {
            var idx = o.dataPointIndex;
            var s = seriesList[0];
            var data = o.w && o.w.globals ? o.w.globals.series[0] : [];
            var lines = [akmEscapeHtml(labels[idx]) + ': ' + akmEscapeHtml(seriesFormat(s, data[idx]))];
            var detail = s.details ? s.details[idx] : null;
            var detailLines = detail == null ? [] : (Array.isArray(detail) ? detail : [detail]);
            detailLines.forEach(function(line) {
              if (line != null && String(line) !== '') lines.push(akmEscapeHtml(line));
            });
            return tooltipWrapHtml(lines.join('<br/>'));
          };

      var options = {
        chart: {
          type: 'line',
          // 宽度传渲染瞬间的像素值：'100%' 在刚插入、尚未布局的容器上初始化会被
          // ApexCharts 算成 NaN（svg/foreignObject width NaN 告警）；容器变宽的重绘
          // 由 entrySig + ResizeObserver 重建兜底，这里不需要百分比自适应
          width: width,
          height: height,
          fontFamily: fontFamily,
          background: 'transparent',
          foreColor: tickColor,
          animations: { enabled: false },
          toolbar: { show: false },
          zoom: { enabled: false }
        },
        series: chartSeries,
        colors: colors,
        stroke: {
          width: 2,
          curve: 'straight',
          lineCap: 'round',
          // 线色必须按序列显式给实色：fill.opacity 数组会被 ApexCharts 连带用到线的描边上
          // （总 Token 面积 12% 时整条线也只剩 12% 不透明度，深色下几乎看不见）
          colors: seriesList.map(function(s) { return s.color; })
        },
        // 默认 null 断线（不设 connectNulls），数据点：主题底色 + 序列色描边，无样本处自动不画
        markers: {
          size: labels.length <= 40 ? 4 : 0,
          strokeWidth: 2,
          hover: { size: 6, sizeOffset: 0 }
        },
        dataLabels: { enabled: false },
        grid: {
          borderColor: gridColor,
          strokeDashArray: 4,
          position: 'back',
          xaxis: { lines: { show: false } },
          padding: { left: 14, right: 10, top: legendOn ? 20 : 4, bottom: 4 }
        },
        // 面积填充：不要用 fill.opacity 控制透明度 —— 它会把透明度同时写进 path 的描边
        // （line 与 area 是同一个 path），导致总 Token 线变成 12% 不透明、深色下几乎看不见，
        // 且每次重绘都会重置。这里 opacity 固定 1，面积的 12% 透明改由 CSS 控制
        // （见 _styles.html：g.apexcharts-series:first-child path.apexcharts-line 的 fill-opacity）
        fill: {
          type: 'solid',
          opacity: 1
        },
        legend: {
          show: legendOn,
          position: 'top',
          horizontalAlign: 'left',
          fontSize: '10px',
          fontFamily: fontFamily,
          markers: { size: 4, strokeWidth: 0 },
          labels: { colors: legendColor },
          itemMargin: { horizontal: 8 },
          showForSingleSeries: false
        },
        tooltip: {
          enabled: true,
          shared: true,
          intersect: false,
          theme: 'dark',
          style: { fontSize: '11px', fontFamily: fontFamily },
          custom: tooltipCustom,
          x: {
            show: true,
            crosshairs: {
              show: true,
              stroke: { color: tickColor, dashArray: 4, width: 1 },
              fill: { type: 'none' }
            }
          },
          y: { show: false }
        },
        xaxis: {
          type: 'category',
          categories: labels,
          axisBorder: { color: gridColor },
          axisTicks: { show: false },
          labels: {
            show: true,
            style: { colors: tickColor, fontSize: '10px', fontFamily: fontFamily },
            // 与旧版一致：最多显示 8 个左右，首尾尽量保留
            hideOverlappingLabels: true,
            maxItems: 8,
            trim: false
          },
          tooltip: { enabled: false },
          crosshairs: { show: false }
        },
        yaxis: yaxis,
        states: { active: { filter: { type: 'none' } }, hover: { filter: { type: 'none' } } },
        noData: { text: emptyText, style: { color: tickColor, fontSize: '11px', fontFamily: fontFamily } }
      };

      // 有数据但全 0：照常画（0 是真实值），在绘图区中央补一句空态说明
      var overlayText = allZero ? emptyText : '';

      var afterPainted = function() {
        self._lastSig = entrySig;
        // 面积序列的描边/填充透明度已全部交给 CSS（见 _styles.html），这里不用再后处理 ——
        // 之前用 JS 改 stroke 会被 ApexCharts 的后续重绘覆盖，CSS 优先级高于属性所以稳定
        // 渲染后连续几轮收敛检查：布局渐变（如侧栏宽度的 0.2s 过渡）期间每次重建读到的
        // 都是中间值，而渐变结束后可能不再触发 ResizeObserver —— 定时兜底直到尺寸对齐
        [160, 400, 800].forEach(function(delay) {
          self._convergeTimer = setTimeout(function() {
            if (!self.isConnected || !self._config || !self._chart) return;
            if (self._sizeSignature() !== self._lastSig) self.render(self._config);
          }, delay);
        });
      };

      akmLoadApex().then(function(ApexCharts) {
        if (!self.isConnected) return;
        var host = self.querySelector('[data-akm-chart]');
        var chartHost = host && host.querySelector('[data-akm-canvas]');
        var overlayEl = host && host.querySelector('[data-akm-overlay]');
        if (self._chart && chartHost) {
          // 已有实例：原地更新尺寸 / 数据 / 样式，不销毁重建 ——
          // 切换 1d / 7d / 30d 或换指标时不再闪一下（此前每次 innerHTML 清空重建，
          // 清空那一帧组件高度塌陷，肉眼就是「图表宽度/位置抖一下」）
          host.style.height = height + 'px';
          chartHost.style.width = width + 'px';
          chartHost.style.height = height + 'px';
          if (overlayEl) {
            overlayEl.textContent = overlayText;
            overlayEl.style.display = allZero ? 'flex' : 'none';
          }
          try {
            // updateOptions 是异步的：必须等它 resolve 再恢复面积描边，否则随后 ApexCharts
            // 重绘会把 stroke 又变回半透明（总 Token 线再次看不见）
            var upd = self._chart.updateOptions(options, true, false);
            if (upd && typeof upd.then === 'function') upd.then(afterPainted);
            else afterPainted();
          } catch (e) {
            // 极少数结构不兼容（如序列数突变）时退回重建
            self._destroy();
            self.innerHTML = '';
            self.render(self._config);
            return;
          }
          return;
        }
        // 首次：建实例
        self._destroy();
        self.innerHTML = '';
        host = document.createElement('div');
        host.setAttribute('data-akm-chart', '1');
        host.style.position = 'relative';
        host.style.width = '100%';
        host.style.height = height + 'px';
        overlayEl = document.createElement('div');
        overlayEl.setAttribute('data-akm-overlay', '1');
        overlayEl.style.cssText = 'position:absolute;inset:0;display:' + (allZero ? 'flex' : 'none') +
          ';align-items:center;justify-content:center;pointer-events:none;font-size:11px;color:' + tickColor;
        overlayEl.textContent = overlayText;
        host.appendChild(overlayEl);
        chartHost = document.createElement('div');
        chartHost.setAttribute('data-akm-canvas', '1');
        // 显式像素尺寸：保证 ApexCharts 初始化时能读到确定宽高（% 宽在未布局容器上会算出 NaN）
        chartHost.style.width = width + 'px';
        chartHost.style.height = height + 'px';
        host.appendChild(chartHost);
        self.appendChild(host);
        self._chart = new ApexCharts(chartHost, options);
        self._chart.render().then(afterPainted);
      }).catch(function(err) {
        // 依赖缺失时显式报错，不伪造数据或静默空白
        self.innerHTML = '<div class="flex items-center justify-center text-xs text-red-400" style="height:' + height + 'px">图表库加载失败：' + akmEscapeHtml(err && err.message ? err.message : '未知错误') + '</div>';
      });
    }
  });
}

// ── akm-donut-chart：通用环形图壳组件（底层同样基于本地 ApexCharts）──
// 用法（与旧版内联 SVG 实现一致）：
//   donut.render({
//     items: [{ label, value, details }],
//     unit, format, maxSlices, size, centerLabel, emptyText, palette
//   });
// 交互：悬浮扇区 → ApexCharts 浮层（名称 / 数值 / 占比 / details），圆心自动切换为该片数值；
//       悬浮右侧图例行 → 同样内容（页面级浮层 akmFloatingTip，图例带数值与占比，
//       ApexCharts 原生 legend 做不到，所以保留自绘）。
if (!customElements.get('akm-donut-chart')) {
  customElements.define('akm-donut-chart', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      this.style.display = 'block';
      this._themeObs = akmObserveTheme(this);
    }

    disconnectedCallback() {
      if (this._themeObs) { this._themeObs.disconnect(); this._themeObs = null; }
      clearTimeout(this._themeTimer);
      if (this._tipHandle) { this._tipHandle.destroy(); this._tipHandle = null; }
      if (this._chart) { try { this._chart.destroy(); } catch (e) {} this._chart = null; }
    }

    _tip() {
      if (!this._tipHandle) this._tipHandle = akmFloatingTip();
      return this._tipHandle;
    }

    render(config) {
      config = config || {};
      this._config = config;
      var self = this;
      var unit = config.unit || '';
      var format = typeof config.format === 'function'
        ? config.format
        : function(v) { return String(v); };
      var size = Math.max(120, parseInt(config.size || 168, 10) || 168);
      var emptyText = config.emptyText || '暂无数据';
      var maxSlices = Math.max(1, parseInt(config.maxSlices || 6, 10) || 6);
      var palette = config.palette || [
        '#818cf8', '#34d399', '#fbbf24', '#f87171', '#38bdf8', '#a78bfa', '#f472b6', '#2dd4bf'
      ];

      var raw = (Array.isArray(config.items) ? config.items : []).map(function(item) {
        return {
          label: String((item && item.label) || ''),
          value: Number((item && item.value) || 0),
          details: (item && item.details) || []
        };
      }).filter(function(item) { return item.value > 0; });
      raw.sort(function(a, b) { return b.value - a.value; });

      if (!raw.length) {
        if (this._chart) { try { this._chart.destroy(); } catch (e) {} this._chart = null; }
        this.innerHTML = '<div class="flex items-center justify-center text-xs text-gray-600" style="height:' + size + 'px">' + akmEscapeHtml(emptyText) + '</div>';
        return;
      }

      var total = raw.reduce(function(sum, item) { return sum + item.value; }, 0);
      // maxSlices 是「含其他」的总片数：长尾时留一片给「其他」，其余按大小取前几项
      var merged = raw.length > maxSlices ? raw.slice(maxSlices - 1) : [];
      var visible = merged.length ? raw.slice(0, maxSlices - 1) : raw;
      var slices = visible.map(function(item) {
        return { label: item.label, value: item.value, details: item.details };
      });
      if (merged.length) {
        var names = merged.map(function(item) { return item.label; }).join('、');
        slices.push({
          label: '其他',
          value: merged.reduce(function(sum, item) { return sum + item.value; }, 0),
          details: ['合并 ' + merged.length + ' 项：' + (names.length > 60 ? names.slice(0, 60) + '…' : names)]
        });
      }
      slices.forEach(function(item, i) {
        item.color = palette[i % palette.length];
        item.share = item.value / total;
      });

      var tickColor = akmCssColor('--c-gray-500', '#94a3b8');
      var strongColor = akmCssColor('--c-gray-200', '#e5e7eb');
      var borderColor = akmCssColor('--c-border', '#334155');
      var fontFamily = '';
      try { fontFamily = window.getComputedStyle(document.body).fontFamily || ''; } catch (e) { fontFamily = ''; }


      // 纯文本行（akmFloatingTip 用 textContent 写入，无需转义；环形图浮层不走 ApexCharts ——
      // 它挂在图表容器内部，会被卡片的 overflow-hidden 裁掉）
      var sliceTipLines = function(s) {
        var lines = [
          s.label + ': ' + format(s.value) + unit,
          '占比 ' + (s.share * 100).toFixed(1) + '%'
        ];
        var details = Array.isArray(s.details) ? s.details : [s.details];
        details.forEach(function(line) {
          if (line != null && String(line) !== '') lines.push(String(line));
        });
        return lines;
      };

      // 扇区高亮 / 圆心切换：扇区悬浮（ApexCharts events）与图例悬浮（自绘 DOM）双向共用。
      // ApexCharts 没有公开「高亮指定扇区」的 API，直接改扇区 path 的样式（加亮目标、淡出其余）。
      var highlightSlice = function(index) {
        self.querySelectorAll('g.apexcharts-series').forEach(function(g, gi) {
          var p = g.querySelector('path');
          if (!p) return;
          p.style.opacity = index < 0 || gi === index ? '1' : '0.4';
          p.style.filter = gi === index ? 'brightness(1.1)' : '';
        });
      };
      var highlightLegendRow = function(index) {
        self.querySelectorAll('[data-legend]').forEach(function(r, ri) {
          r.style.background = ri === index ? 'rgba(99,102,241,.12)' : '';
        });
      };

      var options = {
        chart: {
          type: 'donut',
          width: size,
          height: size,
          fontFamily: fontFamily,
          background: 'transparent',
          foreColor: tickColor,
          animations: { enabled: false },
          toolbar: { show: false },
          zoom: { enabled: false }
        },
        series: slices.map(function(s) { return s.value; }),
        labels: slices.map(function(s) { return s.label; }),
        colors: slices.map(function(s) { return s.color; }),
        // 片间缝用页面分隔色勾边（旧版是 stroke-dasharray 留缝，视觉一致）
        stroke: { width: 2, colors: [borderColor] },
        // 圆心文字（默认 total，悬浮切到该片）不走 ApexCharts 的 donut.labels ——
        // 它与 dataLabels 渲染耦合、实测不稳定，改为自绘 overlay（见下方 then 里）；
        // dataLabels 关掉，���免片上出现百分比数字
        dataLabels: { enabled: false },
        // ApexCharts 默认会在图下方渲染自己的 legend，挤压绘图区把环缩成小圈、
        // 还和自绘图例重复 —— 必须显式关掉（实测）
        legend: { show: false },
        plotOptions: {
          donut: { size: '78%' }
        },
        events: {
          dataPointMouseEnter: function(e, _ctx, cfg) {
            var i = cfg && cfg.dataPointIndex != null ? cfg.dataPointIndex : -1;
            var s = slices[i];
            if (!s) return;
            highlightSlice(i);
            highlightLegendRow(i);
            // 页面级浮层（fixed 挂 body），不会被卡片 overflow-hidden 裁掉
            self._hoverSlice = s;
            self._tip().show(sliceTipLines(s), e.clientX, e.clientY);
          },
          dataPointMouseLeave: function() {
            highlightSlice(-1);
            highlightLegendRow(-1);
            self._hoverSlice = null;
            self._tip().hide();
          }
        },
        states: { hover: { filter: { type: 'none' } }, active: { filter: { type: 'none' } } },
        // 环形图浮层已改走 akmFloatingTip（见 events），关掉 ApexCharts 自带浮层
        tooltip: { enabled: false }
      };

      var legendHtml = slices.map(function(item, i) {
        return '<div data-legend="' + i + '" class="flex items-center gap-2 px-1.5 py-1 rounded cursor-default">'
          + '<span class="w-2.5 h-2.5 rounded-sm shrink-0" style="background:' + item.color + '"></span>'
          + '<span class="flex-1 truncate text-xs text-gray-300" title="' + akmEscapeHtml(item.label) + '">' + akmEscapeHtml(item.label) + '</span>'
          + '<span class="text-xs text-gray-400 tabular-nums shrink-0">' + akmEscapeHtml(format(item.value) + unit) + '</span>'
          + '<span class="text-xs text-gray-500 tabular-nums shrink-0 w-11 text-right">' + (item.share * 100).toFixed(1) + '%</span>'
          + '</div>';
      }).join('');

      akmLoadApex().then(function(ApexCharts) {
        if (!self.isConnected) return;
        if (self._chart) { try { self._chart.destroy(); } catch (e) {} self._chart = null; }
        self.innerHTML = '<div style="display:flex;align-items:center;gap:14px">'
          + '<div data-donut-host style="position:relative;flex:0 0 auto;width:' + size + 'px;height:' + size + 'px"></div>'
          + '<div style="flex:1;min-width:0">' + legendHtml + '</div>'
          + '</div>'
          + (merged.length
            ? '<div class="text-[10px] text-gray-600 mt-2">仅显示前 ' + maxSlices + ' 项，其余合并为「其他」</div>'
            : '');
        var host = self.querySelector('[data-donut-host]');
        var chart = new ApexCharts(host, options);
        self._chart = chart;
        // 圆心不叠加任何文字（用户要求去掉），数值与占比由右侧图例承担
        chart.render();
        var tip = self._tip();
        self.querySelectorAll('[data-legend]').forEach(function(row) {
          row.addEventListener('mouseenter', function() {
            var i = parseInt(row.getAttribute('data-legend'), 10);
            var s = slices[i];
            if (!s) return;
            var box = row.getBoundingClientRect();
            tip.show(sliceTipLines(s), box.left + box.width / 2, box.top);
            highlightLegendRow(i);
            highlightSlice(i);
          });
          row.addEventListener('mouseleave', function() {
            tip.hide();
            highlightLegendRow(-1);
            highlightSlice(-1);
          });
        });
      }).catch(function(err) {
        self.innerHTML = '<div class="flex items-center justify-center text-xs text-red-400" style="height:' + size + 'px">图表库加载失败：' + akmEscapeHtml(err && err.message ? err.message : '未知错误') + '</div>';
      });
    }
  });
}

