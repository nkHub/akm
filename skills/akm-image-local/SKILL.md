---
name: akm-image-local
description: Use when the user wants to generate images, edit existing images, remove backgrounds, repaint regions, or produce high-constraint prompts through the local AKM image service instead of a remote image gateway
---

# AKM Local Image Skill

用于通过本地 AKM 服务直接调用图片生成与图片编辑接口：

- 生成：`http://127.0.0.1:8800/v1/images/generations`
- 编辑：`http://127.0.0.1:8800/v1/images/edits`

适用场景：

- 用户要你生成图片
- 用户要你基于已有图片做编辑/重绘/去背景/局部替换
- 需要稳定产出适合 `gpt-image-2` 的提示词

## 使用原则

1. 默认使用本地 AKM 服务，不直接请求远端图片网关。
2. 生成图片时，优先构造适合 `gpt-image-2` 的高约束提示词，减少反复重试。
3. 编辑图片时，提示词要明确说明：
   - 保留什么
   - 修改什么
   - 风格/材质/光照
   - 不要改变什么
4. 未显式指定时，优先使用：
   - `model`: `gpt-image-2`
   - `size`: `1024x1024`
   - `quality`: `high`
   - `output_format`: `png`
5. 如果用户没有给出足够约束，先补齐缺失信息再调用；若缺失不影响执行，可根据下面模板直接帮用户组织提示词。

## 生成图片调用格式

请求地址：

```text
http://127.0.0.1:8800/v1/images/generations
```

请求方法：

```text
POST
```

请求头：

```json
{
  "Content-Type": "application/json"
}
```

推荐请求体：

```json
{
  "prompt": "<生成提示词>",
  "model": "gpt-image-2",
  "size": "1024x1024",
  "quality": "high",
  "background": "opaque",
  "output_format": "png",
  "n": 1
}
```

## 图片编辑调用格式

请求地址：

```text
http://127.0.0.1:8800/v1/images/edits
```

请求方法：

```text
POST
```

请求类型：

```text
multipart/form-data
```

推荐表单字段：

```text
image: <原图文件>
mask: <可选蒙版文件>
prompt: <编辑提示词>
model: gpt-image-2
size: 1024x1024
quality: high
output_format: png
n: 1
```

## 生成提示词模板

适合通用出图的结构：

```text
主体：<要画的核心对象>
场景：<发生地点 / 环境>
构图：<远景 / 中景 / 特写 / 俯视 / 正面>
风格：<写实 / 插画 / 海报 / 3D / 水彩 / 赛博朋克等>
光照：<晨光 / 柔光 / 电影光 / 霓虹光>
细节：<材质 / 表情 / 动作 / 色彩 / 道具>
约束：<不要出现什么 / 保持画面干净 / 无文字水印>
```

推荐最终提示词写法：

```text
Create a highly detailed image of <主体> in <场景>. Composition is <构图>. Style is <风格>. Lighting is <光照>. Include details such as <细节>. Keep the image clean and cohesive. Do not add watermark, extra text, distorted anatomy, or unrelated objects.
```

## 图片编辑提示词模板

编辑提示词一定要强调“保留原图哪些部分”，否则模型更容易大改。

推荐结构：

```text
保留：<人物身份 / 主体轮廓 / 姿态 / 主要配色 / 构图>
修改：<背景 / 服装 / 表情 / 材质 / 文字 / 局部元素>
风格：<写实 / 插画 / 电商图 / 产品图 / 海报>
质量要求：<边缘干净 / 光影统一 / 高清 / 无伪影>
禁止：<不要改脸 / 不要改姿势 / 不要新增多余元素>
```

推荐最终编辑提示词写法：

```text
Edit this image while preserving the original subject identity, pose, overall composition, and key visual balance. Change only the following parts: <修改项>. Keep the style as <风格>. Make the result clean, high-resolution, and visually consistent, with natural lighting and accurate edges. Do not alter <禁止修改项> and do not introduce unrelated new elements.
```

## 常用编辑提示词示例

### 去背景

```text
Edit this image while preserving the original subject, pose, and proportions. Remove the background completely and replace it with a clean transparent background. Keep edges clean and natural, especially around hair, fur, and semi-transparent details. Do not change the subject itself.
```

### 商品图换纯色背景

```text
Keep the product exactly the same in shape, material, and color. Replace the current background with a clean light gray studio background. Keep realistic soft shadows under the product, maintain sharp edges, and do not change the camera angle or product proportions.
```

### 人像换场景

```text
Preserve the person's face, hairstyle, pose, body proportions, and outfit silhouette. Replace the background with a modern cafe interior with warm natural lighting. Keep the image realistic and consistent, with clean separation between subject and background. Do not change facial identity.
```

### 局部重绘

```text
Only modify the masked area. Keep all unmasked regions unchanged. In the masked area, replace it with <目标内容>. Match the original lighting, perspective, color temperature, and texture so the edit blends seamlessly into the existing image.
```

## 返回结果说明

本地 AKM 图片接口通常返回 OpenAI 风格 JSON，例如：

```json
{
  "created": 1234567890,
  "data": [
    {
      "b64_json": "..."
    }
  ]
}
```

若返回 `data[].b64_json`，说明结果是 base64 图片内容；若返回 `data[].url`，说明结果是图片 URL。

## 执行建议

1. 用户只说“生成一张图”时，优先帮他补成完整提示词后再调用。
2. 用户要编辑图片时，优先询问是否：
   - 只改局部
   - 保留主体身份
   - 需要透明背景
3. 若用户没有明确给尺寸，默认 `1024x1024`。
4. 若用户追求质量，默认 `quality=high`；若更关心速度，可手动改 `medium` 或 `low`。
