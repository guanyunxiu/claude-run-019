# 权限感知型私有科研知识库问答系统

面向科研团队的**私有化**知识库问答系统：多格式科研文档入库 → 超长文档语义分段 →
文档/片段细粒度权限绑定 → 权限穿透式检索 → 多租户硬隔离 → 带来源溯源的问答。

**核心特点：零强制第三方依赖**——仅使用 Python 3.10+ 标准库即可运行
（`http.server` + `sqlite3` + `zipfile`/`zlib` 自研 PDF/Word 解析 + 自研 BM25）。
若环境中装有 `pypdf` / `python-docx`，会自动优先使用。

---

## 一、快速开始

```bash
# 无需 pip install（纯标准库）。可选增强：pip install -r requirements.txt

# 1) 初始化演示租户/账号/文档（两个相互隔离的租户 + 不同权限身份）
python3 -m scripts.seed

# 2) 启动服务
python3 run.py --host 0.0.0.0 --port 8080
# 浏览器打开 http://localhost:8080
```

演示账号（密码均为 `demo123`）：

| 账号 | 租户 | 角色 | 团队 / 部门 |
|---|---|---|---|
| alice@lab.cn | biolab 生物实验室 | 租户管理员 | 分子生物学团队 / 研发部 |
| bob@lab.cn | biolab | 成员 | 分子生物学团队 / 研发部 |
| carol@lab.cn | biolab | 成员 | 细胞生物学团队 / 研发部 |
| dave@lab.cn | biolab | 成员 | 基因治疗团队 / 临床部 |
| erin@chem.cn | chemmat 化学材料中心 | 租户管理员 | 催化团队 / 材料部 |

> 生产首次部署：先注册账号，再用平台管理员接口 `POST /api/admin/tenants` 建租户、
> `POST /api/admin/members` 把用户加入租户（平台管理员标志位 `users.is_platform_admin`）。

运行测试：

```bash
# 单元测试（不需要起服务）
python3 -m unittest tests.test_units -v

# 端到端 API 测试（需要先 seed 并启动服务）
python3 tests/test_api.py
```

---

## 二、六大需求与实现对照

### 1. 多格式私有文档入库解析
- 支持 `.pdf / .docx / .doc(提示另存) / .txt / .md / .log`（科研报告、实验日志等）。
- PDF：优先 `pypdf`，否则使用内置解析器（xref/对象/流解压/页面树/文本操作符
  `Tj/TJ/'/"`/换行定位/**ToUnicode CMap（bfchar/bfrange）中文解码**/跳过内联图片）。
- Word：优先 `python-docx`，否则内置 `zipfile + XML` 解析（段落、标题样式、显式分页符、表格）。
- 清洗：自动识别并剥离跨页重复的**页眉/页脚/页码**（逐位置跨页对齐，数字归一化），
  规整空行与多余空白；TXT 自动做 UTF-8/GB18030 编码探测。
- 原文件按租户隔离落盘，清洗后全文入库，形成团队私有素材库。
- 扫描件（无文字层 PDF）会明确提示先做 OCR。

### 2. 超长文档智能分段存储
- 识别论文式章节结构（`1 / 2.3.1 / 第x章 / 摘要 / Abstract / 材料与方法 / …`、Markdown 标题），
  构建**多级标题路径**（如 `3 实验结果 / 3.2 包封率与释放行为`）。
- **逻辑段落为最小不可割裂单元**：小段落向目标长度（默认 900 字）合并，绝不跨标题合并；
  超长段落先按句末标点（。！？；;）切分，无标点极端情况硬切，保证片段长度有上限（默认 1400 字）。
- 每个片段记录：所属文档、序号、章节路径、全文**字符偏移 [start,end)**、**页码区间**，
  为细粒度授权与精准溯源奠基。

### 3. 细粒度权限绑定
- 文档级：`private`（私有）/ `team`（团队公开）/ `department`（部门公开）/ `public`（租户公开）。
  上传时若选 team/department，归属团队/部门**必填**（缺省取上传者本人的团队/部门；
  上传者自身没有归属时必须显式填写），否则返回 400，避免产生「除所有者外无人可见」的文档。
- 片段级：`chunks.visibility` 可**单独覆盖**文档可见性（`NULL` = 继承文档）。
- 片段附加授权 `chunk_grants`：向**指定用户 / 指定角色 / 指定团队 / 指定部门**放行单个片段。
- 管理权限：租户管理员可管理本租户全部内容；文档所有者可管理其文档与片段。
- **读权限与管理权分离**：同团队/同部门/被片段授权的成员可查看文档详情、片段与溯源原文，
  但不能改可见性、加授权或删除（`can_read_document` vs `can_manage_document`）。
- **文档列表与片段可见性一致**：若私有文档中某个片段被单独覆盖为 public/team/department
  （或被附加授权），该文档会出现在对应可访问者的文档列表中，点进去只能看到其有权的片段；
  列表谓词复用片段级 effective-visibility 判定，保证“检索/问答能命中 ⇔ 列表能进入”。
- 接口：`PUT /api/chunks/{id}/visibility`、`POST /api/chunks/{id}/grants`、
  `DELETE /api/chunks/{id}/grants/{gid}`。

### 4. 权限穿透式检索
- 检索分三步，权限永远先于内容展示：
  1. 依据登录身份生成「**可访问片段白名单 SQL 谓词**」（文档/片段可见性 + 4 类授权主体）；
  2. 仅对白名单片段累计 **BM25 分数**（中文单字 + 双字组 bigram、英文/数字按词），
     越权片段分数恒为 0，**物理上无法进入结果**；
  3. 回取数据时**再次叠加权限谓词**（纵深防御）。
- 中文分词区分强词元（双字组/英文词）与弱词元（单字兜底），避免“率”误命中“转化率”。
- 接口：`POST /api/search`。

### 5. 多租户数据硬隔离（五层）
1. **独立库文件**：每个租户一个 SQLite（`data/tenants/<slug>.db`），schema 中再带 `tenant_id` 闸门；
2. **独立文件目录**：原文存 `data/blobs/<slug>/`；
3. **租户身份来自服务端令牌**：API 从不接受客户端自报租户，slug 做白名单字符校验防路径穿越；
   令牌校验对成员关系使用 **INNER JOIN**——用户被移出租户后，其未过期的旧 Bearer 令牌也会
   **立即失效**（同步删除该租户下其全部会话），无法再鉴权或写入租户库；
4. **每请求只打开所属租户连接**，应用层不存在跨租户 JOIN/查询的代码路径；
5. 全局库仅存租户/账号/令牌，**不含任何业务数据**。

### 6. 单轮精准问答 + 基础溯源
- 默认**内置抽取式问答**（离线、无密钥）：基于检索命中片段做查询覆盖度句级抽取。
- 可选 **LLM 生成式**：配置 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`（OpenAI 兼容
  `/chat/completions`）后，将“**已通过权限过滤**”的片段作为唯一上下文，要求模型只依据资料作答
  并标注 `[来源n]`；LLM 调用失败或返回为空时自动降级为抽取式，响应中
  `mode="extractive"`、`degraded=true`，前端明确提示“已配置 LLM 但调用失败，已自动降级”，
  不会误显示为“LLM 生成”。
- 每条回答附 `sources`：**原文文档名、源文件名、章节路径、片段序号、页码、字符区间、BM25 分**；
  溯源接口 `GET /api/documents/{id}/chunks/{cid}/source` 返回片段精确原文与前后文。

---

## 三、HTTP API 速览

| 方法 & 路径 | 说明 | 权限 |
|---|---|---|
| POST `/api/auth/register` | 注册 | 公开 |
| POST `/api/auth/login` | 登录（body 可选 `tenant_slug`） | 公开 |
| POST `/api/auth/logout` / GET `/api/me` | 会话 | 登录 |
| GET `/api/tenants` | 租户列表（供选择登录上下文） | 公开 |
| POST `/api/admin/tenants` | 创建租户 | 平台管理员 |
| GET/POST `/api/admin/members` | 租户成员管理 | 租户管理员 |
| DELETE `/api/admin/members/{user_id}` | 移出租户（**立即删除该租户下其全部会话**，旧令牌即时失效） | 租户管理员 |
| POST `/api/documents` | 上传（multipart：file/title/visibility/owner_team/owner_dept） | 登录（须为本租户成员） |
| GET `/api/documents` · `/{id}` · DELETE `/{id}` | 列表/详情按**读权限** ACL 过滤；删除需管理权 | 登录 |
| GET `/api/documents/{id}/chunks` | 片段+授权（管理者看全量，成员只看可见） | 登录 |
| GET `/api/documents/{id}/chunks/{cid}/source` | 片段溯源原文定位 | 登录（受权） |
| PUT `/api/chunks/{cid}/visibility` | 设置片段可见性（null=继承） | 所有者/管理员 |
| POST/DELETE `/api/chunks/{cid}/grants[/{gid}]` | 片段授权管理 | 所有者/管理员 |
| POST `/api/search` | 权限穿透检索 | 登录 |
| POST `/api/ask` | 问答 + 溯源 | 登录 |

调用示例：

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"bob@lab.cn","password":"demo123","tenant_slug":"biolab"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -s -X POST http://localhost:8080/api/ask \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"LPN-207 的包封率是多少？"}'
```

---

## 四、代码结构

```
app/
  config.py                 # 全部配置（数据目录、分段/检索参数、LLM）
  core/
    db_global.py            # 全局库：租户/账号/成员/会话
    db_tenant.py            # 租户库：文档/片段/片段授权/查询日志（每租户独立文件）
    security.py             # PBKDF2 口令哈希、会话令牌
    permissions.py          # 可见性模型 + 可访问片段/文档 SQL 谓词
    retrieval.py            # 中文分词 + BM25（权限白名单过滤、按租户缓存）
    qa.py                   # 抽取式问答 + 可选 LLM + 溯源组装
  parsing/
    base.py                 # Page/ParsedDocument 与解析分发
    pdf_extract.py          # pypdf 优先 + 内置 PDF 解析器（含 ToUnicode 中文）
    docx_extract.py         # python-docx 优先 + 内置 ZIP/XML 解析
    text_extract.py         # TXT/MD/日志：编码探测、分页
    cleaner.py              # 页眉页脚识别剥离、空白规整、全文页码区间
    chunker.py              # 章节识别 + 语义自适应分段 + 精确偏移/页码
  api/
    server.py               # 零依赖 HTTP 路由、鉴权、全部接口
    context.py              # 请求上下文（令牌→租户连接，隔离收口）
    request_utils.py        # JSON / multipart 解析
    documents_service.py    # 入库流水线（保存→解析→清洗→分段→落库）
web/index.html              # 单页演示界面（登录/上传/权限/检索/问答/溯源）
scripts/seed.py             # 演示数据
tests/                      # 单元测试 + 端到端 API 测试
data/                       # 运行后生成：global.db、tenants/*.db、blobs/<slug>/
```

---

## 五、关键设计说明

- **为什么检索不会越权？** BM25 索引虽覆盖租户全量片段（为了 IDF 统计与性能），
  但打分循环对每个命中文档都执行 `if cid not in allowed: continue`，结果回取再带一次权限谓词；
  无权限片段不可能产生分数，也不可能被读出。已由 `tests/test_api.py` 中
  “文档内未授权片段仍不可见 / 授权后仅该片段可见 / 跨租户全部为空”等用例固化验证。
- **索引一致性**：文档权限/内容变更会递增 `index_version` 指纹，BM25 缓存自动失效重建。
- **溯源精确性**：分段器的 `char_start/char_end` 在单测中断言
  `full_text[start:end] == chunk.content`，保证前端“原文定位”永不错位。
- **口令**：PBKDF2-HMAC-SHA256（20 万轮）加盐；令牌为 `secrets.token_urlsafe`，12 小时过期。

## 六、可配置项（环境变量）

`KB_DATA_DIR`、`KB_HOST`、`KB_PORT`、`KB_CHUNK_TARGET`（默认 900）、
`KB_CHUNK_MAX`（默认 1400）、`KB_TOP_K`（默认 8）、`KB_MAX_UPLOAD_MB`（默认 100）、
`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`LLM_TIMEOUT`。

## 七、局限与后续可扩展

- 内置 PDF 解析器面向“有文字层”的常见 PDF；含加密、复杂 CID 字体/竖排、Ligature 等场景建议安装 `pypdf`。
- 问答默认是抽取式，适合离线/内网；接入内网部署的 LLM（如通过 OpenAI 兼容网关）即可获得生成式能力。
- 后续可加：OCR 流水线、向量检索混合召回、片段密级标签、审计日志导出、SSO/LDAP、租户级配额。
