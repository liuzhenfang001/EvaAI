# 三期「Rag 知识库」多模态补篇：图片 / 文档 + 文本

> 研究时间：**2026-10-11**。本文是 [`rag-knowledge-base.md`](./rag-knowledge-base.md) 的**多模态补篇**，
> 与它并列，**不重复**它已覆盖的文本侧内容（LangChain v1 生态、向量库横评、嵌入、切分解析、检索、
> 集成形态、知识库接入，即那份的 §1–§7）。凡是文本侧已经定过的事，本文只**引用**并给回链，不重开。
>
> 体裁约定沿用前篇：每条结论追到**一手来源**（官方文档 / 源码 / 模型卡 / first-party API 文档），
> 正文给 Markdown 链接；只有二手来源的一律标「**仅二手，未验证**」。**版本与价格均为 2026-10-11 时点。**
> 本机 `.venv` 实读/实测与官方文档核实过的条目标「**已核实**」，仅来自检索摘要的标「**未核实**」。

---

## 0. 本项目的既有约束（多模态维度的新增事实）

前篇 §0 的约束全部照旧，这里只列**因「图片」而新增的两条基线事实**，它们直接改变后面的结论。

| 新增事实 | 出处 | 对多模态的含义 |
|---|---|---|
| 主模型 `deepseek-flash` = **DeepSeek-V4.1-Flash，原生多模态视觉理解**（图像理解：支持；上下文 1M、输出 384K、Tool Calls 支持） | [DeepSeek 定价页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)（一手，已核实）；[V4.1-Flash 发布公告](https://api-docs.deepseek.com/zh-cn/news/news260910/)（一手） | **agent 主模型本来就能看图**，喂图不需要换模型、不需要额外的视觉模型。这是本文最重要的一个「意外吻合」 |
| `read_file` **明确拒绝二进制**（`_text` 抛 `cannot read … as UTF-8 text — it looks like a binary file`） | 本机源码 `backend/packages/harness/evaharness/sandbox/file_ops.py`（实读） | **目前没有任何工具能把图片字节交回给模型**——这是要补的唯一实现缺口 |

DeepSeek 侧的一手细节（[Vision-Exp 公告](https://api-docs.deepseek.com/zh-cn/news/news260821/)，2026-08-21；V4.1-Flash 2026-09-10 起承接）：
图片走 user 消息的 `content` 数组，支持 **base64 内联**（≤32MiB/图）、**外部 URL**、**Files API `file_id`**（免带宽、可复用）三种传入；
按 token 计费，**一张图最多占 384 tokens**，与文本同价；图片理解支持 Chat Completions / Messages / Responses 三种格式。
> 读法：**384 tok/图**是上限，意味着「把图直接塞进上下文」的成本是**可预算**的（远低于把整页 OCR 文本塞进去的常见量级）——这与后文 §3「图片直接进消息」的可行性直接相关。

前篇 §0 的依赖基线里与本文相关的两条：`langchain-core 1.6.5`、`sqlite-vec 0.1.9`（已在 venv）；**未装** `pillow` / `pypdf` 之类图像或文档解析依赖。

---

## 1. 多模态嵌入：能把图与文嵌到同一空间的在线 API（问题 1）

**口径**：只收「**同一次查询里，文本和图像进同一个向量空间**」的在线 API。纯文本嵌入（OpenAI `text-embedding-3-*`、
DashScope `text-embedding-v4`、前篇 §3 已列的那些）不在此表。所有模型名均已对官方文档核实（标「已核实」）。

| 厂商 | 当前模型 id | 输入模态 | 默认维度 | 价格 | OpenAI 兼容 | 中文 |
|---|---|---|---|---|---|---|
| **Voyage AI** | **`voyage-multimodal-3.5`**（旧 `voyage-multimodal-3`） | **交错 文本+图+视频**，**单一 backbone**（官方强调不是 CLIP 双塔） | 1024（256/512/2048 可切） | $0.12/1M tok **+** $0.60/1B 像素 | **否**（`POST /v1/multimodalembeddings`） | 多模态版**官方未声明**——**未核实** |
| **Cohere** | **`embed-v5.0-pro` / `embed-v5.0-fast`**（旧 `embed-v4.0`） | 文本 / 图 / **图文融合（PDF 整页）** | 2048（256–2048） | pro $0.12、fast $0.08 /1M tok；**图 $0.40/1M** | **否**（`POST /v2/embed`） | 官方称 **100+ 语言**含中文 |
| **Jina** | **`jina-embeddings-v5-omni-small` / `-nano`** | **文本+图+视频+音频+PDF 同一空间**，且与 `v5-text-small` **对齐** | small 1024（Matryoshka 32–1024）/ nano 768 | 共享额度 ~$0.05/1M（nano ~$0.02），10M 免费 | 路径像 `/v1/embeddings`，但**图片用 Jina 自有 `input` 结构**，非标准 OpenAI | 93 语言 |
| **DashScope / 阿里百炼** | **`qwen3-vl-embedding`**（另有 `qwen2.5-vl-embedding`、`tongyi-embedding-vision-plus/-flash`、`multimodal-embedding-v1`） | 文本+图+视频同一语义空间；支持**独立向量**与**融合向量** | 2560（256–2560） | 图/视频 $0.258、文本 $0.1 /1M（国际站）；北京站 ¥0.0018/1k 图 | **否**（专用 multimodal-embedding 端点；只有**文本**嵌入走兼容模式） | **强**（33 语言含中文） |
| **Google** | **`gemini-embedding-2`**（`-001` 是纯文本） | **文本+图+视频+音频+PDF 统一空间** | 3072（128–3072） | $0.15/1M tok | 原生（非 OpenAI 协议） | 多语言（未逐项列） |
| **SiliconFlow**（聚合） | **`Qwen/Qwen3-VL-Embedding-8B`** | 文本+视觉 | —（32K） | ¥0.7/1M 输入，视觉 ¥1.8/1M | **是**（`api.siliconflow.cn/v1`） | 强（Qwen 系） |
| **OpenAI** | `text-embedding-3-small/large` | **纯文本，官方明确 Image "Not supported"** | 1536/3072 | $0.02/$0.13 | 原生 | 多语言（但仅文本） |

一手来源：[Voyage multimodal](https://docs.voyageai.com/docs/multimodal-embeddings)（已核实，页面含 `voyage-multimodal-3.5` 与 "interleaved"/"single backbone"）·
[Voyage 定价](https://docs.voyageai.com/docs/pricing) ·
[Cohere 模型页](https://docs.cohere.com/docs/models)（已核实，含 `embed-v5.0-pro/-fast`、`embed-v4.0`、"Images"/"Multimodal"）·
[Cohere 多模态](https://docs.cohere.com/docs/multimodal-embeddings) ·
[Jina v5-omni 卡](https://huggingface.co/jinaai/jina-embeddings-v5-omni-small)（已核实 "shared vector space"、"text, images"）·
[Jina v4 卡](https://huggingface.co/jinaai/jina-embeddings-v4)、[jina-clip-v2 卡](https://huggingface.co/jinaai/jina-clip-v2) ·
[DashScope 多模态嵌入 API](https://help.aliyun.com/zh/model-studio/multimodal-embedding-api-reference)（已核实，含 `qwen3-vl-embedding`、`tongyi-embedding-vision-plus/flash`、`multimodal-embedding-v1`）·
[Gemini Embeddings](https://ai.google.dev/gemini-api/docs/embeddings)（已核实，含 `gemini-embedding-2`、"Multimodal"）·
[SiliconFlow 嵌入目录](https://siliconflow.cn/models?q=embedding) ·
[OpenAI 模型页](https://developers.openai.com/api/docs/models/text-embedding-3-large)（Image "Not supported"）。

### 1.1 几条要记住的结论

- **OpenAI 官方仍在多模态嵌入上缺席**：`text-embedding-3-*` 官方模型页逐字写 Image "Not supported"、无更新端点。
  网传 `clip-image-embedding-*` **不在官方文档**，不可信。若沿用 OpenAI 嵌入，**拿不到图像向量**。
- **「图+文同空间」有两条技术路线，别混为一谈**：
  - **统一空间、非原味 CLIP**（Voyage multimodal-3.5 **自称单 backbone 交错**；Cohere、DashScope、Gemini embedding-2 官方称图文同空间；
    Jina v5-omni 官方称「shared vector space」，其技术是「Locked Aligned Towers」——对齐塔而非纯双塔）——
    都弱化了 CLIP 的「modality gap」。
  - **CLIP 双塔**（`jina-clip-v2`、BAAI `bge-visualized`）——文搜图/图搜图可行，但**混排文本+图的跨模态检索会退化**。
    主流做法（[Jina v5-omni 公告](https://jina.ai/news/jina-embeddings-v5-omni-multimodal-embeddings-for-text-image-audio-and-video)）
    是**用「Locked Aligned Towers（锁定对齐塔）」把各模态对齐到同一空间**，让跨模态检索不再像原味 CLIP 那样受 modality gap 拖累。
- **OpenAI 兼容性普遍缺席**：Voyage / Cohere / DashScope 多模态 / Gemini 都**不是** OpenAI 协议；
  **只有 SiliconFlow 明确 OpenAI 兼容**。这与前篇 §3.3「在线嵌入走 `OpenAIEmbeddings(base_url=...)`」的省事路线冲突——
  **多模态嵌入几乎一定要单独接一个 partner 包或自写 client**。
- **中文**：明确支持的是 **DashScope qwen3-vl-embedding（33 语）**、**Cohere（100+ 语）**、**Jina（93 语）**、**SiliconFlow/Qwen3-VL**。
  **Voyage 多模态的中文能力官方文档未声明**（前篇文本侧同样存疑）——选它等于把中文效果交给运气。

---

## 2. 图片 / 二进制在向量库底座里怎么存（问题 2）

**口径**：核实每个底座对「**非文本列**」（尤其是**原始图像字节**）的**原生**支持，再单独回答「**原图本身**放哪」。
前篇 §2 的向量库横评（部署、并发、元数据过滤、LangChain 适配）仍然成立，本节只补**二进制这一维**。

### 2.1 各底座对「原图字节」的原生能力（已核实）

| 底座 | 能否原生放原图字节 | 原话 / 依据 |
|---|---|---|
| **LanceDB / Lance 格式** | **能，且最完整** | 官方：`handles multimodal data—images, audio, video, and PDF files—natively by storing the raw bytes in a binary column … eliminating the need for separate object storage`。底层 Lance 有 **Blob API**：`Inline / Packed / Dedicated / External` 四级存储、**惰性/流式读**（`take_blobs` → `BlobFile`、`blob_mode="lazy"`）。阈值 **≤64KiB inline、64KiB–4MiB packed、>4MiB dedicated**，可 per-column 覆盖 |
| **sqlite-vec** | **能**（辅助列，官方专门给了存图的例子） | vec0 的 **auxiliary column**（`+` 前缀）「stores **any kind of data** in a separate internal table」，最多 16 个，**不能进 WHERE**、可进 SELECT。官方原例：`create virtual table vec_image_chunks using vec0( … +image blob );` 并写明「the image auxiliary column can store **the raw image file in a large BLOB column**」 |
| **pgvector** | **能**（但不是 pgvector 的功劳） | `vector` 只是普通列；把**标准 Postgres `bytea`** 与 `vector` 放**同一张表**即可，pgvector 不管 bytea |
| **Chroma** | **否** | metadata Schema 只认 6 种 value type（`string/int/float/bool/float_list/sparse_vector`），**没有 bytes**；原图只能 **base64 塞 str**（膨胀 ~33%，且进 metadata 索引/内存） |
| **Qdrant** | **否** | payload「any information that can be represented using **JSON**」，类型只有 integer/float/bool/keyword/geo/datetime/text/uuid，**无 binary**；只能 base64 塞 keyword |
| **Milvus** | **否** | 标量类型仅 VarChar/Boolean/Int/Float/Double/Array/JSON，**无 BLOB**；`VARCHAR.max_length` **上限 65,535 字节**，原图只能 base64 或外置 |

一手来源：[LanceDB 多模态](https://docs.lancedb.com/tables/multimodal)、[Lance 格式](https://docs.lancedb.com/lance)、
[Lance Blob 指南](https://lance.org/guide/blob/)、
[sqlite-vec vec0](https://alexgarcia.xyz/sqlite-vec/features/vec0.html)（已核实，含 `+image blob` 原例）、
[pgvector README](https://github.com/pgvector/pgvector)、
[Chroma Schema 参考](https://docs.trychroma.com/cloud/schema/index-reference)、[Chroma add-data](https://docs.trychroma.com/docs/collections/add-data)、
[Qdrant payload](https://qdrant.tech/documentation/manage-data/payload/)、
[Milvus schema](https://github.com/milvus-io/milvus-docs/blob/v2.5.x/site/en/userGuide/schema/schema.md)、[Milvus string 字段](https://github.com/milvus-io/milvus-docs/blob/v2.5.x/site/en/userGuide/schema/string.md)。

> 两个「能」是**性质不同**的：**LanceDB** 的二进制是**一等列 + 惰性/流式读 + 外置 URI 逃生门**；
> **sqlite-vec** 的辅助列是「**随行取回**」——它和向量同表、不必 JOIN，但**读就是全量读**（无惰性）。
> 对「缩略图级小图」两者都行；对「原图/大图」，只有 LanceDB 有部分读与大对象分流。

### 2.2 原始图像 / 文档**本身**放哪（单机个人助手约束）

权威依据是 SQLite 官方两篇（一手）：
[Internal Versus External BLOBs](https://www.sqlite.org/intern-v-extern-blob.html) 的结论是
**BLOB < ~100KB 存库内更快、> ~100KB 独立文件更快**（交叉点在 100KB，page size 8192/16384 最佳）；
[35% Faster Than The Filesystem](https://www.sqlite.org/fasterthanfs.html) 补充**小 blob（缩略图级）存库比单个文件快约 35%、省约 20% 磁盘**，
但**冷文件系统缓存下可能反过来、大 blob 文件系统占优**。

在「单机个人助手、没有 S3/MinIO」下，「外置」就是**本地文件系统 + DB 里存一个路径指针**。综合成一条可选规则：

- **缩略图 / 小图（< ~100KB）**：进 DB（sqlite-vec 辅助列 blob / LanceDB inline，或本地库里）。
- **原图 / 大图 / 视频**：**落在文件系统**（本项目已有 `/input`、`/workspace` 挂载），DB 里只留**虚拟路径**。
- 这条与各库官方立场一致：**Chroma 自己就劝你把大图外置**（add-data 页原句：大图/视频「use the ids to associate records with your external documents」）；
  **LanceDB** 默认鼓励入库但同样给 `s3://` External URI 逃生门。

> **与现有架构的吻合**：本项目沙箱本来就把「用户上传」放 `/input`、工作产物放 `/workspace`，都是**文件系统**。
> 「原图落文件系统 + 库内指针」不需要任何新存储层，**直接复用沙箱挂载**。前篇 §7.1「`/input` 只读上传 + 目录挂载」的口子原样可用。

---

## 3. 检索到图片后怎么交给模型（问题 3）

本节所有 API 名与字段**均对 `backend/.venv` 实装源码核实**（`langchain-core 1.6.5` / `langchain-openai 1.6.6` / `langgraph 1.2.12`），
比只查文档更硬。

### 3.1 把图放进消息：**标准的 v1 内容块是「扁平」的**（已核实，且与旧记忆不同）

`langchain-core 1.x` 的图像内容块是 **`ImageContentBlock(TypedDict)`**，`type="image"`，字段**扁平**：
`url` / `base64` / `file_id` / `mime_type`（+ 自动生成的 `id`）。
工厂函数 `create_image_block(*, url=None, base64=None, file_id=None, mime_type=None, ...)`，
**三者必给其一**，**base64 必须配 `mime_type`**（否则 `ValueError`）。用法：

```python
from langchain_core.messages.content import create_image_block
from langchain_core.messages import HumanMessage

msg = HumanMessage(content_blocks=[
    {"type": "text", "text": "这张图里有什么？"},
    create_image_block(base64=..., mime_type="image/png"),   # 或 url=... 或 file_id=...
])
```

- **必须改掉旧记忆**：`{"type":"image","source_type":"base64","data":...}` 是 **v0 老式块**——langchain-core 仍**容忍**它
  （`is_data_content_block` 对新旧都返回 True），但 v1 标准**产出的是扁平形式**，别再写 `source_type`/`data`。
- `{"type":"image_url","image_url":{"url":...}}` 是 **OpenAI provider 原生格式**，**仍然有效**（原样透传），但那是 OpenAI 专用，不是「标准块」。
- `BaseMessage.content_blocks`（`langchain-core ≥1.0` 起）会把 `content` 归一成标准块列表；
  可用 `LC_OUTPUT_VERSION=v1` 或 `init_chat_model(..., output_version="v1")` 让模型也**输出** v1 块。

### 3.2 `create_agent` 能不能直接吃多模态？（能）

`create_agent` 建的是普通 LangGraph 图，用户消息就是 state 里的 `HumanMessage` ——
**把图像块挂在 `HumanMessage` 上即可**，agent 主路径**零特殊处理**。
唯二约束：**（a）模型得支持这个模态**（本项目 `deepseek-flash` 支持，见 §0）；
**（b）长会话的摘要压缩是「纯文本」的**——Deep Agents 官方多模态说明里点明：压缩时只数**文本** token，且摘要会把旧的图/音/文件块**丢掉**。
结论：**图片不该长期留在消息里**，应放 `/workspace` 挂载、在消息里传**路径或 URL**（不是内联 base64）——**这正是本项目「结果落 `/workspace` 返回路径」的既定做法**（前篇 §6.2）。

### 3.3 工具的返回值里带图片：**可行，且是官方文档化的形状**

`@tool` 直接返回一个**标准内容块列表**，运行时把它转成多模态 `ToolMessage`：

```python
@tool
def show_image() -> list[dict]:
    """把一张图交给模型。"""
    return [
        {"type": "text", "text": "命中的图片："},
        {"type": "image", "url": "https://..."},           # 或 base64 + mime_type
    ]
```

- 允许的块类型（实装源码 `langchain_core/tools/base.py`）：`TOOL_MESSAGE_BLOCK_TYPES = ("text","image_url","image","json","search_result","custom_tool_call_output","document","file")`。
- `ToolMessage` 构造器同时接受 `content`、`content_blocks: list[ContentBlock]`、**`artifact: Any`**、`status: "success"|"error"`。
  `artifact` 的字段说明是「**not meant to be sent to the model**」——即**只有一部分进模型、全量留在 artifact**，
  官方示例正是 `artifact={"type":"image","base64_data":"/9j/..."}`。
- 工具返回 `Command` 也可以，但若目标是当前图，**必须**带上 `tool_call_id` 匹配的 `ToolMessage`，否则 ToolNode 抛 `ValueError`。
- MCP 工具返回图片/mixed 内容走**同一条**转换路径。

### 3.4 与本项目的两个具体落点

- **好消息**：**把检索到的图片喂给模型**这条链，`langchain-core` 已经全部支持——
  一个工具返回 `[{"type":"text",...},{"type":"image","url":...}]` 就能让 DeepSeek Flash 看见图。**不需要动 agent 主路径。**
- **缺口**：**当前没有任何工具能从沙箱读二进制**（`read_file` 拒绝非 UTF-8）。而 DeepSeek 的图只能来自
  base64 / URL / file_id——**本地 `/workspace` 里的原图既不是 URL，也没有 file_id**。
  要打通「命中图片 → 返回 `image` 块」，**需要新增一个能读沙箱图片、产出 `image` 内容块的工具**（扩展 `read_file` 或新建）。
  这与前篇 §6.2「写 `/workspace` + 返回路径」的方案**冲突于一点**：模型拿到路径后，**没有工具能把该路径变成可见的图**——
  所以要么给「读图工具」，要么检索工具直接把图片以 `image` 块返回（而不是只返回路径）。

---

## 4. 图片入索引的三条路（问题 4）

三条路各自的代价，以及「PDF 里的内嵌图片怎么办」。

| | ① OCR 抽文字 | ② 视觉模型生成 caption → 嵌文本 | ③ 直接多模态嵌入 |
|---|---|---|---|
| 干什么 | 把图里的字识别成文本，进文本 RAG | VLM 写一段描述，把描述当文本嵌入 | 用 §1 的多模态模型直接嵌原图 |
| 延迟/入库 | 本地 CPU 即可（PaddleOCR/RapidOCR） | 每图一次 VLM 前向（本项目模型就是 VLM，见 §0） | 每图一次嵌入调用，无 OCR 步 |
| 成本 | 本地免费；云 OCR 按次 | 一次 VLM token（本项目 ≈ 图 ≤384 tok + 提示词） | 一次嵌入 token + 图按像素计价（Voyage/Cohere） |
| 检索质量 | 只拿到「字」，**版式/图形/照片全部丢失** | 保住语义，但**描述有损**（图表/表格尤其） | **上限最高**，支持图搜图/图搜文 |
| 中文 | **PaddleOCR 最强**（专用中文模型、竖排/表格/手写） | 取决于 VLM（本项目 DeepSeek Flash 中文强） | DashScope/Qwen3-VL、Jina、Cohere 明确中文；Voyage 存疑 |
| 许可 | 三者皆 Apache-2.0 | 随 VLM | BGE-VL **MIT**、GME/Qwen2-VL **Apache-2.0**、`jina-embeddings-v4` **CC-BY-NC-4.0（非商用）** |

一手来源：[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)（Apache-2.0）、
[RapidOCR](https://github.com/RapidAI/RapidOCR)（Apache-2.0）、
[Tesseract](https://github.com/tesseract-ocr/tesseract)（Apache-2.0）、
[BGE-VL](https://huggingface.co/BAAI/BGE-VL-base)（MIT）、
[GME / gme-Qwen2-VL-2B-Instruct](https://huggingface.co/Alibaba-NLP/gme-Qwen2-VL-2B-Instruct)（Apache-2.0，Qwen2-VL backbone，MTEB-zh 有评）、
[Jina v4 卡](https://huggingface.co/jinaai/jina-embeddings-v4)（CC-BY-NC-4.0）。

**主流做法 / 判断**：
- 「有字要检索」→ **① OCR**（中文首选 PaddleOCR）；「有图要检索语义」→ **② caption 或 ③ 联合嵌入**。
- **务实组合**：**每条图既存原图、又存一条 caption、再用 ③ 嵌原图**（三者各存一份，检索时融合）。
  代价是重复；收益是「字面命中（OCR）+ 语义命中（caption/联合嵌入）」都保住——与前篇 §5「混合检索」同一条思路。
- **一条修正**：多模态统一嵌入器常有 **modality gap**（文本侧偏置，可能检到错模态），
  且 ColPali 式多向量会让**索引膨胀**（每页上千向量）。见 [UniversalRAG 论文 2504.20734](https://arxiv.org/abs/2504.20734)（**仅二手，未验证**其具体数字）。
- **零额外依赖的特殊选项**：本项目**主模型本来就是 VLM**（§0），所以 **② caption 这条路「不用装任何东西」**——
  用 `deepseek-flash` 写 caption，再走**已有的文本嵌入**。代价是描述有损、且 caption 也是 token 花费。

### 4.2 PDF 里的内嵌图片：Docling 与 Unstructured **都能抽**（已核实）

- **Docling**（**MIT**）：`PdfPipelineOptions(generate_picture_images=True)`（**默认为 False，不开则拿不到图**），
  遍历 `PictureItem`/`TableItem` → `element.get_image(doc).save(...)`；
  或 `save_as_markdown(..., image_mode=ImageRefMode.EMBEDDED | REFERENCED | PLACEHOLDER)`；
  CLI 有 `--image-export-mode`。docling 还自带「给抽出的图生成描述」的示例，**正好接 ② 路**。
- **Unstructured**（OSS 库 **Apache-2.0**）：`partition_pdf(strategy="hi_res", extract_image_block_types=["Image","Table"], extract_image_block_to_payload=True|False, extract_image_block_output_dir=...)`。
  **`strategy="hi_res"` 是硬前提**（抽图只在这个策略下）；`extract_images_in_pdf` 是**旧参、已标记废弃**，
  现用 `extract_image_block_types`。注意：**hi_res 要拉它的布局/表格模型**（`unstructured-inference`，各自许可要逐模型确认）。

一手来源：[Docling 导出图示例](https://docling-project.github.io/docling/_generated/examples/export_figures/)、
[Docling 图描述示例](https://docling-project.github.io/docling/_generated/examples/pictures_description/)、
[Docling CLI](https://docling-project.github.io/docling/reference/cli/)、[Docling LICENSE (MIT)](https://github.com/docling-project/docling/blob/main/LICENSE)、
[Unstructured PDF 源码 `extract_image_block_types`](https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/pdf.py)、
[Unstructured 文档](https://docs.unstructured.io/api-reference/legacy-api/partition/extract-image-block-types)、[Unstructured LICENSE (Apache-2.0)](https://github.com/Unstructured-IO/unstructured/blob/main/LICENSE.md)。

> 与前篇 §4.3 的衔接：文本侧 PDF 解析本就「没有零依赖且质量好的选项」（pypdf 有损、重型方案拖模型）。
> **要抽图只能上重型方案**——Docling（MIT，重量级）或 Unstructured（`hi_res` 强制拉模型）。
> 「PDF 内嵌图」把「要不要引入重型解析器」这个本就存在的分叉**变得更硬**。

---

## 5. 底座重估：图片是否改变选型（问题 5）

在「含图片、规模可能比纯文本大」的假设下，重估前篇 §2 的底座，并纳入 **Qdrant / Milvus** 作专业对照。

| | **sqlite-vec** | **LanceDB** | **Chroma** | **pgvector** | **Qdrant** | **Milvus** |
|---|---|---|---|---|---|---|
| 形态 | SQLite 扩展 | 嵌入式 Rust（Lance 列存） | 嵌入式/单机 server | Postgres 扩展 | **独立服务** | **分布式服务** |
| **原生二进制列** | ✅ 辅助列 `+image blob` | ✅ **一等 binary 列 + Blob API（惰性/流式）** | ❌ base64 | ✅ bytea（非它之功） | ❌ JSON only | ❌ 无 BLOB（VARCHAR≤65535B） |
| **ANN 索引** | ❌ **无，纯暴力** | ✅ IVF / IVF-HNSW | ✅ HNSW | ✅ HNSW / IVFFlat | ✅ HNSW | ✅ **最全**（FLAT/IVF/PQ/HNSW/DiskANN…） |
| **原生「图→向量」** | ❌ | ✅（OpenCLIP/ColPali/Jina，`SourceField` 直吃 image bytes） | ❌ | ❌ | ❌ | △ 多向量字段（非「存原图」） |
| 单机契合 | **最高（零新增）** | 高（文件即库） | 中（索引常驻 RAM） | 低（要 Postgres） | **低（要服务）** | **最低（分布式）** |
| 前篇定位 | 保底 | 备选 | 一手适配 | 功能全 | 新纳入对照 | 新纳入对照 |

### 5.1 **因「图片」而改变的结论**

1. **「原图能不能入库」第一次分出胜负**：只有 **LanceDB / sqlite-vec / pgvector** 能原生放字节；
   **Chroma / Qdrant / Milvus 只能 base64 塞字符串**（膨胀、进索引/内存）。前篇说 Chroma「接口最顺手」，
   但**在图片这一维上 Chroma 反而最差**——它自己的文档都劝大图外置。
2. **LanceDB 从「备选」升为「含图场景最契合的嵌入式底座」**：原生二进制列 + Blob 惰性/流式读 +
   **原生多模态嵌入函数** + IVF/IVF-HNSW ANN，且仍是「文件即库」的单机形态。前篇里它只输在
   「LangChain 适配器在未维护的 community 包」——但**多模态底下我们本来就要自写工具**（前篇 §1.4 已得出
   「可定制性比工厂函数重要」），所以这条劣势的权重下降。
3. **规模压力被放大**：图比文本多、向量更大（多向量/ColPali 更甚），
   **sqlite-vec 的「无 ANN、全表暴力」硬伤提前暴露**（见 §6）。纯文本几千条能忍，含图上万条就未必。
4. **原图落盘必须显式决定**：小图进库、大图落文件系统 + 指针（§2.2）。

### 5.2 **不变的结论**

- 向量维度/距离度量、KNN 与过滤的基本交互，与有没有图无关。
- **Qdrant / Milvus 仍是「与单机个人助手相反」的选项**：未因图片变得合适——二者都**存不了原生二进制**，且是独立/分布式服务。
  它们的价值只作**能力对照基准**（Milvus 的 ANN 种类最全、Qdrant 的 `exact`/payload 过滤最精细）。
- **「一张图要不要配元数据过滤」的形状差异不变**：Chroma/pgvector 声明式 dict、sqlite-vec 列谓词、前篇结论照旧。
- **pgvector 技术上完全胜任**（bytea 同表 + HNSW），但**「要起一个 Postgres 进程」这条原罪不变**。

> **一句话**：图片把取舍从「哪个库 API 顺手」改成「**能否原生存二进制 × 有没有 ANN × 大对象能否惰性读**」。
> 三个都满足的嵌入式选项里，**LanceDB 是唯一全中的**；sqlite-vec 仍保底（能存小图、但无 ANN 且全量读）；
> Chroma / Qdrant / Milvus 在「原图入库」这一项上要么 base64 要么加常驻服务；pgvector 可行但要 Postgres。

---

## 6. 规模阈值：暴力扫描到多少不可接受（问题 6）

**一手锚点**是 sqlite-vec 作者 Alex Garcia 自己的博客与 benchmark（[sqlite-vec stable release](https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html)，一手）：

- 作者自述适用面：「大多数项目是**几千、也许几十万**向量，很少到百万级」，
  实用上限「**大概在几十万（hundreds of thousands）**，取决于维度/量化」。
- 实测（Mac Mini M1 / 8GB，k=20，逐条串行）：**10 万向量落盘**，768/1024 维均 **<75ms**，1536 维 105ms，3072 维 214ms；
  **100 万向量落盘**——「**任何维度的浮点向量都过不了 100ms**」，3072 维 **8.52s**，192 维也 192ms；
  **二值化 3072 维 100 万 → 124ms**。
- **LanceDB 官方口径**（[Vector Index 文档](https://docs.lancedb.com/indexing/vector-index)，一手）：
  「≤10 万行、或能接受 ~100ms 延迟时，**通常不需要建索引**」；「超过约 100 万行、维度高、或延迟敏感时再建」。
  [Vector Search 文档](https://docs.lancedb.com/search/vector-search)补一句：暴力对「**几十万以上**」不可扩展，延迟随数据量**线性**。

**换算表（自算，非来源）**：暴力需常驻全部原始向量，`bytes = N × D × 4`（float32）：

| N \ D | 768 | 1024 | 1536 |
|---|---|---|---|
| 10k | 29 MB | 41 MB | 61 MB |
| 100k | 293 MB | 410 MB | 614 MB |
| 1M | 2.9 GB | 4.1 GB | 6.1 GB |

延迟对 N **线性**（多来源一致）：768 维下 100k ≈ 75ms ⇒ 1M ≈ **0.7–0.8s**（自估外推）。

**各库中小规模对比**：

- **sqlite-vec**：**无 ANN**，纯暴力；作者给的实用上限「几十万」。**含图场景若上到「上万图 × 多向量（ColPali 式）」会快速逼近上限**。
- **LanceDB**：**≤10 万行官方说不用索引**；IVF/IVF-HNSW 在更大规模接管。
- **Chroma**：默认 **HNSW**（`ef_construction=100`、M=16），单机即用 ANN；无 IVF/PQ/GPU。
- **Qdrant**：默认 HNSW；`params.exact=true` 可**全扫描做精确对照**；`full_scan_threshold`（默认 10000 KB）**小集合自动改全扫描**——
  即专业库也承认「小集合暴力更划算」。
- **pgvector**：**默认就是精确扫描**，加索引才近似；官方未给行数阈值（Supabase/Neon 的「<10k 够用、50k 起变贵」属**仅二手，未验证**）。
- **Milvus**：索引最全（FLAT 暴力 / IVF / HNSW / DiskANN），官方说**过滤率高或 k 大**时用 FLAT/IVF 更划算（具体数字**仅二手，未验证**）。

一手来源：[sqlite-vec stable release 博客](https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html)、
[sqlite-vec KNN](https://alexgarcia.xyz/sqlite-vec/features/knn.html)、
[LanceDB 索引](https://docs.lancedb.com/indexing/vector-index)、[LanceDB 检索](https://docs.lancedb.com/search/vector-search)、
[Chroma configure](https://docs.trychroma.com/docs/collections/configure)、
[pgvector README](https://github.com/pgvector/pgvector)、
[Qdrant 检索](https://qdrant.tech/documentation/search/search/)、[Qdrant 索引](https://qdrant.tech/documentation/concepts/indexing/)、
[Milvus 索引](https://milvus.io/docs/index-explained.md)。

> **读法**：**「几十万向量」是暴力法的软上限**（作者本人口径），**「~10 万」是 LanceDB 官方说的「可不必建索引」线**。
> 对「个人助手知识库」而言，**纯文本时几条路都在舒适区**；**含图后**，只要不是「上万张图、每张多向量」，
> sqlite-vec 暴力仍可能够用——但**一旦接近 10 万级向量，就必须有 ANN**，此时 sqlite-vec 是唯一没有的。

---

## 7. 需要用户决策的真实分叉

> 每点给选项与代价，**不替用户拍板**。前篇 §8 的六个决策仍然有效，这里是**图片新增的**分叉。

**决策 M1：多模态嵌入走哪条路——联合嵌入，还是「VLM 写描述 + 文本嵌入」？**
- A. **真·多模态嵌入**（§1）：检索上限最高、支持图搜图；代价：**几乎都不是 OpenAI 协议**（要新 partner 包/自写 client）、
  图按像素或图 token 计价、中文效果**分厂商**（DashScope/Cohere/Jina 明确，Voyage 存疑）。
- B. **把图交给主模型 `deepseek-flash` 写 caption，再用现有文本嵌入**：**零额外依赖**（模型已多模态）、
  复用整套文本栈与中文能力；代价：**描述有损**（图表/表格尤其）、caption 也花 token、图搜图退化。
- （可叠加：两者都做，检索时融合——成本与复杂度最高。）

**决策 M2：图像是「一等可被搜到的对象」，还是「文档的附属附件」？**
- A. **一等对象**（图本身可被语义检索、可图搜图）：需要真·多模态嵌入（M1-A）+ 能存原图的底座（§5）。
- B. **附属附件**（图只是某段文本旁边的配图，用户靠文本检索到、再看图）：走 caption/OCR 即可，底座不用换。
- 这条**决定要不要为图片动向量库底座**——若选 B，前篇 §2 的文本侧选型基本原样保留。

**决策 M3：向量库底座——sqlite-vec 不动，还是升 LanceDB？**
- A. **维持 sqlite-vec**：能存小图（辅助列 blob）、零新增、已在 venv；代价：**无 ANN**，含图且上量（~10 万向量）后暴力不可接受，大图只能全量读。
- B. **升 LanceDB**：**唯一全中**（原生二进制列 + Blob 惰性读 + 原生多模态嵌入 + ANN），仍是嵌入式；代价：引 Lance/Arrow 心智与新格式目录、
  且多模态底下工具本就要自写（community 适配器的劣势权重下降）。
- （C. **只在「预计图多且大」时才升**——即 M2 选 A 才触发。）

**决策 M4：原始图像文件放哪？**
- A. **文件系统 + DB 路径指针**（推荐，与既有 `/input`、`/workspace` 沙箱天然吻合）：无需新存储层；代价：备份/迁移要一起搬两个地方。
- B. **小图进 DB blob、大图落文件系统**（照 SQLite 官方 100KB 分界）：检索时少一次 IO；代价：两套规则、边界要定。
- C. **全部进 DB blob**：单一数据源；代价：大图全量读、SQLite 膨胀（官方说 >100KB 反而更慢）。

**决策 M5：命中图片后怎么交给模型（补齐 §3.4 的缺口）？**
- A. **新增「读沙箱图片」工具**：模型先拿到路径、按需把图变成 `image` 内容块；代价：多一轮工具调用、要写新工具。
- B. **检索工具直接返回 `image` 内容块**（不返回路径）：一步到位；代价：与「结果落 `/workspace` 返回路径」的既定形态分叉，
  上下文被图占（每图 ≤384 tok）。
- C. **只返回路径、让用户自己看**（present_files 已支持图片预览，前端 `FilePreview` 能显示）：实现最省；代价：**模型看不见图**，不能对图作答。

**决策 M6：模型/接口选择——沿用手上的 `deepseek-flash`，还是另配专用视觉/嵌入模型？**
- A. **全用 `deepseek-flash`**（VLM + 文本嵌入用别家）：图理解零新增（§0）；代价：图上限 384 tok/张、detail 需调（low/high/auto）、
  封面视模型为**每请求建图的外部依赖**。
- B. **视觉理解用一个模型、嵌入用另一个多模态嵌入 API**：能力最强；代价：**两套 key、两套计价、两套失败模式**。

---

## 附：本文的一手来源清单（按出现顺序）

- DeepSeek 定价页（`deepseek-flash` = V4.1-Flash，图像理解：支持）— https://api-docs.deepseek.com/zh-cn/quick_start/pricing
- DeepSeek V4.1-Flash 发布 — https://api-docs.deepseek.com/zh-cn/news/news260910/
- DeepSeek V4-Flash-Vision-Exp 上线（base64/URL/file_id、384 tok/图）— https://api-docs.deepseek.com/zh-cn/news/news260821/
- Voyage 多模态嵌入 — https://docs.voyageai.com/docs/multimodal-embeddings · https://docs.voyageai.com/docs/pricing
- Cohere 模型 — https://docs.cohere.com/docs/models · https://docs.cohere.com/docs/multimodal-embeddings
- Jina v5-omni — https://huggingface.co/jinaai/jina-embeddings-v5-omni-small · v4 — https://huggingface.co/jinaai/jina-embeddings-v4 · clip-v2 — https://huggingface.co/jinaai/jina-clip-v2
- DashScope 多模态嵌入 API — https://help.aliyun.com/zh/model-studio/multimodal-embedding-api-reference
- Gemini Embeddings（`gemini-embedding-2`）— https://ai.google.dev/gemini-api/docs/embeddings
- OpenAI 嵌入模型页（Image Not supported）— https://developers.openai.com/api/docs/models/text-embedding-3-large
- SiliconFlow 嵌入目录 — https://siliconflow.cn/models?q=embedding
- LanceDB 多模态 — https://docs.lancedb.com/tables/multimodal · Lance 格式 — https://docs.lancedb.com/lance · Blob 指南 — https://lance.org/guide/blob/
- LanceDB 索引 — https://docs.lancedb.com/indexing/vector-index · 检索 — https://docs.lancedb.com/search/vector-search
- LanceDB 嵌入函数（OpenCLIP/ColPali/Jina）— https://docs.lancedb.com/integrations/embedding/openclip · https://docs.lancedb.com/integrations/embedding/colpali
- sqlite-vec vec0（辅助列 `+image blob`）— https://alexgarcia.xyz/sqlite-vec/features/vec0.html · KNN — https://alexgarcia.xyz/sqlite-vec/features/knn.html
- sqlite-vec stable release 博客（作者实测/实用上限）— https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html · README — https://github.com/asg017/sqlite-vec
- Chroma Schema — https://docs.trychroma.com/cloud/schema/index-reference · add-data — https://docs.trychroma.com/docs/collections/add-data · configure — https://docs.trychroma.com/docs/collections/configure
- pgvector README — https://github.com/pgvector/pgvector
- Qdrant payload — https://qdrant.tech/documentation/manage-data/payload/ · 检索 — https://qdrant.tech/documentation/search/search/ · 索引 — https://qdrant.tech/documentation/concepts/indexing/
- Milvus schema — https://github.com/milvus-io/milvus-docs/blob/v2.5.x/site/en/userGuide/schema/schema.md · string — https://github.com/milvus-io/milvus-docs/blob/v2.5.x/site/en/userGuide/schema/string.md · 索引 — https://milvus.io/docs/index-explained.md
- SQLite Internal vs External BLOBs — https://www.sqlite.org/intern-v-extern-blob.html · 35% Faster Than The Filesystem — https://www.sqlite.org/fasterthanfs.html
- LangChain messages（多模态）— https://docs.langchain.com/oss/python/langchain/messages · tools（返回多模态）— https://docs.langchain.com/oss/python/langchain/tools
- LangChain 标准消息内容块（v1）— https://www.langchain.com/blog/standard-message-content
- Deep Agents 多模态说明（压缩丢图/文本 token）— https://docs.langchain.com/oss/python/deepagents/multimodal
- 本机实装源码（已核实）：`langchain_core/messages/content.py`（`ImageContentBlock` / `create_image_block`）、`messages/tool.py`（`content_blocks` / `artifact`）、`messages/base.py`（`content_blocks` 属性）、`block_translators/openai.py`、`tools/base.py`（`TOOL_MESSAGE_BLOCK_TYPES`）、`langgraph/prebuilt/tool_node.py`、`langchain_openai/chat_models/base.py`、`langchain/agents/factory.py`
- Docling 导出图 — https://docling-project.github.io/docling/_generated/examples/export_figures/ · 图描述 — https://docling-project.github.io/docling/_generated/examples/pictures_description/ · LICENSE(MIT) — https://github.com/docling-project/docling/blob/main/LICENSE
- Unstructured PDF 抽图 — https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/pdf.py · 文档 — https://docs.unstructured.io/api-reference/legacy-api/partition/extract-image-block-types · LICENSE(Apache-2.0) — https://github.com/Unstructured-IO/unstructured/blob/main/LICENSE.md
- PaddleOCR — https://github.com/PaddlePaddle/PaddleOCR · RapidOCR — https://github.com/RapidAI/RapidOCR · Tesseract — https://github.com/tesseract-ocr/tesseract
- BGE-VL — https://huggingface.co/BAAI/BGE-VL-base · GME — https://huggingface.co/Alibaba-NLP/gme-Qwen2-VL-2B-Instruct
- UniversalRAG（多模态嵌入 modality gap，仅二手）— https://arxiv.org/abs/2504.20734
- 本项目源码（实读）：`backend/packages/harness/evaharness/sandbox/file_ops.py`（`read_file` 拒二进制）、`backend/packages/harness/evaharness/models/deepseek.py`、`backend/config.yaml`、`backend/app/tools/present_files.py`
