# Nano Banana 2.1 接入与验收

核实日期：**2026-10-07**。

BananaPrism 的 Nano Banana 2.1 支持文生图和局部标注编辑，尺寸为 `1K`、`2K`、`4K`，默认 `1K`。本页区分已核实的公开协议与仍需用户 API Key 完成的实际生图验证。

## 模型与请求

| 平台 | 实际发送的模型 ID | 新模型使用的接口 | 鉴权 |
| --- | --- | --- | --- |
| OpenRouter | `google/gemini-nano-banana-2.1` | `POST https://openrouter.ai/api/v1/images` | `Authorization: Bearer <API_KEY>` |
| AIHubMix | `gemini-nano-banana-2.1` | `POST https://aihubmix.com/gemini/v1beta/models/gemini-nano-banana-2.1:generateContent` | `x-goog-api-key: <API_KEY>` |

两者均发送 `Content-Type: application/json`。应用内部保存带 `google/` 前缀的模型 ID，AIHubMix 适配器会移除该前缀。AIHubMix 使用官方文档中的稳定主机 `aihubmix.com`。

OpenRouter 的最小文生图请求体：

```json
{
  "model": "google/gemini-nano-banana-2.1",
  "prompt": "白色背景上的红色陶瓷杯，柔和的摄影棚光线",
  "resolution": "1K",
  "aspect_ratio": "1:1",
  "n": 1
}
```

2.1 的三个尺寸均使用 Images API；此接口不使用 Chat Completions 的 `modalities` 或 `image_config`。编辑时添加 `input_references`，按原图、彩色标注图的顺序发送两项：

```json
{
  "type": "image_url",
  "image_url": { "url": "data:image/jpeg;base64,<图像数据>" }
}
```

结果图片从 `data[].b64_json` 读取，并结合 `media_type` 校验实际格式；Images API 通常不提供模型文字说明。

AIHubMix 的最小文生图请求体：

```json
{
  "contents": [
    { "role": "user", "parts": [{ "text": "白色背景上的红色陶瓷杯，柔和的摄影棚光线" }] }
  ],
  "generationConfig": {
    "responseModalities": ["IMAGE", "TEXT"],
    "imageConfig": { "aspectRatio": "1:1", "imageSize": "1K" }
  }
}
```

编辑时在同一个 user 消息的 `parts` 中放入说明、原图、标注说明和彩色标注图；图片采用 `inlineData: {"mimeType":"image/jpeg","data":"<Base64>"}`。结果从 `candidates[].content.parts[].inlineData` 读取，字段为 `mimeType` 和 `data`。尺寸中的 `K` 必须大写。

OpenRouter 的模型 endpoint 目录确认支持以下 14 种比例：

`1:1`、`1:4`、`1:8`、`2:3`、`3:2`、`3:4`、`4:1`、`4:3`、`4:5`、`5:4`、`8:1`、`9:16`、`16:9`、`21:9`。

该目录还明确 `n` 只能为 `1`、最多 14 张参考图、无流式支持。Google 的模型说明确认 1K/2K/4K 和宽幅比例；2.1 不支持 512 尺寸。BananaPrism 本版界面沿用六种比例（`16:9`、`1:1`、`4:3`、`3:2`、`9:16`、`3:4`），局部编辑固定发送两张等尺寸网络副本，黑白选区蒙版留在本地。

## 非流式响应与编辑上下文

OpenRouter 2.1 的公开能力目录明确不支持流式；AIHubMix 2.1 使用非流式 `generateContent` 获取完整图片。AIHubMix 的 Gemini 图像指南记录过 Pro 预览版流式只返回推理、不返回图片的情况，因此这里不依赖流式传输图片；这不是对 2.1 流式能力的实测结论。

Gemini 的 `thought: true` part 可能含中间思考图，不能作为最终生成图片。解析器会跳过这些 part，只展示最终文字和图片。

当前每次编辑都是独立请求：发送原图和彩色标注图两个参考输入，不提交此前 assistant/model 消息，也不维护多轮会话。因此无需重放 `thoughtSignature`。若以后实现真正的多轮会话，需要保存并原样回传模型历史及签名，不能把当前展示时的过滤逻辑用作历史存储逻辑。

## 验证范围

- 已核实两平台公开模型 ID、官方请求/响应协议，以及 OpenRouter 2.1 endpoint 的实时能力目录。
- AIHubMix 已公开该模型的 Chat Completions 入口，但查询时参数 schema 目录尚无 2.1。这里的原生适配依据 AIHubMix 的 Gemini 原生代理声明及 Google 协议；不把通用代理声明视为该模型已经实测成功。
- 本次未使用用户 API Key 发起付费生成或编辑；自动化协议测试不等于真实服务验收。网络、账户权限、余额和上游供应商可用性仍需以下步骤确认。

## 最短手工验收

使用各平台有余额、获准调用该模型的 API Key。每个平台先只做一次 1K 生成和一次 1K 编辑，共四次付费请求。

1. 打开应用，在“控制台”添加并保存 OpenRouter、AIHubMix 两组 API 预设；回到主界面选择 OpenRouter 预设和 Nano Banana 2.1。
2. 选择 `1K`、`1:1`，输入“白色背景上的红色陶瓷杯，柔和的摄影棚光线”，发送一次生成请求。确认有可打开的图片和同名 JSON sidecar，记录实际模型、尺寸、日志和保存路径；仅 HTTP 200 或仅文字不算生图成功。
3. 导入该图片作为工作图，进入“局部编辑”，框选杯身，输入“将选中杯身改为蓝色，保留背景和杯子造型”。在预检中确认两张输入顺序和 `1K` 参数，再发送一次。确认返回可打开的完整图片，杯身变化且无标注色残留；检查未选区域的保持情况。
4. 切换 AIHubMix 预设，重复第 2、3 步。OpenRouter 结果没有文字说明是正常情况。

如需验证高分辨率，再自行选择一次 `2K`/`4K` 请求，并检查真实输出像素；会另行产生费用。遇到失败先核对日志中的 HTTP 状态、模型 ID 和平台权限，不通过切换旧模型或降低尺寸来算作 2.1 验收通过。

## 官方来源

- [OpenRouter Nano Banana 2.1 模型页](https://openrouter.ai/google/gemini-nano-banana-2.1)
- [OpenRouter 2.1 endpoint 能力目录](https://openrouter.ai/api/v1/images/models/google/gemini-nano-banana-2.1/endpoints)
- [OpenRouter Images API：请求、参考图和响应格式](https://openrouter.ai/docs/guides/overview/multimodal/image-generation)
- [AIHubMix 2.1 模型说明及公开接口](https://aihubmix.com/model/gemini-nano-banana-2.1/llms.txt)
- [AIHubMix Gemini 原生 Generate Content](https://docs.aihubmix.com/cn/api-reference/google-vertex-ai-compatible/generate-content)
- [AIHubMix Gemini 原生 SDK 与基础地址](https://docs.aihubmix.com/cn/api/Gemini-SDK)
- [AIHubMix Gemini 图像配置与非流式说明](https://docs.aihubmix.com/cn/api/Gemini-Guides)
- [Google Nano Banana 2.1 能力](https://ai.google.dev/gemini-api/docs/models/gemini-nano-banana-2.1)
- [Google 图像生成：尺寸与比例](https://ai.google.dev/gemini-api/docs/image-generation)
- [Google Generate Content 的 Part、thought 和 thoughtSignature](https://ai.google.dev/api/generate-content#Part)
