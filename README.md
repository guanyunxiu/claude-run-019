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

| 账号 | 租户 | 角色 | 团队 / 部门 | clearance |
|---|---|---|---|---|
| alice@lab.cn | biolab 生物实验室 | 租户管理员 | 分子生物学团队 / 研发部 | secret |
| bob@lab.cn | biolab | 成员 | 分子生物学团队 / 研发部 | sensitive |
| carol@lab.cn | biolab | 成员 | 细胞生物学团队 / 研发部 | sensitive |
| dave@lab.cn | biolab | 成员 | 基因治疗团队 / 临床部 | internal |
| erin@chem.cn | chemmat 化学材料中心 | 租户管理员 | 催化团队 / 材料部 | secret |

seed 另含《新一代载体临床申报机密实验方案》（secret，含一个被覆盖为 public 但密级仍
secret 的片段），用于验证 clearance 不足时 public/team 也无法绕过密级闸门。

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

#### 三维权限：时限授权 + 显式 deny + 密级 clearance

在「有授权就能看」之上，有效权限统一为：

```
可访问 = 基础可见性(或有效 allow 授权)
         ∧ 用户 clearance ≥ 片段有效密级
         ∧ ¬命中 deny（片段级或文档级，且 deny 未过期）
         ∧ 命中的 allow 未过期
```

- **密级 classification**：`internal(0) < sensitive(1) < secret(2)`。文档与片段
  都有密级，片段密级为 NULL 时继承文档；成员在租户内有 `clearance`（存 tenant_users，
  默认 internal）。**密级闸门独立于可见性——把 secret 片段覆盖为 public，clearance
  不足者在列表/检索/问答/溯源仍然拿不到**，杜绝「public 覆盖绕过密级」。
- **显式拒绝 effect=deny**：`chunk_grants` 与文档级 `document_rules` 均支持
  `allow`/`deny`，deny 永远优先于 allow 与 public/team 可见性；文档级 deny 对整篇
  文档全部片段生效（列表也不出现）。
- **时限授权**：规则带 `expires_at`（或创建时传相对秒数 `expires_in`），过期后
  立即从可访问白名单消失，无需删除规则；deny 过期同样自动解除。
- **管理员旁路**：租户 `admin` 旁路密级/deny/时限，对本租户全部内容可读可管（审计运维），
  但旁路**仅限租户内**——跨租户仍由独立库文件硬隔离，谓词始终绑定 `tenant_id`。
- 列表/检索/问答/溯源四个出口共用同一谓词（`accessible_chunks_where` 与
  `accessible_document_where`），保证语义一致：**文档可见当且仅当至少存在一个可访问片段**，
  因此不会出现“列表/详情 200 但检索为 0、片段列表为空”的空壳文档——public/team 文档
  若全部片段被 deny（或密级全部不足），列表与详情同样不返回；能进入文档就至少能看到一个片段。
- **信封不泄露隐藏分片**：对仅能访问部分片段的用户，列表/详情返回的 `classification`
  是“可见片段中的最高密级”（不是整篇密级），`chunk_count`/`char_count` 只统计可见片段，
  避免通过文档信封推断隐藏分片数量、字数与密级；管理员/所有者看到的是整篇真实值。
- 任何密级/规则/授权变更、文档新增/删除都递增租户级**单调索引代数**（`meta.index_generation`），
  BM25 内存倒排按代数缓存并实时重建；用单调计数器而非“版本号加总+行数”，避免删除后重传
  同构文档时指纹撞回旧值。
- 接口：
  - 成员密级：`PUT /api/admin/members/{uid}/clearance`，加成员时可传 `clearance`；
  - 文档密级：`PUT /api/documents/{id}/classification`（上传可带 `classification`）；
  - 片段密级：`PUT /api/chunks/{cid}/classification`；
  - 片段规则：`POST /api/chunks/{cid}/grants`，body 支持
    `{subject_type, subject_value, effect:"allow|deny", expires_in?:秒, expires_at?}`；
  - 文档规则：`GET/POST /api/documents/{id}/rules`、
    `DELETE /api/documents/{id}/rules/{rid}`。

### 4. 权限穿透式检索
- 检索分三步，权限永远先于内容展示：
  1. 依据登录身份生成「**可访问片段白名单 SQL 谓词**」（可见性 + 密级闸门 +
     deny 排除 + 时限/allow）；
  2. 仅对白名单片段累计 **BM25 分数**（中文单字 + 双字组 bigram、英文/数字按词），
     越权/密级不足/被拒/过期片段分数恒为 0，**物理上无法进入结果**；
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
- **TOCTOU 复核**：问答在三处按“当前”权限实时校验，防止“检索后、返回前”被加 deny/降密
  （慢 LLM 场景尤甚）导致本次响应泄权——①检索命中后先过滤再构造上下文；②LLM 返回后，
  若其引用的任一片段在调用期间失效则丢弃生成答案并降级；③返回前最终复核 `sources`。
  被 deny 的片段不会出现在答案正文或来源中。
- 每条回答附 `sources`：**原文文档名、源文件名、章节路径、片段序号、页码、字符区间、BM25 分**；
  溯源接口 `GET /api/documents/{id}/chunks/{cid}/source` 返回片段精确原文与前后文，
  **前后文窗口按相邻片段的 ACL/密级/deny/时限逐条裁切**：窗口不得滑入相邻无权片段
  （含其章节标题），返回 `context_clipped/clipped_side` 标记是否发生裁切；管理员旁路不裁切。

---

## 三、HTTP API 速览

| 方法 & 路径 | 说明 | 权限 |
|---|---|---|
| POST `/api/auth/register` | 注册 | 公开 |
| POST `/api/auth/login` | 登录（body 可选 `tenant_slug`） | 公开 |
| POST `/api/auth/logout` / GET `/api/me` | 会话 | 登录 |
| GET `/api/tenants` | 租户列表（供选择登录上下文） | 公开 |
| POST `/api/admin/tenants` | 创建租户 | 平台管理员 |
| GET/POST `/api/admin/members` | 成员管理（POST 可带 `clearance`） | 租户管理员 |
| PUT `/api/admin/members/{uid}/clearance` | 调整成员密级许可 | 租户管理员 |
| DELETE `/api/admin/members/{user_id}` | 移出租户（**立即删除该租户下其全部会话**，旧令牌即时失效） | 租户管理员 |
| POST `/api/documents` | 上传（multipart：file/title/visibility/classification/owner_team/owner_dept） | 登录（须为本租户成员） |
| GET `/api/documents` · `/{id}` · DELETE `/{id}` | 列表/详情按**读权限+密级+deny** 过滤；删除需管理权 | 登录 |
| GET `/api/documents/{id}/chunks` | 片段+规则（管理者看全量，成员只看可访问片段） | 登录 |
| GET `/api/documents/{id}/chunks/{cid}/source` | 片段溯源原文定位 | 登录（受权） |
| PUT `/api/chunks/{cid}/visibility` · `/classification` | 设置片段可见性/密级（null=继承） | 所有者/管理员 |
| POST/DELETE `/api/chunks/{cid}/grants[/{gid}]` | 片段 allow/deny 规则（可带 `expires_in/expires_at`） | 所有者/管理员 |
| PUT `/api/documents/{id}/classification` | 设置文档密级 | 所有者/管理员 |
| GET/POST/DELETE `/api/documents/{id}/rules[/{rid}]` | 文档级 allow/deny 规则（整篇生效） | 所有者/管理员 |
| POST `/api/search` | 权限穿透检索 | 登录 |
| POST `/api/ask` | 问答 + 溯源 | 登录 |
| GET `/api/audit` | 权限变更审计查询（`start/end/actor_id/document_id/action/limit/offset`） | 租户管理员 |
| GET `/api/audit/export` | 审计导出 CSV（同过滤参数） | 租户管理员 |

### 权限变更审计（只追加）

- 租户库内置 `audit_log` 表，记录**谁（id/姓名/邮箱/IP）、在什么时间、做了什么动作、
  作用在哪个对象（文档/片段/规则/成员）、改前改后 JSON 摘要**。
- 覆盖动作：文档上传/删除/改密级、片段可见性/密级变更、grant 与文档规则的增删
  （含 allow/deny/有效期）、成员加入（`member.add`）、**成员信息更新（`member.update`，
  对已在租户用户重复 POST 时按改前/改后留痕）**、成员密级调整（成员更新中密级变化也记
  `member.clearance_update`，与专门接口一致，不伪装成加入）、踢人。
- **只追加护栏**：表上有 `BEFORE UPDATE/DELETE` 触发器，任何改写或抹除（含直接连库）都会被
  SQLite `RAISE(ABORT)` 拒绝；应用层也无修改/删除接口。
- 访问控制：仅**本租户管理员**可查/导出，普通成员调用返回 403、匿名 401；查询强制带
  `tenant_id` 闸门，审计存于各租户独立库，**别的租户审计完全看不到**。
- 全局库操作（成员密级/踢人/加成员）也会把审计冗余写入对应租户库，保证单租户审计完整。
- `GET /api/audit?document_id=&actor_id=&action=&start=&end=&limit=&offset=` 返回 JSON：
  普通查询默认 200 条、硬上限 2000；`GET /api/audit/export?...` 返回带 BOM 的 UTF-8 CSV
 （Excel 可直接打开），**导出使用独立的更高上限：默认 10000、硬上限 10000**，
  不受普通查询 200/2000 的限制。
- **写路径一致**：`scripts/seed.py` 与建租户接口走与正式 API 相同的审计写入——seed 的
  文档上传、给 Bob 的片段授权、机密片段可见性/密级覆盖、成员加入都会落 `audit_log`；
  平台管理员 `POST /api/admin/tenants` 指定 `admin_email` 时，新租户也会记录该管理员的
  `member.add`。初始化动作的 actor 标记为「系统初始化」。

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
    db_tenant.py            # 租户库：文档/片段/授权/文档规则/只追加审计/索引代数（独立文件）
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
