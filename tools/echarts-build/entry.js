// 为「门店 AI 管理智能体」生成按需裁剪的 echarts IIFE 包（A2B-S2）
//
// 关键认知（2026-09-18 实测）：echarts 5 的 `lib/chart/*.js` 与 `lib/component/*.js`
// **没有任何导出**——它们内部 `import { install } from './x/install.js'; use(install);`
// 即「副作用自注册」。因此正确用法是纯副作用 import，**不能**把它们当组件对象传给 use()
// （那会报 "install is not a function"）。渲染器例外：`echarts/renderers` 具名导出。
//
// 组件清单来自门店实测：dashboard.html（line/bar/pie + dataZoom inside&slider + graphic 空态）
// + training.html（radar）。
import * as echartsNS from 'echarts/core'

import 'echarts/lib/chart/line'
import 'echarts/lib/chart/bar'
import 'echarts/lib/chart/pie'
import 'echarts/lib/chart/radar'

import 'echarts/lib/component/grid'
import 'echarts/lib/component/tooltip'
import 'echarts/lib/component/legend'
import 'echarts/lib/component/title'
import 'echarts/lib/component/graphic'
import 'echarts/lib/component/radar'
import 'echarts/lib/component/dataZoom'
import 'echarts/lib/component/dataZoomInside'
import 'echarts/lib/component/dataZoomSlider'

import { CanvasRenderer, SVGRenderer } from 'echarts/renderers'

const echarts = echartsNS
echarts.use([CanvasRenderer, SVGRenderer])

window.echarts = echarts
export default echarts
